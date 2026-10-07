import copy
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from server.core import connect,initialize,canonical,digest,verify
from server.live import initialize_live,store_observations
from server.predictions import (build_forecast,estimate,training_events,publish,settle,forecast_ledger,evaluation,refresh_predictions,predictions_summary,verify_predictions,outcome,monitoring)

class PredictionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=connect(self.tmp.name+'/test.db');initialize(self.db);initialize_live(self.db)
        self.now=datetime(2026,10,7,14,tzinfo=timezone.utc)
        self.source='espn:nba:events';self.feed=dict(self.db.execute('SELECT * FROM live_feeds WHERE id=?',(self.source,)).fetchone())
        self.history=[]
        for i in range(12):
            e=self.event('past-'+str(i),self.now-timedelta(days=14-i),True)
            e['home']['score']='110' if i<9 else '90';e['away']['score']='100'
            e['content_hash']=digest({k:v for k,v in e.items() if k!='content_hash'})
            self.history.append(e)
        self.fixture=self.event('future',self.now+timedelta(hours=4),False)
        self.store(self.fixture)
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def event(self,id,start,completed):
        e={'external_id':id,'league':'NBA','source':self.source,'name':'Away at Home','starts_at':start.isoformat(),'home':{'id':'1','name':'Home','score':'110' if completed else None},'away':{'id':'2','name':'Away','score':'100' if completed else None},'state':'post' if completed else 'pre','completed':completed,'status':'Final' if completed else 'Scheduled','source_url':'https://www.espn.com/nba/game/_/gameId/1','fetched_at':(self.now-timedelta(minutes=2)).isoformat(),'last_checked_at':(self.now-timedelta(minutes=1)).isoformat(),'fresh':True}
        e['content_hash']=digest(e);return e
    def store(self,e):
        payload={k:v for k,v in e.items() if k not in ('source','fetched_at','last_checked_at','fresh','content_hash')}
        e['content_hash']=digest(payload)
        with self.db:
            store_observations(self.db,self.feed,[payload],e['fetched_at'])
            self.db.execute('UPDATE live_seen SET last_seen=? WHERE source=? AND external_id=?',(e['last_checked_at'],e['source'],e['external_id']))
            self.db.execute("UPDATE live_feeds SET status='ok',last_success=? WHERE id=?",(e['last_checked_at'],e['source']))
    def build(self):
        forecast,reason=build_forecast(self.fixture,self.history+[self.fixture],self.now)
        self.assertIsNone(reason);return forecast
    def test_probability_distribution_and_training_direction(self):
        f=self.build();self.assertAlmostEqual(sum(f['probabilities'].values()),1)
        self.assertGreater(f['probabilities']['home'],.5);self.assertEqual(f['predicted_outcome'],'home')
        self.assertEqual(f['model_version'],'experimental-elo-v1');self.assertEqual(f['mode'],'experimental-live')
    def test_future_information_excluded(self):
        future=copy.deepcopy(self.history[0]);future['external_id']='leak';future['fetched_at']=(self.now+timedelta(seconds=1)).isoformat()
        used=training_events(self.history+[future],'NBA',self.now)
        self.assertEqual(len(used),12);self.assertNotIn('leak',[e['external_id'] for e in used])
        f=self.build();f['training_snapshot'][0]['observed_at']=(self.now+timedelta(days=1)).isoformat()
        with self.assertRaises(ValueError):publish(self.db,f,self.fixture,self.now)
    def test_stale_started_future_timestamps_and_small_samples_rejected(self):
        for change in ({'fresh':False},{'state':'in'},{'starts_at':self.now.isoformat()},{'last_checked_at':(self.now+timedelta(seconds=1)).isoformat()}):
            event={**self.fixture,**change};f,reason=build_forecast(event,self.history,self.now)
            self.assertIsNone(f);self.assertTrue(reason)
        f,reason=build_forecast(self.fixture,self.history[:2],self.now);self.assertIsNone(f);self.assertIn('Need 8',reason)
    def test_new_team_and_balanced_estimates_not_forecast(self):
        event=copy.deepcopy(self.fixture);event['home']['id']='unseen'
        f,reason=build_forecast(event,self.history,self.now);self.assertIsNone(f);self.assertIn('per team',reason)
        history=[{**e,'home':{**e['home'],'score':'100'},'away':{**e['away'],'score':'100'}} for e in self.history]
        self.assertEqual(training_events(history,'NBA',self.now),[])
    def test_publication_idempotency_and_immutability(self):
        f=self.build();self.assertTrue(publish(self.db,f,self.fixture,self.now))
        self.assertFalse(publish(self.db,f,self.fixture,self.now))
        self.assertEqual(len(forecast_ledger(self.db)),1)
        for query in ("UPDATE outcome_forecasts SET payload='{}'",'DELETE FROM outcome_forecasts'):
            with self.assertRaises(sqlite3.IntegrityError):self.db.execute(query)
        self.assertTrue(verify_predictions(self.db));self.assertTrue(verify(self.db))
    def test_audit_rejects_changed_fixture_and_failed_feed(self):
        f=self.build();changed=copy.deepcopy(self.fixture);changed['state']='in';self.store(changed)
        with self.assertRaises(ValueError):publish(self.db,f,self.fixture,self.now)
        self.store(self.fixture);self.db.execute("UPDATE live_feeds SET status='unavailable' WHERE id=?",(self.source,));self.db.commit()
        with self.assertRaises(ValueError):publish(self.db,f,self.fixture,self.now)
    def result(self,home_score,away_score):
        result=copy.deepcopy(self.fixture);result.update({'completed':True,'state':'post','status':'Final','fetched_at':(self.now+timedelta(hours=7,minutes=59)).isoformat(),'last_checked_at':(self.now+timedelta(hours=7,minutes=59)).isoformat()})
        result['home']['score']=home_score;result['away']['score']=away_score;self.store(result);return result
    def test_win_loss_ledger_exact_and_results_immutable(self):
        publish(self.db,self.build(),self.fixture,self.now)
        result=self.result('90','110')
        self.assertEqual(settle(self.db,[result],self.now+timedelta(hours=8)),1)
        self.assertEqual(settle(self.db,[result],self.now+timedelta(hours=8)),0)
        rows=forecast_ledger(self.db);m=evaluation(rows)
        self.assertEqual((m['wins'],m['losses'],m['pending']),(0,1,0))
        self.assertEqual(m['accuracy'],0);self.assertGreater(m['brier'],0);self.assertGreater(m['log_loss'],0)
        self.assertTrue(verify_predictions(self.db));self.assertTrue(verify(self.db))
        for query in ('DELETE FROM outcome_results',"UPDATE outcome_results SET payload='{}'"):
            with self.assertRaises(sqlite3.IntegrityError):self.db.execute(query)
        corrected=self.result('120','90');self.assertEqual(settle(self.db,[corrected],self.now+timedelta(hours=8)),0)
        self.assertEqual(evaluation(forecast_ledger(self.db))['losses'],1)
    def test_no_result_guessing_or_post_start_forecasts(self):
        publish(self.db,self.build(),self.fixture,self.now)
        result=self.result(None,'100');self.assertIsNone(outcome(result))
        self.assertEqual(settle(self.db,[result],self.now+timedelta(hours=8)),0)
        result=self.result('100','90');result['status']='Abandoned'
        self.assertEqual(settle(self.db,[result],self.now+timedelta(hours=8)),0)
        result['status']='Final';result['fresh']=False
        self.assertEqual(settle(self.db,[result],self.now+timedelta(hours=8)),0)
        with self.assertRaises(ValueError):publish(self.db,self.build(),self.fixture,self.now+timedelta(hours=5))
    def test_draw_predictions_and_settlement_are_classes(self):
        fixture=copy.deepcopy(self.fixture);fixture['league']='Premier League'
        self.source='espn:eng.1:events';fixture['source']=self.source
        self.feed=dict(self.db.execute('SELECT * FROM live_feeds WHERE id=?',(self.source,)).fetchone())
        history=[{**e,'league':'Premier League'} for e in self.history]
        for e in history:e['home']={**e['home'],'score':'1'};e['away']={**e['away'],'score':'1'};e['source']=self.source
        self.store(fixture)
        f,reason=build_forecast(fixture,history,self.now)
        self.assertIsNone(reason);self.assertEqual(f['predicted_outcome'],'draw');self.assertGreater(f['probabilities']['draw'],.5)
        self.store(fixture);publish(self.db,f,fixture,self.now)
        result=self.result('1','1');result['league']='Premier League';result['source']=self.source;self.store(result)
        self.assertEqual(settle(self.db,[result],self.now+timedelta(hours=8)),1)
        self.assertEqual(evaluation(forecast_ledger(self.db))['wins'],1)
    def test_corrections_do_not_silently_change_original_predictions(self):
        f=self.build();publish(self.db,f,self.fixture,self.now)
        original=forecast_ledger(self.db)[0]['hash']
        self.history[0]['home']['score']='0'
        refresh_predictions(self.db,self.history+[self.fixture],self.now)
        self.assertEqual(forecast_ledger(self.db)[0]['hash'],original)
    def test_tracker_has_no_fake_historical_wins(self):
        summary=predictions_summary(self.db);self.assertEqual(summary['metrics']['total'],0)
        self.assertIsNone(summary['metrics']['accuracy']);self.assertEqual(summary['forecasts'],[])
        report=refresh_predictions(self.db,self.history+[self.fixture],self.now)
        self.assertEqual(report['published'],1)
        self.assertEqual(predictions_summary(self.db)['metrics']['pending'],1)
    def test_monitoring_uses_scored_samples_and_does_not_rewrite_after_losses(self):
        f=self.build();f['result']={'outcome':'away','correct':False}
        self.assertFalse(monitoring([f]*29)['pause_new_forecasts'])
        self.assertTrue(monitoring([f]*30)['pause_new_forecasts'])
        pending={**f,'result':None}
        self.assertFalse(monitoring([pending]*100)['pause_new_forecasts'])
    def test_result_team_identity_mismatch_cannot_score(self):
        publish(self.db,self.build(),self.fixture,self.now)
        result=self.result('110','90');result['home']['id']='different-team'
        self.assertEqual(settle(self.db,[result],self.now+timedelta(hours=8)),0)
    def test_unsupported_penalty_or_tie_result_remains_pending(self):
        result=self.result('1','1');self.assertIsNone(outcome(result))
        result['league']='Premier League';result['status']='Final - Penalties';self.assertIsNone(outcome(result))
        result['status']='Final';self.assertEqual(outcome(result),'draw')
        result['status']='FT';self.assertEqual(outcome(result),'draw')
        result['status']='FT-Pens';self.assertIsNone(outcome(result))
        result['league']='NHL';result['status']='Final/SO';result['home']['score']='2'
        self.assertEqual(outcome(result),'home')

if __name__=='__main__':unittest.main()

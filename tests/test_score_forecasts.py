import copy
import sqlite3
import unittest
from datetime import timedelta
import test_predictions as fixtures
from server.score_forecasts import initialize,build,publish,refresh,ledger,metrics,summary,value,targets,monitoring,evidence_report

class ScoreForecastTests(unittest.TestCase):
    event=fixtures.PredictionTests.event
    store=fixtures.PredictionTests.store
    result=fixtures.PredictionTests.result
    tearDown=fixtures.PredictionTests.tearDown
    # Reuse fixture helpers, not inherited test cases.
    def setUp(self):
        fixtures.PredictionTests.setUp(self);initialize(self.db)
        self.history=[]
        for i in range(24):
            e=self.event('score-past-'+str(i),self.now-timedelta(days=30-i),True)
            e['home']['score']='130';e['away']['score']='100'
            self.history.append(e)
    def test_score_distributions_fixed_targets_and_no_future_leakage(self):
        records,reason=build(self.fixture,self.history,self.now)
        self.assertIsNone(reason);self.assertEqual(len(records),8)
        for r in records:self.assertAlmostEqual(sum(r['probabilities'].values()),1);self.assertGreater(r['estimated_probability'],.6)
        future=copy.deepcopy(self.history[0]);future['external_id']='future-leak';future['fetched_at']=(self.now+timedelta(seconds=1)).isoformat()
        actual,_=build(self.fixture,self.history+[future],self.now)
        self.assertEqual(records,actual)
    def test_low_sample_and_started_fixtures_rejected(self):
        self.assertFalse(build(self.fixture,self.history[:4],self.now)[0])
        started={**self.fixture,'starts_at':self.now.isoformat()}
        self.assertFalse(build(started,self.history,self.now)[0])
    def test_prospective_idempotent_scoring_and_immutable_records(self):
        records,_=build(self.fixture,self.history,self.now)
        for r in records:self.assertTrue(publish(self.db,r,self.fixture,self.now));self.assertFalse(publish(self.db,r,self.fixture,self.now))
        self.assertEqual(metrics(ledger(self.db))['pending'],8)
        result=self.result('90','100');refresh(self.db,[result],self.now+timedelta(hours=8))
        m=metrics(ledger(self.db));self.assertEqual((m['wins'],m['losses'],m['pending']),(2,6,0))
        refresh(self.db,[result],self.now+timedelta(hours=8));self.assertEqual(len(ledger(self.db)),8)
        self.assertTrue(summary(self.db)['integrity'])
        for table in ('score_forecasts','score_results'):
            with self.assertRaises(sqlite3.IntegrityError):self.db.execute('DELETE FROM '+table)
    def test_soccer_both_scoring_and_threshold_boundaries(self):
        e=copy.deepcopy(self.history[0]);e['home']['score']='1';e['away']['score']='1'
        t=targets('Premier League')
        total=next(x for x in t if x['target_key']=='total')
        self.assertTrue(value(e,t[-1]));self.assertFalse(value(e,total))
        e['home']['score']='0';self.assertFalse(value(e,t[-1]))
        e['home']['score']='2';self.assertTrue(value(e,total))
    def test_changed_feed_prevents_publication(self):
        records,_=build(self.fixture,self.history,self.now)
        self.db.execute("UPDATE live_feeds SET status='unavailable' WHERE id=?",(self.source,));self.db.commit()
        with self.assertRaises(ValueError):publish(self.db,records[0],self.fixture,self.now)
    def test_60_filter_metrics_retain_recorded_losses(self):
        records,_=build(self.fixture,self.history,self.now)
        for r in records:publish(self.db,r,self.fixture,self.now)
        result=self.result('90','100');refresh(self.db,[result],self.now+timedelta(hours=8))
        self.assertEqual(summary(self.db)['above_60_metrics']['losses'],6)
    def test_binary_monitor_requires_distinct_matches(self):
        records,_=build(self.fixture,self.history,self.now);r=records[0];r['result']={'correct':False,'outcome':'no'}
        self.assertFalse(monitoring([r]*30)['total']['pause_new_forecasts'])
        rows=[{**r,'event_key':str(i)} for i in range(30)]
        self.assertTrue(monitoring(rows)['total']['pause_new_forecasts'])


    def test_home_margin_orientation_and_shared_matches_count_once(self):
        records,_=build(self.fixture,self.history,self.now)
        positive=next(r for r in records if r['kind']=='margin' and r['threshold']>0)
        self.assertEqual(positive['unique_team_games'],24)
        self.assertGreater(positive['probabilities']['yes'],.5)
        reversed_history=[]
        for original in self.history:
            e=copy.deepcopy(original)
            e['home'],e['away']=e['away'],e['home']
            reversed_history.append(e)
        actual,_=build(self.fixture,reversed_history,self.now)
        actual_positive=next(r for r in actual if r['kind']=='margin' and r['threshold']>0)
        # Team-oriented evidence remains unchanged; only the home-oriented league prior changes.
        self.assertGreater(actual_positive['probabilities']['yes'],.5)
        self.assertEqual(actual_positive['unique_team_games'],24)

    def test_prior_excludes_shared_team_results_and_reports_uncertainty(self):
        records,_=build(self.fixture,self.history,self.now)
        self.assertTrue(all(r['background_games']==0 for r in records))
        self.assertTrue(all(r['empirical_frequency_interval'] is not None for r in records))
        self.assertTrue(all(r['parameters']['league_prior_excludes_team_games'] for r in records))
    def test_preseason_and_old_history_do_not_make_predictions(self):
        fixture={**self.fixture,'match_type':'Preseason'}
        self.assertFalse(build(fixture,self.history,self.now)[0])
        old=[{**e,'starts_at':(self.now-timedelta(days=100)).isoformat()} for e in self.history]
        self.assertFalse(build(self.fixture,old,self.now)[0])
    def test_thresholds_are_unique_monotone_and_half_points(self):
        for league in ('NBA','NFL','NHL','MLB','Premier League','Champions League'):
            ts=targets(league)
            self.assertEqual(len(ts),len({t['target_key'] for t in ts}))
            self.assertTrue(all(t['threshold']%1==.5 for t in ts if t['threshold'] is not None))
        records,_=build(self.fixture,self.history,self.now)
        totals=sorted([r for r in records if r['kind']=='total'],key=lambda r:r['threshold'])
        self.assertEqual(sorted([r['probabilities']['yes'] for r in totals],reverse=True),[r['probabilities']['yes'] for r in totals])
    def test_evidence_report_separates_model_and_targets(self):
        records,_=build(self.fixture,self.history,self.now)
        rows=[{**r,'result':None} for r in records]
        report=evidence_report(rows)
        self.assertEqual(len(report),8)
        self.assertTrue(all(r['metrics']['accuracy'] is None for r in report))
        self.assertTrue(all(r['distinct_matches']==1 for r in report))

    def test_per_league_review_gate_cannot_be_hidden_by_other_sports(self):
        records,_=build(self.fixture,self.history,self.now);r=records[0]
        bad=[{**r,'event_key':'nba-'+str(i),'result':{'correct':False,'outcome':'no'}} for i in range(30)]
        good=[{**r,'league':'NFL','event_key':'nfl-'+str(i),'result':{'correct':True,'outcome':'yes'}} for i in range(100)]
        report=monitoring(bad+good)
        self.assertTrue(report['NBA:total']['pause_new_forecasts'])
        self.assertFalse(report['NFL:total']['pause_new_forecasts'])
    def test_new_grid_does_not_rewrite_legacy_forecasts(self):
        from server.core import canonical,digest
        records,_=build(self.fixture,self.history,self.now)
        legacy={**next(r for r in records if r['target_key']=='total'),'model_version':'experimental-score-frequency-v1'}
        hash_=digest(legacy)
        self.db.execute('INSERT INTO score_forecasts VALUES(?,?,?,?,?,?)',(hash_[:24],legacy['event_key'],legacy['target_key'],canonical(legacy),hash_,legacy['published_at']));self.db.commit()
        refresh(self.db,self.history+[self.fixture],self.now)
        rows=ledger(self.db)
        self.assertEqual(len(rows),8)
        original=next(r for r in rows if r['target_key']=='total')
        self.assertEqual(original['hash'],hash_);self.assertEqual(original['model_version'],legacy['model_version'])

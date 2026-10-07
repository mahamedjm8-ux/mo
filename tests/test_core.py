import copy
import sqlite3
import tempfile
import unittest
from server.core import connect, initialize, seed_demo, ledger, metrics, verify, record_forecast, record_result, probability, walk_forward

class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(self.tmp.name+'/test.db')
        initialize(self.db)
        seed_demo(self.db)
    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()
    def test_probability(self):
        self.assertAlmostEqual(probability(0,2),.5)
        self.assertGreater(probability(5,0),.5)
    def test_metrics_match_entire_ledger(self):
        rows = ledger(self.db)
        settled = [r for r in rows if r['outcome'] is not None]
        m = metrics(rows)
        self.assertEqual(m['total'],124)
        self.assertEqual(m['settled'],len(settled))
        self.assertAlmostEqual(m['brier'],sum((r['probability']-r['outcome'])**2 for r in settled)/len(settled))
    def test_idempotency(self):
        before = self.db.execute('SELECT COUNT(*) FROM audit').fetchone()[0]
        seed_demo(self.db)
        record_forecast(self.db,{k:v for k,v in ledger(self.db)[0].items() if k not in ('id','hash','outcome','result_available_at')})
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM audit').fetchone()[0],before)
    def test_immutable_predictions_results_and_audit(self):
        for table in ('forecasts','results','audit'):
            with self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(f'DELETE FROM {table}')
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE forecasts SET payload='{}'")
    def test_no_lookahead_or_post_start_publication(self):
        f = copy.deepcopy(ledger(self.db)[0]); f['event_id']='new'
        f['feature_timestamp'] = f['starts_at']
        with self.assertRaises(ValueError): record_forecast(self.db,f)
        f['feature_timestamp']=f['published_at']; f['published_at']=f['starts_at']
        with self.assertRaises(ValueError): record_forecast(self.db,f)
    def test_unknown_source_and_premature_results_rejected(self):
        f = ledger(self.db)[0]
        with self.assertRaises(ValueError): record_result(self.db,f['event_id'],1,'unknown',f['starts_at'])
        with self.assertRaises(ValueError): record_result(self.db,f['event_id'],1,'synthetic-v1',f['published_at'])
    def test_audit_chain(self):
        self.assertTrue(verify(self.db))
        self.db.execute('DROP TRIGGER forecast_no_update')
        self.db.execute("UPDATE forecasts SET payload='{}' WHERE id=(SELECT id FROM forecasts LIMIT 1)")
        self.assertFalse(verify(self.db))
    def test_empty_metrics(self):
        self.assertIsNone(metrics([])['brier'])
        self.assertEqual(metrics([])['settled'],0)
    def test_walk_forward_uses_only_available_training_results(self):
        rows = ledger(self.db)
        folds = walk_forward(rows)
        self.assertGreater(len(folds),0)
        for fold in folds:
            self.assertLess(fold['latest_training_result'][:10],fold['test_from'])
            self.assertGreaterEqual(fold['train_count'],40)
        for row in rows:
            row['result_available_at']='2099-01-01T00:00:00+00:00'
        self.assertEqual(walk_forward(rows),[])

if __name__ == '__main__': unittest.main()

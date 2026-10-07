import tempfile
import unittest
from server.providers import normalize_fotmob, import_events
from server.core import connect, initialize, verify

class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.document={'leagues':[{'name':'Premier League','matches':[{'id':123,'home':{'name':'Arsenal'},'away':{'name':'Chelsea'},'status':{'utcTime':'2026-10-10T15:00:00Z','finished':False}}]}]}
    def test_normalizes_fixtures_without_inventing_statistics(self):
        events=normalize_fotmob(self.document)
        self.assertEqual(events[0]['home'],'Arsenal')
        self.assertEqual(events[0]['id'],'fotmob:123')
        self.assertNotIn('probability',events[0])
        self.assertNotIn('ratings',events[0])
    def test_missing_timestamps_rejected(self):
        del self.document['leagues'][0]['matches'][0]['status']['utcTime']
        with self.assertRaises(ValueError): normalize_fotmob(self.document)
    def test_import_idempotency_and_mode_separation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=connect(tmp+'/test.db'); initialize(db)
            self.assertEqual(import_events(db,self.document),1)
            self.assertEqual(import_events(db,self.document),0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM forecasts').fetchone()[0],0)
            self.assertTrue(verify(db))
            db.close()

if __name__=='__main__': unittest.main()

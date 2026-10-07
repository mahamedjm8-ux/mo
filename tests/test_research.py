import tempfile
import unittest
from server.core import connect,initialize
from server.live import initialize_live
from server.research import read_page,collect_source,research_summary

class ResearchTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.db=connect(self.tmp.name+'/db');initialize(self.db);initialize_live(self.db)
  self.source=dict(self.db.execute("SELECT * FROM research_sources WHERE name='FanGraphs'").fetchone())
 def tearDown(self):self.db.close();self.tmp.cleanup()
 def fetcher(self,url):
  if url.endswith('/robots.txt'):return 'User-agent: *\nAllow: /'
  return '<html><title>Test baseball statistics</title><h2>Team results</h2><table><tr><th>Player</th><td>Example</td><td>10</td></tr></table><h2>Best betting picks</h2><script>invented hidden data</script></html>'
 def test_only_public_static_evidence_retained(self):
  e=read_page(self.source,self.fetcher)
  self.assertEqual(e['table_count'],1);self.assertEqual(e['table_samples'][0],['Player','Example','10'])
  self.assertEqual(e['headings'],['Team results']);self.assertIsNone(e['reported_update_text'])
 def test_robots_disallow_respected(self):
  def denied(url):return 'User-agent: *\nDisallow: /'
  self.assertFalse(collect_source(self.db,self.source,denied))
  s=next(s for s in research_summary(self.db)['sources'] if s['id']==self.source['id'])
  self.assertEqual(s['status'],'unavailable');self.assertIsNone(s['evidence'])
 def test_library_does_not_claim_feature_integration(self):
  self.assertTrue(collect_source(self.db,self.source,self.fetcher));self.assertTrue(collect_source(self.db,self.source,self.fetcher))
  self.assertEqual(self.db.execute('SELECT COUNT(*) FROM research_evidence').fetchone()[0],1)
  s=next(s for s in research_summary(self.db)['sources'] if s['id']==self.source['id'])
  self.assertFalse(s['used_in_forecasts']);self.assertEqual(s['evidence']['table_count'],1)
  self.assertEqual(len(research_summary(self.db)['sources']),21)

if __name__=='__main__':unittest.main()

import sys,unittest,copy
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from quota_sentinel.quota.pi_live import normalize_pi_live
from quota_sentinel.quota.models import QuotaNormalizationError
NOW=1790960000
CODEX={'rate_limit':{'primary_window':{'used_percent':19,'reset_at':NOW+18000,'limit_window_seconds':18000},'secondary_window':{'used_percent':8,'reset_at':NOW+604800,'limit_window_seconds':604800}}}
class LiveTests(unittest.TestCase):
 def test_live_windows(self):
  q=normalize_pi_live('codex',CODEX,captured_at=NOW);self.assertTrue(q.fresh);self.assertFalse(q.cached);self.assertEqual(q.five_hour.remaining_percent,81);self.assertEqual(q.source,'Pi · live')
 def test_bad_usage_is_not_zero(self):
  for value in (None,True,-1,101,float('nan')):
   d=copy.deepcopy(CODEX);d['rate_limit']['primary_window']['used_percent']=value
   with self.subTest(value=value),self.assertRaises(QuotaNormalizationError):normalize_pi_live('codex',d,captured_at=NOW)
 def test_old_snapshot_cannot_be_live(self):
  with self.assertRaises(QuotaNormalizationError):normalize_pi_live('codex',{'fiveHour':{'remainingPercent':80,'resetAt':NOW+18000},'weekly':{'remainingPercent':90,'resetAt':NOW+604800}},captured_at=NOW)
 def test_implausible_resets_and_window_sizes(self):
  for field,value in (('reset_at',0),('reset_at',NOW+10**9),('reset_at',True),('limit_window_seconds',300)):
   d=copy.deepcopy(CODEX);d['rate_limit']['primary_window'][field]=value
   with self.assertRaises(QuotaNormalizationError):normalize_pi_live('codex',d,captured_at=NOW)
 def test_opencode_monthly_display(self):
  d={'usage':{'rolling':{'percent':20,'resetsAt':'2026-10-03T05:00:00Z'},'weekly':{'percent':10,'resetsAt':'2026-10-09T05:00:00Z'},'monthly':{'percent':5,'resetsAt':'2026-11-01T00:00:00Z'}}}
  q=normalize_pi_live('opencode',d,captured_at=NOW);self.assertEqual(q.monthly.remaining_percent,95)
 def groups(self):
  return {'groups':[{'displayName':'Gemini','buckets':[{'bucketId':'gemini-5h','window':'5h','remainingFraction':.8,'resetTime':'2026-10-03T05:00:00Z'},{'bucketId':'gemini-week','window':'weekly','remainingFraction':.9,'resetTime':'2026-10-09T05:00:00Z'}]}]}
 def test_antigravity_groups(self):
  self.assertEqual(normalize_pi_live('antigravity',self.groups(),captured_at=NOW).five_hour.remaining_percent,80)
 def test_unknown_ambiguous_or_missing_groups(self):
  for change in ('unknown','duplicate','missing'):
   d=self.groups();b=d['groups'][0]['buckets']
   if change=='unknown':d['groups'][0]['displayName']='unknown'
   elif change=='duplicate':b.append(copy.deepcopy(b[0]))
   else:b[0].pop('remainingFraction')
   with self.assertRaises(QuotaNormalizationError):normalize_pi_live('antigravity',d,captured_at=NOW)
if __name__=='__main__':unittest.main()

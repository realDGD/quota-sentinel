"""Installed helpers, portable client prefixes and bounded interactive pipes."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from dataclasses import replace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

class Boundary(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
 def test_helpers_without_repo_files(self):
  self.assertIsNotNone(importlib.util.find_spec('quota_sentinel.helpers'),'installed helper resources missing')
  from quota_sentinel.helpers import resource_path
  for name in ('run_with_timeout.py','opencode_usage.py','antigravity_usage.py','clinepass_usage.py','pi_quota_query.mjs','pi_quota/auth.mjs','capture-codex-quota.ts'):
   path=resource_path(name);self.assertTrue(path.is_file(),name);self.assertIn('quota_sentinel',path.parts)
  with self.assertRaises(ValueError):resource_path('../config.json')
 def test_native_codex_interactive_rpc_and_selected_home(self):
  from quota_sentinel.runtime.quota_probe import QuotaCollector
  cli=self.root/'official codex.py';home=self.root/'native home';home.mkdir();record=self.root/'home.txt'
  cli.write_text('import json,os,sys\nfrom pathlib import Path\nPath('+repr(str(record))+').write_text(os.environ.get("CODEX_HOME",""))\nfor line in sys.stdin:\n q=json.loads(line)\n r={"rateLimits":{"primary":{"usedPercent":5,"windowDurationMins":300,"resetsAt":1800000000},"secondary":{"usedPercent":10,"windowDurationMins":10080,"resetsAt":1800600000}}}\n print(json.dumps({"id":q["id"],"result":r if q["method"]=="account/rateLimits/read" else {}}),flush=True)\n')
  collector=QuotaCollector(self.root/'state',self.root/'work',providers=('codex',),tier_chains={'codex':('native',)},codex_bin=cli,environment={'CODEX_HOME':str(home)})
  reading=collector.collect()['codex'];self.assertIsNotNone(reading.quota,reading.error)
  self.assertEqual(record.read_text(),str(home));self.assertFalse((home/'auth.json').exists())
 def test_pi_plugin_preflight_uses_private_agent_directory(self):
  from quota_sentinel.runtime.pi_plugins import check_pi_plugin
  from quota_sentinel.runtime.models import ModelRunnerConfig
  cli=self.root/'pi.py';record=self.root/'environment.json'
  cli.write_text('import os,json\nfrom pathlib import Path\nPath('+repr(str(record))+').write_text(json.dumps({"agent":os.environ.get("PI_CODING_AGENT_DIR"),"wrong":os.environ.get("PI_AGENT_DIR")}))\nprint("clinepass  cline-pass/deepseek-v4.1-flash")\n')
  pkg=self.root/'plugin';pkg.mkdir();(pkg/'package.json').write_text(json.dumps({'name':'pi-clinepass-provider','pi':{'extensions':['entry.ts']}}));(pkg/'entry.ts').write_text('fixture')
  auth=self.root/'auth.json';auth.write_text('{}')
  config=ModelRunnerConfig(cli,auth,self.root,self.root/'unused',plugin_entries={'clinepass':pkg/'entry.ts'})
  check=check_pi_plugin('clinepass',config);self.assertTrue(check.available,check.reason)
  data=json.loads(record.read_text());self.assertTrue(data['agent']);self.assertNotEqual(data['agent'],str(auth.parent));self.assertFalse(Path(data['agent']).exists())
 def test_cleanup_bounds_recomputed(self):
  from quota_sentinel.config import new_user_defaults
  from quota_sentinel.runtime.budgets import _prepare
  from quota_sentinel.platform.process import CLEANUP_ALLOWANCE_SECONDS
  config=new_user_defaults()
  self.assertGreaterEqual(_prepare(config,'opencode','direct'),config.budgets['credentials']['timeout']+CLEANUP_ALLOWANCE_SECONDS)

 def test_each_runner_accepts_portable_python_client(self):
  from quota_sentinel.runtime.models import ModelRunner,ModelRunnerConfig
  from quota_sentinel.runtime.codex_exec import CodexExecRunner,CodexExecConfig
  from quota_sentinel.runtime.agy_exec import AgyExecRunner,AgyExecConfig
  from quota_sentinel.runtime.direct import DirectRunner
  cli=self.root/'selected client.py'
  cli.write_text('import json,sys\na=sys.argv[1:]\nif "--version" in a:print("1.2.12")\nelif "print-bearer-token" in a:print("synthetic-bearer")\nelif "--config" in a:\n sys.stdin.read();print(json.dumps({"choices":[{"message":{"content":"1"}}],"usage":{"total_tokens":2}}));print("QSHTTPSTATUS:200")\nelif "exec" in a:\n print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"1"}}));print(json.dumps({"type":"turn.completed","usage":{"input_tokens":2,"output_tokens":1}}))\nelif "--output-format" in a:print(json.dumps({"status":"SUCCESS","response":"1","usage":{"input_tokens":564,"output_tokens":1,"total_tokens":565,"thinking_tokens":0}}))\nelse:print("1")\n')
  auth=self.root/'auth.json';auth.write_text('{"openai-codex":{"type":"oauth","access":"synthetic-only"}}')
  configurations=(
   ('codex',ModelRunner(ModelRunnerConfig(cli,auth,self.root,self.root/'unused',timeout=2,kill_grace=0,auth_timeout=1,capture_providers=frozenset()))),
   ('codex',CodexExecRunner(CodexExecConfig(codex_bin=cli,codex_home=self.root,state_dir=self.root/'codex-state',timeout=2,kill_grace=0))),
   ('antigravity',AgyExecRunner(AgyExecConfig(agy_bin=cli,state_dir=self.root/'agy-state',timeout=2,kill_grace=0,preflight=False))),
   ('opencode',DirectRunner(curl_bin=cli,timeout=2,key_reader=lambda _: 'synthetic-only',capture_providers=frozenset())),
  )
  for index,(provider,runner) in enumerate(configurations):
   with self.subTest(transport=type(runner).__name__):
    result=runner.run(provider,self.root/str(index),'initial',1,1)
    self.assertTrue(result.success,result.error_summary)

 def test_agy_usage_uses_portable_pipe_without_uv(self):
  from quota_sentinel.runtime.quota_probe import QuotaCollector
  report={'status':'SUCCESS','num_turns':0,'usage':{'input_tokens':0,'output_tokens':0,'total_tokens':0},'command':{'name':'usage','data':{'groups':[{'name':'Gemini','buckets':[{'window':w,'remaining_fraction':.8,'reset_time':t} for w,t in [('5h','2027-01-01T00:00:00Z'),('weekly','2027-01-07T00:00:00Z')]]}]}}}
  cli=self.root/'agy.py';cli.write_text('import sys\nprint("1.2.12" if "--version" in sys.argv else '+repr(json.dumps(report))+')\n')
  collector=QuotaCollector(self.root/'state',self.root/'work',providers=('antigravity',),tier_chains={'antigravity':('native',)},agy_bin=cli,uv_bin=self.root/'missing-uv')
  reading=collector.collect()['antigravity'];self.assertIsNotNone(reading.quota,reading.error)

if __name__=='__main__':unittest.main()

"""Apply only saved nonsecret coordinates inside the Windows service process."""
import json
import os
from pathlib import Path
import sys
from .services import SAFE_ENVIRONMENT

def main(argv=None):
 paths=sys.argv[1:] if argv is None else argv
 if len(paths)!=1:raise ValueError('one service manifest is required')
 path=Path(paths[0])
 if path.stat().st_size>65536:raise ValueError('service manifest too large')
 document=json.loads(path.read_text(encoding='utf-8'))
 env=document.get('environment');args=document.get('arguments')
 if not isinstance(env,dict) or any(k not in SAFE_ENVIRONMENT or not isinstance(v,str) or '\x00' in v for k,v in env.items()):raise ValueError('invalid service environment')
 if not isinstance(args,list) or not args or args[-1]!='serve' or any(not isinstance(v,str) or '\x00' in v for v in args):raise ValueError('invalid service arguments')
 os.environ.update(env)
 from quota_sentinel.__main__ import main as cli
 from .files import private_directory,private_open
 from contextlib import redirect_stdout,redirect_stderr
 import io
 logs=private_directory(Path.cwd()/'logs')
 with io.TextIOWrapper(private_open(logs/'service.out.log','ab'),encoding='utf-8',write_through=True) as output,io.TextIOWrapper(private_open(logs/'service.err.log','ab'),encoding='utf-8',write_through=True) as error:
  with redirect_stdout(output),redirect_stderr(error):return cli(args)
if __name__=='__main__':
 try:raise SystemExit(main())
 except (OSError,ValueError):print('quota-sentinel: invalid service manifest',file=sys.stderr);raise SystemExit(3)

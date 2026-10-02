"""Synthetic process tree: no credentials, network or provider clients."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

mode=sys.argv[1]
if mode=='argv':
 print(json.dumps(sys.argv[2:],ensure_ascii=False));sys.exit(0)
if mode=='flood':
 while True:os.write(1,b'x'*65536)
if mode=='stdin-stall':
 time.sleep(60);sys.exit(0)
if mode=='leaf':
 if os.name!='nt':signal.signal(signal.SIGTERM,signal.SIG_IGN)
 Path(sys.argv[2]).write_text(str(os.getpid()))
 time.sleep(60);sys.exit(0)
if mode in ('tree','leader-exit','detached'):
 if os.name!='nt':signal.signal(signal.SIGTERM,signal.SIG_IGN)
 child=subprocess.Popen([sys.executable,__file__,'leaf',sys.argv[2]],
   start_new_session=mode=='detached' and os.name!='nt')
 deadline=time.monotonic()+3
 while not Path(sys.argv[2]).exists() and time.monotonic()<deadline:time.sleep(.005)
 if mode=='leader-exit':sys.exit(0)
 if mode=='detached':time.sleep(.3);sys.exit(0)
 time.sleep(60)

"""Private publication never exposes partial bytes or weak permissions."""
import importlib.util
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

class Files(unittest.TestCase):
 def setUp(self):
  self.assertIsNotNone(importlib.util.find_spec('quota_sentinel.platform'),'platform boundary missing')
  from quota_sentinel.platform import files
  self.files=files
 def test_private_permissions(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'private'
   with self.files.private_open(p,'wb') as f:f.write(b'secret')
   self.assertEqual(p.read_bytes(),b'secret')
   if os.name!='nt':self.assertEqual(stat.S_IMODE(p.stat().st_mode),0o600)
   self.files.publish_private(p,b'next')
   if os.name!='nt':self.assertEqual(stat.S_IMODE(p.stat().st_mode),0o600)
 def test_failed_publish_keeps_prior_state(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'state';p.write_bytes(b'prior')
   with patch('os.replace',side_effect=OSError('synthetic failure')):
    with self.assertRaises(OSError):self.files.publish_private(p,b'new')
   self.assertEqual(p.read_bytes(),b'prior');self.assertEqual(list(p.parent.iterdir()),[p])
 def test_reader_observes_complete_file(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'state';first=b'a'*65536;second=b'b'*32768
   self.files.publish_private(p,first);stop=threading.Event();observed=[]
   def reader():
    while not stop.is_set():observed.append(p.read_bytes())
   thread=threading.Thread(target=reader);thread.start()
   try:
    for _ in range(12):self.files.publish_private(p,second);self.files.publish_private(p,first)
   finally:stop.set();thread.join(2)
   self.assertTrue(observed);self.assertTrue(all(x in (first,second) for x in observed))
 @unittest.skipIf(os.name=='nt','POSIX symlink fixture')
 def test_symlink_not_followed(self):
  with tempfile.TemporaryDirectory() as tmp:
   target=Path(tmp)/'target';target.write_bytes(b'prior');link=Path(tmp)/'link';link.symlink_to(target)
   with self.assertRaises(OSError):self.files.private_open(link,'wb')
   self.assertEqual(target.read_bytes(),b'prior')

if __name__=='__main__':unittest.main()

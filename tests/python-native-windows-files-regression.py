"""Native restricted directory inheritance, including SQLite WAL sidecars."""
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
if os.name!='nt':
 print('UNVERIFIED: native Windows file permissions require Windows');raise SystemExit(77)
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from quota_sentinel.platform.files import private_directory, private_open
from quota_sentinel.platform.windows_files import verify_private_handle

class NativeFiles(unittest.TestCase):
 def test_private_directory_children_and_database(self):
  with tempfile.TemporaryDirectory() as tmp:
   path=private_directory(Path(tmp)/'历史 space &')
   child=path/'ordinary';child.write_bytes(b'synthetic')
   with child.open('rb') as handle:verify_private_handle(handle.fileno())
   db=path/'history.sqlite3'
   with private_open(db,'ab'):pass
   connection=sqlite3.connect(db)
   try:
    connection.execute('PRAGMA journal_mode=WAL');connection.execute('CREATE TABLE example(value)');connection.commit()
    for entry in (db,Path(str(db)+'-wal'),Path(str(db)+'-shm')):
     self.assertTrue(entry.exists())
     with entry.open('rb') as handle:verify_private_handle(handle.fileno())
   finally:connection.close()
if __name__=='__main__':unittest.main()

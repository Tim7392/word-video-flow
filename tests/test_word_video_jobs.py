import json
from pathlib import Path
import tempfile
import unittest

from word_video.jobs import submit,status,control,job_lock,connect


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.db=self.root/'queue.sqlite3'
        self.background=self.root/'background.mp4';self.background.touch()
        self.request={'idempotency_key':'中文测试','output':str(self.root/'output'),
            'source':{'text':'apple [ˈæpəl] n. 苹果'},
            'lesson':{'background':str(self.background)},
            'provider':{'kind':'local','items':[]}}

    def test_idempotency_conflict_and_controls(self):
        first=submit(self.db,self.request)
        self.assertEqual(first['id'],submit(self.db,self.request)['id'])
        self.assertEqual('queued',first['state'])
        self.assertEqual('pause',control(self.db,first['id'],'pause')['control'])
        self.assertEqual('run',control(self.db,first['id'],'run')['control'])
        self.request['lesson']['speed']=1.5
        with self.assertRaisesRegex(ValueError,'IDEMPOTENCY_CONFLICT'):submit(self.db,self.request)

    def test_running_process_lock_and_interruption(self):
        job=submit(self.db,self.request)['id']
        with connect(self.db) as con: con.execute('UPDATE jobs SET state=? WHERE id=?',('running',job))
        with job_lock(self.db,job):
            self.assertEqual('running',status(self.db,job)['state'])
            with self.assertRaises(RuntimeError):
                with job_lock(self.db,job):pass
        self.assertEqual('interrupted',status(self.db,job)['state'])

    def test_no_secret_fields(self):
        self.request['provider']['api_key']='do-not-store'
        with self.assertRaises(ValueError): submit(self.db,self.request)

if __name__=='__main__':unittest.main()

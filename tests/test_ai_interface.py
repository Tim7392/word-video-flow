"""Behavioral acceptance of the local AI interface and repeatable publication."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import subtitle_factory_api as api
import subtitle_factory_core as core

TEXT = 'apple [ˈæpəl] n. 苹果\nbanana [bəˈnɑːnə] n. 香蕉\npear n. 梨'


class AIInterfaceTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix='subtitle_ai_')
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.output = self.root/'中文 输出'

    def request(self, action='generate', **extra):
        return {'action': action, 'source': {'text': TEXT}, 'output': str(self.output), **extra}

    def cli(self, args, raw=None, no_site=False):
        env = dict(os.environ)
        env['PYTHONUTF8'] = '1'
        command = [sys.executable]+(['-S'] if no_site else [])+[str(ROOT/'subtitle_factory_cli.py')]+args
        run = subprocess.run(command, cwd=self.root, input=raw, capture_output=True, timeout=20, env=env)
        response = json.loads(run.stdout.decode('utf-8'))
        return run, response

    def test_cli_text_plan_runs_without_site_packages_or_qt(self):
        run, response = self.cli(['plan', '--stdin'], TEXT.encode('utf-8'), no_site=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue(response['ok'])
        self.assertEqual(response['result']['file_count'], 5)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_json_file_invocation_is_cwd_independent_and_unicode_safe(self):
        path = self.root/'请求 文件.json'
        path.write_text(json.dumps(self.request(idempotency_key='lesson 甲'), ensure_ascii=False), encoding='utf-8-sig')
        run, response = self.cli(['request', '--json-file', str(path)])
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(response['result']['file_count'], 5)
        self.assertTrue(all(Path(item['path']).is_absolute() for item in response['result']['files']))
        self.assertTrue(all(Path(item['path']).is_file() for item in response['result']['files']))

    def test_machine_error_envelopes_and_exit_codes(self):
        cases = [(['generate'], None, 2, 'INVALID_REQUEST'),
                 (['request'], b'{broken', 2, 'INVALID_REQUEST'),
                 (['request'], b'{"action":"doctor","action":"schema"}', 2, 'INVALID_REQUEST'),
                 (['inspect', '--input', str(self.root/'absent.txt')], None, 3, 'INPUT_ERROR'),
                 (['generate', '--text', 'apple n. 苹果', '--output', str(self.output), '--batch-size', '0'], None, 2, 'INVALID_REQUEST')]
        for args, raw, code, error in cases:
            with self.subTest(args=args):
                run, response = self.cli(args, raw)
                self.assertEqual(run.returncode, code)
                self.assertFalse(response['ok'])
                self.assertEqual(response['error']['code'], error)
        self.assertFalse(self.output.exists())

    def test_plan_uses_real_timing_and_does_not_create_destination(self):
        result = api.execute(self.request('plan', batch_size=2))
        self.assertEqual(result['package_count'], 2)
        self.assertEqual(result['file_count'], 10)
        self.assertEqual([p['duration_ms'] for p in result['packages']], [5600, 2500])
        self.assertFalse(self.output.exists())
        actual = api.execute(self.request(batch_size=2))
        self.assertEqual([(i['relative_path'], i['sha256']) for i in result['files']],
                         [(i['relative_path'], i['sha256']) for i in actual['files']])

    def test_files_equal_pre_refactor_frozen_oracle(self):
        baseline = ROOT/'ui_redesign_20260912_151831/baseline/Words_SRT.py'
        manifest = json.loads((baseline.parent.parent/'baseline_contract.json').read_text(encoding='utf-8-sig'))
        self.assertEqual(hashlib.sha256(baseline.read_bytes()).hexdigest(), manifest['source_sha256'])
        # Run the old oracle separately so its GUI imports/log handler cannot
        # affect the headless API or the GUI suite in this process.
        oracle = self.root/'oracle'
        script = '''import importlib.util,sys
spec=importlib.util.spec_from_file_location('oracle',sys.argv[1]);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
data,errors=m.LogicCore().parse(txt=sys.argv[3]);assert not errors
m._generate_packages(m.LogicCore(),data,sys.argv[2],.25,.8,2)
'''
        env = dict(os.environ, QT_QPA_PLATFORM='offscreen', LOCALAPPDATA=str(self.root/'logs'))
        run = subprocess.run([sys.executable, '-c', script, str(baseline), str(oracle), TEXT],
                             capture_output=True, env=env, timeout=20)
        self.assertEqual(run.returncode, 0, run.stderr)
        result = api.execute(self.request(batch_size=2, timing={'first_six': .25, 'extra': .8}))
        self.assertEqual({p.name:p.read_bytes() for p in oracle.glob('*.srt')},
                         {Path(i['path']).name:Path(i['path']).read_bytes() for i in result['files']})

    def test_inspect_pagination_and_first_matching_word_range(self):
        result = api.execute({'action': 'inspect', 'source': {'text': TEXT+'\napple n. 苹果'},
                              'range': {'start_word': 'APPLE', 'end_word': 'pear'}, 'offset': 1, 'limit': 1})
        self.assertEqual(result['selection'], {'start': 1, 'end': 3})
        self.assertEqual(result['entries'][0]['word'], 'banana')
        self.assertEqual(result['entries'][0]['index'], 2)
        self.assertEqual(result['next_offset'], 2)

    def test_txt_encodings_docx_table_and_vocabulary_srt(self):
        sources=[]
        for encoding in ('utf-8-sig', 'utf-16', 'gb18030'):
            path=self.root/(encoding+'.txt');path.write_bytes(TEXT.encode(encoding));sources.append(path)
        path=self.root/'input.srt'
        path.write_text('1\n00:00:09,000 --> 00:00:11,000\napple\n[ˈæpəl]\nn. 苹果\n', encoding='utf-8')
        sources.append(path)
        from docx import Document
        document=Document();table=document.add_table(rows=1,cols=3)
        for cell,text in zip(table.rows[0].cells,('apple','[ˈæpəl]','n. 苹果')):cell.text=text
        path=self.root/'词表.docx';document.save(path);sources.append(path)
        for path in sources:
            with self.subTest(path=path.name):
                result=api.execute({'action':'inspect','source':{'path':str(path)}})
                self.assertEqual(result['entries'][0]['word'],'apple')
                self.assertEqual(result['entries'][0]['phonetic'],'[ˈæpəl]')

    def test_mixed_invalid_input_is_not_silently_skipped(self):
        with self.assertRaises(api.FactoryError) as failure:
            api.execute(self.request(source={'text': 'apple n. 苹果\n纯中文非法行'}))
        self.assertEqual(failure.exception.code, 'INPUT_ERROR')
        self.assertFalse(self.output.exists())

    def test_key_retry_across_processes_verifies_and_reuses_files(self):
        request=self.request(idempotency_key='unit-1',batch_size=2)
        raw=json.dumps(request,ensure_ascii=False).encode('utf-8')
        first_run,first=self.cli(['request'],raw)
        second_run,second=self.cli(['request'],raw)
        self.assertEqual((first_run.returncode,second_run.returncode),(0,0))
        self.assertFalse(first['result']['reused']);self.assertTrue(second['result']['reused'])
        self.assertEqual(first['result']['directory'],second['result']['directory'])
        self.assertEqual(len(list(self.output.rglob('*.srt'))),10)
        status=api.execute({'action':'status','output':str(self.output),'idempotency_key':'unit-1'})
        self.assertEqual(status['state'],'complete')

    def test_different_content_or_modified_output_is_not_overwritten(self):
        request=self.request(idempotency_key='unit-2')
        result=api.execute(request)
        with self.assertRaises(api.FactoryError) as conflict:
            api.execute({**request,'timing':{'first_six':1}})
        self.assertEqual(conflict.exception.code,'IDEMPOTENCY_CONFLICT')
        path=Path(result['files'][0]['path']);path.write_bytes(b'user edited this')
        with self.assertRaises(api.FactoryError) as changed:api.execute(request)
        self.assertEqual(changed.exception.code,'OUTPUT_CHANGED')
        self.assertEqual(path.read_bytes(),b'user edited this')

    def test_same_key_concurrency_publishes_one_run(self):
        request=self.request(idempotency_key='concurrent')
        def attempt():
            try:return api.execute(request)
            except api.FactoryError as exc:return exc.code
        with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(lambda _:attempt(),range(4)))
        self.assertTrue(any(isinstance(result,dict) for result in results))
        self.assertTrue(all(isinstance(result,dict) or result=='INCOMPLETE_RUN' for result in results))
        self.assertEqual(len(list(self.output.rglob('*.srt'))),5)
        self.assertTrue(api.execute(request)['reused'])

    def test_incomplete_run_and_manifest_path_escape_fail_closed(self):
        key='unfinished';directory=api.keyed_directory(self.output,key);directory.mkdir(parents=True)
        with self.assertRaises(api.FactoryError) as failure:api.execute(self.request(idempotency_key=key))
        self.assertEqual(failure.exception.code,'INCOMPLETE_RUN')
        result=api.execute(self.request(idempotency_key='complete'))
        record=Path(result['run_directory'])/'result.json'
        contents=json.loads(record.read_text(encoding='utf-8'))
        outside=self.root/'outside.srt';outside.write_bytes(b'outside')
        contents['files'][0]['relative_path']=str(outside)
        contents['files'][0]['sha256']=api.sha(outside)
        record.write_text(json.dumps(contents),encoding='utf-8')
        with self.assertRaises(api.FactoryError) as failure:api.execute(self.request(idempotency_key='complete'))
        self.assertEqual(failure.exception.code,'OUTPUT_CHANGED')
        self.assertEqual(outside.read_bytes(),b'outside')

    def test_failure_before_publication_allows_clean_retry(self):
        request=self.request(idempotency_key='retry-failure')
        with mock.patch.object(api,'_generate_packages',side_effect=OSError('simulated disk error')):
            with self.assertRaises(api.FactoryError):api.execute(request)
        self.assertFalse(api.keyed_directory(self.output,'retry-failure').exists())
        self.assertEqual(api.execute(request)['file_count'],5)

    def test_legacy_no_key_conflicts_keep_user_files(self):
        first=api.execute(self.request())
        original={Path(i['path']):Path(i['path']).read_bytes() for i in first['files']}
        second=api.execute(self.request())
        self.assertNotEqual(first['directory'],second['directory'])
        self.assertTrue(all(path.read_bytes()==data for path,data in original.items()))

    def test_unknown_fields_bool_numbers_and_bad_ranges_are_rejected(self):
        requests=[self.request(batch_size=True),self.request(batch_size=-1),
                  self.request(timing={'first_six':float('inf')}), self.request(timing={'extra':False}),
                  self.request(range={'start':0}),self.request(range={'start':3,'end':1}),
                  self.request(range={'start':1,'start_word':'apple'}),self.request(overwrite=True),
                  {'action':'status','output':str(self.output),'idempotency_key':'x'*257},
                  {'action':'doctor','output':str(self.output)}]
        for request in requests:
            with self.subTest(request=request),self.assertRaises(api.FactoryError) as failure:
                api.execute(request)
            self.assertEqual(failure.exception.code,'INVALID_REQUEST')
        self.assertFalse(self.output.exists())


if __name__=='__main__':unittest.main()

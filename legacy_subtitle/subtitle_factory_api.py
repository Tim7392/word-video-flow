"""Local, GUI-independent API used by the AI command line.

execute(request) returns JSON-compatible data or raises FactoryError. No GUI,
network listener, persistent application log, or desktop automation is needed.
"""
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile

from subtitle_factory_core import LogicCore, _read_source, _generate_packages, _suffixes

API_VERSION = '1.0'
EXIT_CODES = {'INVALID_REQUEST': 2, 'INPUT_ERROR': 3, 'OUTPUT_ERROR': 4,
              'IDEMPOTENCY_CONFLICT': 5, 'INCOMPLETE_RUN': 5,
              'OUTPUT_CHANGED': 5, 'INTERNAL_ERROR': 1, 'INTERRUPTED': 130}
ACTION_FIELDS = {
    'schema': {'action'}, 'doctor': {'action'},
    'status': {'action', 'output', 'idempotency_key'},
    'inspect': {'action', 'source', 'range', 'offset', 'limit'},
    'plan': {'action', 'source', 'output', 'range', 'timing', 'batch_size', 'idempotency_key'},
    'generate': {'action', 'source', 'output', 'range', 'timing', 'batch_size', 'idempotency_key'}}


class FactoryError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code, self.details = code, details
        self.exit_code = EXIT_CODES[code]


def invalid(message):
    raise FactoryError('INVALID_REQUEST', message)


def object_fields(value, allowed, name):
    if not isinstance(value, dict):
        invalid(name+' 必须是 JSON 对象。')
    unknown = set(value)-set(allowed)
    if unknown:
        invalid(name+' 含未知字段：'+', '.join(sorted(unknown)))


def integer(value, name, minimum=1):
    if type(value) is not int or value < minimum:
        invalid(f'{name} 必须是大于等于 {minimum} 的整数。')
    return value


def string(value, name):
    if not isinstance(value, str) or not value.strip():
        invalid(name+' 必须是非空字符串。')
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def request_schema():
    description = {
        'api_version': API_VERSION,
        'purpose': '英语词表转换为教学用五类 SRT；不执行语音识别、翻译或保留输入 SRT 的原时间轴。',
        'commands': ['schema', 'doctor', 'inspect', 'plan', 'generate', 'status', 'request'],
        'exit_codes': EXIT_CODES,
        'request_schema': {
            '$schema': 'https://json-schema.org/draft/2020-12/schema',
            'type': 'object', 'additionalProperties': False, 'required': ['action'],
            'properties': {
                'action': {'enum': ['inspect', 'plan', 'generate', 'status', 'doctor', 'schema']},
                'source': {'type': 'object', 'additionalProperties': False,
                           'properties': {'path': {'type': 'string', 'minLength': 1}, 'text': {'type': 'string'}},
                           'oneOf': [{'required': ['path']}, {'required': ['text']}]},
                'output': {'type': 'string', 'minLength': 1},
                'range': {'type': 'object', 'additionalProperties': False, 'properties': {
                    'start': {'type': 'integer', 'minimum': 1}, 'end': {'type': 'integer', 'minimum': 1},
                    'start_word': {'type': 'string', 'minLength': 1}, 'end_word': {'type': 'string', 'minLength': 1}},
                    'allOf': [{'not': {'required': ['start', 'start_word']}},
                              {'not': {'required': ['end', 'end_word']}}]},
                'timing': {'type': 'object', 'additionalProperties': False, 'properties': {
                    'first_six': {'type': 'number', 'exclusiveMinimum': 0, 'default': .4},
                    'extra': {'type': 'number', 'exclusiveMinimum': 0, 'default': .2}}},
                'batch_size': {'type': 'integer', 'minimum': 1},
                'idempotency_key': {'type': 'string', 'minLength': 1, 'maxLength': 256},
                'offset': {'type': 'integer', 'minimum': 0, 'default': 0},
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 1000, 'default': 50}},
            'allOf': [
                {'if': {'properties': {'action': {'enum': ['inspect', 'plan', 'generate']}}},
                 'then': {'required': ['source']}},
                {'if': {'properties': {'action': {'const': 'generate'}}}, 'then': {'required': ['output']}},
                {'if': {'properties': {'action': {'const': 'status'}}},
                 'then': {'required': ['output', 'idempotency_key']}}]},
        'semantics': {
            'range': '一基序号，含首尾；单词定位忽略英文大小写并取第一次匹配。',
            'timing': '前六个中文汉字和超出部分的每字秒数；每词固定英文阶段 2 秒，释义至少 0.5 秒。',
            'batch': '每包五轨，每包从 00:00:00 开始。',
            'inspect': '分页返回解析后的词条；任何解析错误都会中止，不静默跳过。',
            'plan': '只在临时目录渲染预览，不创建或修改目标目录。',
            'generate': '返回实际绝对路径及 SHA256；无 key 时沿用 GUI 的同名另建目录行为。',
            'idempotency': '带 key 时使用输出目录下确定的 run_* 子目录；同内容重试复用并校验文件；内容不同、文件被改或未完成均返回非零退出码。',
            'stdout': 'UTF-8 单个 JSON 对象；--help 例外，为人类帮助文本。'},
        'tracks': [{'index': i, 'suffix': suffix} for i, suffix in enumerate(_suffixes, 1)],
        'example': {'action': 'generate', 'source': {'text': 'apple [ˈæpəl] n. 苹果\nbanana n. 香蕉'},
                    'output': 'C:/output/word-subtitles', 'batch_size': 50, 'idempotency_key': 'lesson-001'}}
    properties = description['request_schema']['properties']
    for action, fields in ACTION_FIELDS.items():
        description['request_schema']['allOf'].append({
            'if': {'properties': {'action': {'const': action}}},
            'then': {'properties': {name: False for name in properties if name not in fields}}})
    return description


def doctor():
    try:
        docx_version = importlib.metadata.version('python-docx')
    except importlib.metadata.PackageNotFoundError:
        from subtitle_factory_core import docx
        docx_version = getattr(docx, '__version__', None)
    return {'python': sys.version.split()[0], 'frozen': bool(getattr(sys, 'frozen', False)),
            'gui_required': False, 'network_required': False,
            'qt_loaded': any(n.startswith(('PyQt', 'PySide')) for n in sys.modules),
            'docx_available': docx_version is not None, 'docx_version': docx_version,
            'entrypoint': str(Path(sys.executable if getattr(sys, 'frozen', False) else __file__).resolve())}


def load_words(source):
    object_fields(source, ('path', 'text'), 'source')
    if len(source) != 1:
        invalid('source 必须且只能指定 path 或 text。')
    if 'path' in source:
        path = Path(string(source['path'], 'source.path')).expanduser().resolve()
        try:
            text = _read_source(path)
        except Exception as exc:
            raise FactoryError('INPUT_ERROR', str(exc), {'path': str(path)}) from exc
        origin = {'kind': 'file', 'path': str(path)}
    else:
        if not isinstance(source['text'], str):
            invalid('source.text 必须是字符串。')
        text = source['text']
        origin = {'kind': 'text'}
    data, errors = LogicCore().parse(txt=text)
    if errors:
        raise FactoryError('INPUT_ERROR', '存在无法识别的词条，未生成任何输出。', {'lines': errors})
    if not data:
        raise FactoryError('INPUT_ERROR', '没有可识别的英语词条。')
    origin['text_sha256'] = hashlib.sha256(text.encode('utf-8')).hexdigest()
    return data, origin


def selection(data, requested):
    object_fields(requested, ('start', 'end', 'start_word', 'end_word'), 'range')
    endpoints = []
    for side, default in (('start', 1), ('end', len(data))):
        word_key = side+'_word'
        if side in requested and word_key in requested:
            invalid(f'range.{side} 与 range.{word_key} 不能同时指定。')
        if word_key in requested:
            word = string(requested[word_key], 'range.'+word_key).strip().lower()
            index = next((i for i, row in enumerate(data, 1) if row['w'].lower() == word), None)
            if index is None:
                raise FactoryError('INPUT_ERROR', '定位单词不存在。', {'field': word_key, 'word': word})
        else:
            index = integer(requested.get(side, default), 'range.'+side)
        endpoints.append(index)
    start, end = endpoints
    if not 1 <= start <= end <= len(data):
        invalid(f'范围必须在 1～{len(data)} 内，且起始不能大于结束。')
    return data[start-1:end], {'start': start, 'end': end}


def options(request):
    timing = request.get('timing', {})
    object_fields(timing, ('first_six', 'extra'), 'timing')
    timing = {'first_six': timing.get('first_six', .4), 'extra': timing.get('extra', .2)}
    for name, value in timing.items():
        try:
            valid = type(value) in (int, float) and math.isfinite(value) and value > 0
        except OverflowError:
            valid = False
        if not valid:
            invalid('timing.'+name+' 必须是有限的正数。')
        timing[name] = float(value)
    batch = integer(request['batch_size'], 'batch_size') if 'batch_size' in request else None
    key = string(request['idempotency_key'], 'idempotency_key') if 'idempotency_key' in request else None
    if key and len(key) > 256:
        invalid('idempotency_key 最多 256 个字符。')
    return timing, batch, key


def package_layout(data, batch):
    size = batch or len(data)
    return [data[i:i+size] for i in range(0, len(data), size)]


def render_plan(data, timing, batch):
    """Measure the actual canonical output instead of duplicating timing math."""
    files, packages = [], []
    core = LogicCore()
    with tempfile.TemporaryDirectory(prefix='subtitle_plan_') as folder:
        for number, rows in enumerate(package_layout(data, batch), 1):
            prefix = f'_Part{number}' if batch else ''
            core.gen(rows, folder, timing['first_six'], timing['extra'], prefix)
            base = (re.sub(r'[^\w]', '', rows[0]['w']) or 'Output')+prefix
            duration = 0
            for track, suffix in enumerate(_suffixes, 1):
                path = Path(folder)/(base+suffix)
                content = path.read_text(encoding='utf-8-sig')
                ends = re.findall(r' --> (\d+):(\d{2}):(\d{2}),(\d{3})', content)
                duration = max(duration, max((int(h)*3600+int(m)*60+int(s))*1000+int(ms)
                                             for h,m,s,ms in ends))
                files.append({'relative_path': path.name, 'package': number, 'track': track,
                              'bytes': path.stat().st_size, 'sha256': sha(path)})
            packages.append({'number': number, 'word_count': len(rows), 'first_word': rows[0]['w'],
                             'last_word': rows[-1]['w'], 'duration_ms': duration})
    return {'package_count': len(packages), 'file_count': len(files), 'packages': packages, 'files': files}


def keyed_directory(output, key):
    return output/('run_'+hashlib.sha256(key.encode('utf-8')).hexdigest()[:24])


def saved_run(directory, fingerprint=None):
    try:
        saved = json.loads((directory/'result.json').read_text(encoding='utf-8'))
    except FileNotFoundError as exc:
        raise FactoryError('INCOMPLETE_RUN', '该任务正在运行或上次未完整提交，未重复生成。',
                           {'directory': str(directory), 'hint': '检查该目录；如需独立重试，使用新的 idempotency_key。'}) from exc
    except (ValueError, OSError) as exc:
        raise FactoryError('OUTPUT_CHANGED', '任务记录无法读取。', {'directory': str(directory)}) from exc
    if not isinstance(saved, dict):
        raise FactoryError('OUTPUT_CHANGED', '任务记录格式无效。', {'directory': str(directory)})
    if fingerprint is not None and saved.get('fingerprint') != fingerprint:
        raise FactoryError('IDEMPOTENCY_CONFLICT', '相同 key 已用于不同的词条、时长或分包参数。')
    try:
        if saved['file_count'] != len(saved['files']) or not saved['files']:
            raise ValueError('file count mismatch')
        for item in saved['files']:
            path = (directory/item['relative_path']).resolve()
            if not path.is_relative_to(directory.resolve()) or not path.is_file() or sha(path) != item['sha256']:
                raise ValueError(item['relative_path'])
            item['path'] = str(path)
    except (KeyError, TypeError, OSError, ValueError) as exc:
        raise FactoryError('OUTPUT_CHANGED', '已生成的文件缺失、被改动或记录无效，未覆盖它们。',
                           {'directory': str(directory)}) from exc
    saved['reused'] = True
    return saved


def generate(data, timing, batch, output, key, common):
    fingerprint = digest({'api_version': API_VERSION, 'entries': data, 'timing': timing, 'batch_size': batch})
    directory = keyed_directory(output, key) if key else output
    if key and directory.exists():
        return saved_run(directory, fingerprint)
    plan = render_plan(data, timing, batch)
    reserved = False
    if key:
        output.mkdir(parents=True, exist_ok=True)
        try:
            directory.mkdir()
            reserved = True
        except FileExistsError:
            return saved_run(directory, fingerprint)
    published = False
    try:
        actual, count, file_count = _generate_packages(LogicCore(), data, str(directory),
                                                       timing['first_six'], timing['extra'], batch)
        published = True
        for item in plan['files']:
            path = actual/item['relative_path']
            if sha(path) != item['sha256']:
                raise FactoryError('OUTPUT_CHANGED', '生成后的内容与预览不一致。')
            item['path'] = str(path)
            if key:
                item['relative_path'] = str(path.relative_to(directory))
        result = {**common, **plan, 'fingerprint': fingerprint, 'directory': str(actual),
                  'idempotent': bool(key), 'reused': False}
        if key:
            result['run_directory'] = str(directory)
            temporary = directory/'result.json.tmp'
            temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(directory/'result.json')
        return result
    except Exception:
        # Only remove an empty directory exclusively reserved by this call.
        # Published files remain inspectable if the final manifest could not be written.
        if reserved and not published:
            try:
                directory.rmdir()
            except OSError:
                pass
        raise


def execute(request):
    object_fields(request, ('action', 'source', 'output', 'range', 'timing', 'batch_size',
                            'idempotency_key', 'offset', 'limit'), 'request')
    action = request.get('action')
    allowed = ACTION_FIELDS
    if not isinstance(action, str) or action not in allowed:
        invalid('action 必须是 schema、doctor、inspect、plan、generate 或 status。')
    object_fields(request, allowed[action], action)
    if action == 'schema':
        return request_schema()
    if action == 'doctor':
        return doctor()
    try:
        output = Path(string(request['output'], 'output')).expanduser().resolve() if 'output' in request else None
        if action in ('generate', 'status') and output is None:
            invalid('必须指定 output。')
        if output is not None and output.exists() and not output.is_dir():
            raise FactoryError('OUTPUT_ERROR', 'output 必须是目录。')
        if action == 'status':
            key = string(request.get('idempotency_key'), 'idempotency_key')
            if len(key) > 256:
                invalid('idempotency_key 最多 256 个字符。')
            directory = keyed_directory(output, key)
            if not directory.exists():
                return {'state': 'not_found', 'directory': str(directory)}
            return {'state': 'complete', **saved_run(directory)}
        data, source = load_words(request.get('source'))
        selected, span = selection(data, request.get('range', {}))
        common = {'source': source, 'total_count': len(data), 'selected_count': len(selected), 'selection': span}
        if action == 'inspect':
            offset = integer(request.get('offset', 0), 'offset', 0)
            limit = integer(request.get('limit', 50), 'limit')
            if limit > 1000:
                invalid('limit 最多 1000。')
            page = selected[offset:offset+limit]
            return {**common, 'entries': [{'index': span['start']+offset+i, 'word': row['w'],
                      'phonetic': row['p'], 'definition': row['d_f'], 'definition_without_pos': row['d_c']}
                     for i,row in enumerate(page)], 'offset': offset, 'limit': limit,
                    'next_offset': offset+len(page) if offset+len(page)<len(selected) else None}
        timing, batch, key = options(request)
        common.update(timing=timing, batch_size=batch)
        if action == 'plan':
            result = {**common, **render_plan(selected, timing, batch)}
            result['output_root'] = str(output) if output is not None else None
            result['planned_directory'] = str(keyed_directory(output, key) if key else output) if output else None
            result['existing_name_conflict'] = bool(output and not key and any((output/i['relative_path']).exists() for i in result['files']))
            return result
        return generate(selected, timing, batch, output, key, common)
    except FactoryError:
        raise
    except (OSError, OverflowError, ValueError) as exc:
        raise FactoryError('OUTPUT_ERROR', str(exc)) from exc

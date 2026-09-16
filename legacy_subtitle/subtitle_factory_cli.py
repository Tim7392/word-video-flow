"""JSON command line for Subtitle Factory. No GUI modules are imported."""
import argparse
import json
from pathlib import Path
import sys

from subtitle_factory_api import API_VERSION, FactoryError, execute


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise FactoryError('INVALID_REQUEST', message)


def read_stdin():
    if sys.stdin.isatty():
        raise FactoryError('INVALID_REQUEST', '请通过管道传入 UTF-8 内容，或使用文件参数。')
    try:
        return sys.stdin.buffer.read().decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise FactoryError('INVALID_REQUEST', '标准输入必须采用 UTF-8 编码。') from exc


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise FactoryError('INVALID_REQUEST', 'JSON 字段重复：'+key)
        result[key] = value
    return result


def parse_request(raw):
    def invalid_constant(value):
        raise FactoryError('INVALID_REQUEST', 'JSON 不允许非有限数字：'+value)
    try:
        return json.loads(raw, object_pairs_hook=unique_object, parse_constant=invalid_constant)
    except json.JSONDecodeError as exc:
        raise FactoryError('INVALID_REQUEST', f'JSON 格式错误：第 {exc.lineno} 行，第 {exc.colno} 列。') from exc


def parser():
    root = Parser(description='Subtitle Factory: local vocabulary -> five SRT tracks. UTF-8 JSON stdout.')
    sub = root.add_subparsers(dest='action', required=True)
    sub.add_parser('schema', help='Describe JSON requests, capabilities and exit codes')
    sub.add_parser('doctor', help='Check runtime and dependencies without a window')
    request = sub.add_parser('request', help='Read a complete UTF-8 JSON request')
    request.add_argument('--json-file', help='Request file; omit to read stdin')
    for action in ('inspect', 'plan', 'generate'):
        command = sub.add_parser(action)
        source = command.add_mutually_exclusive_group(required=True)
        source.add_argument('--input', dest='source_path', help='TXT, DOCX or vocabulary SRT')
        source.add_argument('--text', help='Vocabulary text (prefer stdin/JSON file for multiline text)')
        source.add_argument('--stdin', action='store_true', help='Read UTF-8 vocabulary text from stdin')
        command.add_argument('--start', type=int, help='One-based inclusive first entry')
        command.add_argument('--end', type=int, help='One-based inclusive last entry')
        command.add_argument('--start-word')
        command.add_argument('--end-word')
        if action == 'inspect':
            command.add_argument('--offset', type=int, default=0)
            command.add_argument('--limit', type=int, default=50)
        else:
            command.add_argument('--output', required=action=='generate')
            command.add_argument('--batch-size', type=int)
            command.add_argument('--first-six', type=float, default=.4)
            command.add_argument('--extra', type=float, default=.2)
            command.add_argument('--idempotency-key')
    status = sub.add_parser('status', help='Verify a previous keyed run and its files')
    status.add_argument('--output', required=True)
    status.add_argument('--idempotency-key', required=True)
    return root


def request_from_args(args):
    fields = vars(args)
    action = fields.pop('action')
    if action == 'request':
        try:
            raw = Path(args.json_file).read_text(encoding='utf-8-sig') if args.json_file else read_stdin()
        except (OSError, UnicodeError) as exc:
            raise FactoryError('INVALID_REQUEST', '无法读取 UTF-8 JSON 请求文件：'+str(exc)) from exc
        return parse_request(raw)
    request = {'action': action}
    if action in ('schema', 'doctor'):
        return request
    if action == 'status':
        return {**request, **fields}
    if fields.pop('stdin'):
        source = {'text': read_stdin()}
    elif fields['source_path'] is not None:
        source = {'path': fields['source_path']}
    else:
        source = {'text': fields['text']}
    fields.pop('source_path'); fields.pop('text')
    request['source'] = source
    span = {key: fields.pop(key) for key in ('start', 'end', 'start_word', 'end_word')}
    request['range'] = {key: value for key,value in span.items() if value is not None}
    if action != 'inspect':
        request['timing'] = {'first_six': fields.pop('first_six'), 'extra': fields.pop('extra')}
    request.update({key: value for key,value in fields.items() if value is not None})
    return request


def main(argv=None):
    action = None
    try:
        request = request_from_args(parser().parse_args(argv))
        action = request.get('action') if isinstance(request, dict) else None
        result = execute(request)
        response = {'ok': True, 'api_version': API_VERSION, 'action': action, 'result': result}
        code = 0
    except FactoryError as exc:
        response = {'ok': False, 'api_version': API_VERSION, 'action': action,
                    'error': {'code': exc.code, 'message': str(exc), 'details': exc.details}}
        code = exc.exit_code
    except KeyboardInterrupt:
        response = {'ok': False, 'api_version': API_VERSION, 'action': action,
                    'error': {'code': 'INTERRUPTED', 'message': '操作被取消；带 key 的任务可用 status 核验。'}}
        code = 130
    except Exception as exc:
        response = {'ok': False, 'api_version': API_VERSION, 'action': action,
                    'error': {'code': 'INTERNAL_ERROR', 'message': str(exc)}}
        code = 1
    raw = json.dumps(response, ensure_ascii=False, allow_nan=False)+'\n'
    try:
        sys.stdout.buffer.write(raw.encode('utf-8'))
        sys.stdout.buffer.flush()
    except BrokenPipeError:
        return 1
    return code


if __name__ == '__main__':
    raise SystemExit(main())

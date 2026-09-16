"""H0 工具：从现有请求 JSON 派生一个"新幂等键/新输出目录"的变体。

用途：同一份输入要在不同代码版本上重跑（幂等键相同会直接返回原任务，不会重新执行）。
只改 `idempotency_key` 与 `output` 两个字段，其余逐字保留。
"""
import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--fixture', required=True)
    parser.add_argument('--key', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()

    source = Path(args.fixture)
    data = json.loads(source.read_text(encoding='utf-8'))
    old_key, old_output = data.get('idempotency_key'), data.get('output')
    data['idempotency_key'] = args.key
    data['output'] = args.output
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps({'fixture': str(source), 'out': str(target),
                      'old_key': old_key, 'key': args.key,
                      'old_output': old_output, 'output': args.output,
                      'entries': len(data.get('lesson', {}).get('entries', [])),
                      'prepared_speech': len(data.get('prepared_speech', []) or [])},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

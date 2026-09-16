"""QA-7: build a self-contained project root for the published-directory check.

Everything the run touches lives inside the root, so "the published documents
point at files that exist" is a statement about this root alone.

Usage: python setup_root.py --template DIR --root DIR [--background FILE]
"""
import argparse
import json
from pathlib import Path
import shutil
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

DEFAULT_BACKGROUND = Path(r'D:\单词速记自动化_测试归档_0915\_prepared-1080p'
                          r'\background-from-reference-1080p.mp4')


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--template', required=True)
    parser.add_argument('--root', required=True)
    parser.add_argument('--project', default='p1')
    parser.add_argument('--background', default=str(DEFAULT_BACKGROUND))
    args = parser.parse_args(argv)

    template = Path(args.template).resolve(strict=True)
    root = Path(args.root).resolve()
    target = root / 'projects' / args.project
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(template, target)

    old = str(template)
    rewritten = []
    assets_path = target / 'assets.json'
    if assets_path.exists():
        # Point every asset at the copy that now sits under this root.  Rewriting by
        # *basename* rather than by prefix is deliberate: a template may share one
        # media folder across several projects, so the stored path need not live
        # under the folder that was copied.
        document = json.loads(assets_path.read_text(encoding='utf-8'))
        moved, kept = 0, 0
        for item in document.get('assets', []):
            source = Path(item['path'])
            candidate = target / 'media' / source.name
            if source.name and candidate.exists():
                item['path'] = str(candidate)
                moved += 1
            else:
                kept += 1
        assets_path.write_text(json.dumps(document, ensure_ascii=False, indent=1),
                               encoding='utf-8')
        rewritten.append('assets.json (moved %d, kept %d)' % (moved, kept))

    assets = json.loads((target / 'assets.json').read_text(encoding='utf-8'))
    outside = [item['path'] for item in assets['assets']
               if not item['path'].startswith(str(target))]
    # The delivery background is an input of this run, not part of the project:
    # the check passes it explicitly so a missing one can be exercised too.
    delivery = target / 'delivery.json'
    if delivery.exists():
        document = json.loads(delivery.read_text(encoding='utf-8'))
        document['background'] = str(Path(args.background).resolve())
        delivery.write_text(json.dumps(document, ensure_ascii=False, indent=1),
                            encoding='utf-8')

    report = {'template': str(template), 'root': str(root), 'project': str(target),
              'rewritten': rewritten, 'assets': len(assets['assets']),
              'assets_outside_root': outside,
              'background': str(Path(args.background).resolve()),
              'background_exists': Path(args.background).exists(),
              'project_json': (target / 'project.json').exists()}
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if not outside and report['project_json'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

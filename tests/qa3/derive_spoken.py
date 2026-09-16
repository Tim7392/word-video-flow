"""QA-3: derive the spoken-reading text from the approved rules, independently.

Two sources of truth, neither of them the implementation under test:

  legacy  the delivered archive itself.  Its ``timeline.json`` publishes, for all
          50 words of batch 151-200, the reading text the old engine produced.
          Whatever rule reproduces those 50 entries *is* the legacy rule; it is
          checked here before it is used for anything else.
  v2      the approved policy text kept in ``accept_range.spoken_of`` (the
          wording reviewed on 2026-09-16): a part-of-speech tag goes away together
          with the connectors that only join tags, phonetics embedded in the
          definition go too, and anything that is part of the real definition
          ("R&D", "A/B", a slash inside a word) is left alone.

The word list is parsed by QA's own reader (``accept_range.split_entry``), which
reproduces the legacy core's display meaning from the original line instead of
calling it.

Usage:
  python derive_spoken.py --wordlist TXT [--archive BATCH] [--json OUT]
                          [--show 179,186,200] [--dump TXT]
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from acceptance.accept_range import split_entry, spoken_of  # noqa: E402

WORDLIST_SHA256 = 'b42f381ab2a3757d9b31c11a22444bebf20ca3f26982aaba7e6283b97916e99a'

# The approved policy, restated as data so the counts below can be recomputed by
# hand from it.  Each entry is (name, pattern, replacement, then-collapse-spaces).
POS_WORD = r'(?:n|v|vt|vi|adj|adv|prep|conj|pron|num|art|int|aux|abbr|pl)\.'
RULES = {
    'legacy': {'pattern': re.compile(r'(?<![A-Za-z])%s' % POS_WORD), 'replace': '',
               'collapse': False},
    'v2': {'pattern': re.compile(r'(?<![A-Za-z])%s(?:\s*(?:&|/|,|，|、)\s*%s)*'
                                 % (POS_WORD, POS_WORD)),
           'replace': ' ', 'collapse': True,
           'then': re.compile(r'/[^/\s]+/'), 'then_replace': ' '},
}


def clean(meaning, policy):
    rule = RULES[policy]
    text = rule['pattern'].sub(rule['replace'], meaning)
    if 'then' in rule:
        text = rule['then'].sub(rule['then_replace'], text)
    return ' '.join(text.split()) if rule['collapse'] else text.strip()


def entries_of(wordlist):
    lines = [line.strip() for line in
             Path(wordlist).read_text(encoding='utf-8-sig').splitlines()]
    lines = [line for line in lines if line]
    out = []
    for number, line in enumerate(lines, 1):
        word, phonetic, meaning = split_entry(line)
        out.append({'index': number, 'line': line, 'word': word,
                    'phonetic': phonetic, 'meaning': meaning,
                    'legacy': clean(meaning, 'legacy'), 'v2': clean(meaning, 'v2')})
    return out


def archive_anchor(archive, wordlist):
    """Prove the legacy rule on the artefact that was delivered with it."""
    timeline = json.loads((Path(archive) / 'timeline.json').read_text(encoding='utf-8'))
    published = {int(w['index']): w for w in timeline['words']}
    mine = {entry['index']: entry for entry in entries_of(wordlist)}
    checked, mismatches = 0, []
    for index, word in sorted(published.items()):
        if index not in mine:
            mismatches.append('word %d not in the word list' % index)
            continue
        checked += 1
        for field, value in (('word', mine[index]['word']),
                             ('phonetic', mine[index]['phonetic']),
                             ('meaning', mine[index]['meaning']),
                             ('spoken_meaning', mine[index]['legacy'])):
            if word.get(field) != value:
                mismatches.append('word %d %s: archive %r != derived %r'
                                  % (index, field, word.get(field), value))
    return {'checked': checked, 'mismatches': mismatches[:10],
            'legacy_rule_confirmed': not mismatches}


def counts(entries):
    legacy, v2 = {}, {}
    for entry in entries:
        legacy.setdefault(entry['legacy'], []).append(entry['index'])
        v2.setdefault(entry['v2'], []).append(entry['index'])
    return legacy, v2


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--wordlist', required=True)
    parser.add_argument('--archive', default=None)
    parser.add_argument('--json')
    parser.add_argument('--dump', default=None, help='把逐行推导写成 TSV')
    parser.add_argument('--spoken-json', default=None,
                        help='把 {index: v2 朗读文本} 写成 accept_range --spoken-file 用的表')
    parser.add_argument('--show', default='')
    args = parser.parse_args(argv)

    wordlist = Path(args.wordlist)
    digest = hashlib.sha256(wordlist.read_bytes()).hexdigest()
    if digest != WORDLIST_SHA256:
        raise SystemExit('只读词表内容变了（%s），期望必须重新复核' % digest)
    entries = entries_of(wordlist)
    report = {'wordlist': str(wordlist), 'wordlist_sha256': digest,
              'lines': len(entries), 'rules': {name: rule['pattern'].pattern
                                               for name, rule in RULES.items()}}
    if args.archive:
        report['archive_anchor'] = archive_anchor(args.archive, wordlist)
    # Which lines the two rules disagree on, and what the disagreement looks like.
    differ = [e for e in entries if e['legacy'] != e['v2']]
    report['v2_differs_from_legacy'] = len(differ)
    report['residual_connectors'] = [
        e['index'] for e in entries
        if re.search(r'[&/]', e['v2'])]
    report['examples'] = [{'index': e['index'], 'meaning': e['meaning'],
                           'legacy': e['legacy'], 'v2': e['v2']}
                          for e in differ[:8]]
    # Words whose v2 form still ends up empty (a meaning that was only a tag).
    report['v2_empty'] = [e['index'] for e in entries if not e['v2']]
    if args.show:
        wanted = {int(part) for part in args.show.split(',') if part.strip()}
        report['selected'] = [{'index': e['index'], 'line': e['line'],
                               'meaning': e['meaning'], 'legacy': e['legacy'],
                               'v2': e['v2']} for e in entries if e['index'] in wanted]
    if args.dump:
        target = Path(args.dump)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('\n'.join(
            '%d\t%s\t%s\t%s' % (e['index'], e['word'], e['legacy'], e['v2'])
            for e in entries) + '\n', encoding='utf-8')
        report['dump'] = str(target)
    if args.spoken_json:
        target = Path(args.spoken_json)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({str(e['index']): e['v2'] for e in entries},
                                     ensure_ascii=False, indent=1), encoding='utf-8')
        report['spoken_json'] = str(target)
    text = json.dumps(report, ensure_ascii=False, indent=1)
    print(text)
    if args.json:
        target = Path(args.json)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

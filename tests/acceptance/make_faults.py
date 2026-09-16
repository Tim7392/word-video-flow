"""Fault injection: derive deliberately broken batches from one delivered good batch.

Origin: `D:\\1\\1-AI_workflow\\word_video_m0\\tools\\make_faults.py` (2026-09-16),
generalised by QA (task QA-1) so it runs from the new worktree:

  * every path is an argument (``--good-batch``, ``--faults``, ``--wordlist``,
    ``--range``) instead of an M0 constant, so the tool never assumes where the
    read-only archive or the word list live;
  * the "which fault was verified so far" list is returned as data
    (``EXPECTED``/``CASES``) for the matrix and for the pytest self-test;
  * ``--verify-original`` re-reads the sha256 of every file the original
    ``complete.json`` lists, which is the hard proof that the source archive was
    not written through a hard link.

The samples live under ``faults/`` and are built with NTFS hard links, so only
the files that are actually changed take space.  Every derived sample gets its
``complete.json`` paths rewritten to the sample directory and its hashes
recomputed - otherwise the *integrity* face would report a hash mismatch first
and hide the semantic defect the sample is meant to test.

``complete.json`` is **copied, never linked**: it is rewritten on every build and
writing in place through a hard link would corrupt the read-only original.  That
accident really happened in M0 (2026-09-16, see ``restore_complete.py`` in the M0
evidence directory).  Media files are only ever read, so linking them is safe.

Usage:
  python make_faults.py build --good-batch GOOD --faults DIR --wordlist TXT
                             [--range 151-200] [--verify-original]
  python make_faults.py list
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import wave

sys.stdout.reconfigure(encoding='utf-8', errors='replace')


def resolve(tool):
    found = shutil.which(tool)
    if found:
        return found
    for candidate in (Path.home() / '.local' / 'bin' / (tool + '.EXE'),
                      Path.home() / '.local' / 'bin' / tool,
                      Path(r'C:\ffmpeg\bin') / (tool + '.exe')):
        if candidate.exists():
            return str(candidate)
    raise RuntimeError('cannot find %s' % tool)


FFMPEG = resolve('ffmpeg')
FFPROBE = resolve('ffprobe')


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def link_tree(source, target):
    """Hard-link copy of a whole tree; an existing target is removed first.

    Exception: ``complete.json`` is never hard linked.  It is rewritten by this
    tool, and writing in place would go through the link into the original
    archive (that happened once on 2026-09-16).  Media files are read-only, so
    linking them is safe and saves gigabytes.
    """
    if target.exists():
        shutil.rmtree(target)
    for path in sorted(source.rglob('*')):
        rel = path.relative_to(source)
        destination = target / rel
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if path.name == 'complete.json':
                shutil.copy2(path, destination)
            else:
                os.link(path, destination)


def write_bytes(path, data):
    """Replace a file that may be a hard link: unlink first, then write."""
    path = Path(path)
    path.unlink()
    path.write_bytes(data)


class Faults:
    """Builds every fault sample below one directory from one good batch."""

    def __init__(self, good_batch, faults_dir, wordlist=None, range_=(151, 200)):
        self.good = Path(good_batch).resolve(strict=True)
        self.faults = Path(faults_dir)
        self.wordlist = Path(wordlist).resolve() if wordlist else None
        self.range = range_
        self.batch_name = self.good.name
        self.notes = {}

    # --- plumbing ---------------------------------------------------------

    def sample_dir(self, name):
        return self.faults / name / self.batch_name

    def base_sample(self, name):
        sample = self.sample_dir(name)
        sample.parent.mkdir(parents=True, exist_ok=True)
        link_tree(self.good, sample)
        complete = self.rewrite_bookkeeping(sample)
        return sample, complete

    def rewrite_bookkeeping(self, sample):
        """Point complete.json at the sample directory; drop nothing yet."""
        complete_path = sample / 'complete.json'
        complete = json.loads(complete_path.read_text(encoding='utf-8'))
        original = str(self.good)
        for item in complete.get('files', []):
            if str(item['path']).startswith(original):
                item['path'] = str(sample) + str(item['path'])[len(original):]
        for key in ('draft', 'video'):
            value = complete.get('batch', {}).get(key)
            if isinstance(value, str) and value.startswith(original):
                complete['batch'][key] = str(sample) + value[len(original):]
        write_bytes(complete_path, json.dumps(complete, ensure_ascii=False,
                                              indent=1).encode('utf-8'))
        return complete

    def refresh_hashes(self, sample, complete):
        """Recompute sha256 only for files that really changed; drop the rest."""
        kept, dropped = [], []
        for item in complete['files']:
            path = Path(item['path'])
            if not path.exists():
                dropped.append(path.name)
                continue
            kept.append({'path': item['path'], 'sha256': sha256(path)})
        complete['files'] = kept
        write_bytes(sample / 'complete.json',
                    json.dumps(complete, ensure_ascii=False, indent=1).encode('utf-8'))
        return dropped

    def finalize(self, sample, complete, note):
        dropped = self.refresh_hashes(sample, complete)
        if dropped:
            note += '；complete.json 中已删除 %d 条不存在的条目' % len(dropped)
        self.notes[sample.parent.name] = note

    def load_draft(self, sample):
        path = sample / 'editable-draft' / 'draft_content.json'
        return path, json.loads(path.read_text(encoding='utf-8'))

    def save_draft(self, path, draft):
        write_bytes(path, json.dumps(draft, ensure_ascii=False).encode('utf-8'))

    # --- the samples ------------------------------------------------------

    def make_good(self):
        """Control: the untouched good batch must still PASS."""
        sample, complete = self.base_sample('good')
        self.finalize(sample, complete, '对照组：未改动的好样本')

    def make_missing_track(self):
        sample, complete = self.base_sample('bad-missing-phonetic-track')
        path, draft = self.load_draft(sample)
        before = len(draft['tracks'])
        draft['tracks'] = [t for t in draft['tracks']
                           if not (t['type'] == 'text' and t.get('name') == 'phonetic')]
        self.save_draft(path, draft)
        self.finalize(sample, complete,
                      '删掉整条音标轨（%d -> %d 轨）' % (before, len(draft['tracks'])))

    def make_phonetic_shifted(self):
        sample, complete = self.base_sample('bad-phonetic-start-shifted')
        path, draft = self.load_draft(sample)
        shifted = 0
        for track in draft['tracks']:
            if track['type'] == 'text' and track.get('name') == 'phonetic':
                for segment in track['segments']:
                    segment['target_timerange']['start'] += 100000  # 100 ms
                    shifted += 1
        self.save_draft(path, draft)
        self.finalize(sample, complete, '全部音标段延后 100ms：%d 段' % shifted)

    def make_word2_changed(self):
        """timeline / SRT / draft changed together: self-consistent, only the
        read-only word list can still see it."""
        sample, complete = self.base_sample('bad-word2-text-changed')
        timeline_path = sample / 'timeline.json'
        timeline = json.loads(timeline_path.read_text(encoding='utf-8'))
        victim = timeline['words'][1]
        old = victim['word']
        victim['word'] = old + 's'
        write_bytes(timeline_path, json.dumps(timeline, ensure_ascii=False,
                                              indent=1).encode('utf-8'))
        base = '%04d-%04d' % (self.range[0], self.range[1])
        hits = []
        for suffix in ('_01_英文重复.srt', '_02_英文单次.srt'):
            path = sample / 'srt' / (base + suffix)
            lines = path.read_text(encoding='utf-8-sig').splitlines()
            hit = sum(1 for line in lines if line.strip() == old)
            lines = [old + 's' if line.strip() == old else line for line in lines]
            write_bytes(path, ('\n'.join(lines) + '\n').encode('utf-8-sig'))
            hits.append('%s %d 处' % (suffix, hit))
        draft_path, draft = self.load_draft(sample)
        # Text lives in materials.texts[].content (a JSON string); segments only
        # reference material_id.
        target_ids = set()
        for track in draft['tracks']:
            if track['type'] == 'text' and track.get('name') == 'english':
                segments = sorted(track['segments'],
                                  key=lambda s: s['target_timerange']['start'])
                target_ids.add(segments[1]['material_id'])
        hit = 0
        for material in draft['materials'].get('texts', []):
            if material['id'] not in target_ids:
                continue
            content = json.loads(material['content'])
            if content.get('text') == old:
                content['text'] = old + 's'
                material['content'] = json.dumps(content, ensure_ascii=False)
                hit += 1
        self.save_draft(draft_path, draft)
        self.finalize(sample, complete,
                      'timeline/SRT/草稿协同把第 2 个词 %r 改成 %r（SRT %s，草稿素材 %d 处）'
                      % (old, old + 's', '、'.join(hits), hit))

    def make_mix_truncated(self):
        sample, complete = self.base_sample('bad-mix-truncated')
        path = sample / 'video' / 'mix.wav'
        with wave.open(str(path), 'rb') as stream:
            params = stream.getparams()
            frames = stream.readframes(stream.getnframes())
        keep = len(frames) - int(params.framerate * 2 * 1.5)  # tail 1.5 s (mono s16)
        keep -= keep % 2
        temporary = path.with_suffix('.truncated.wav')
        with wave.open(str(temporary), 'wb') as stream:
            stream.setparams(params)
            stream.writeframes(frames[:keep])
        path.unlink()
        os.rename(temporary, path)
        self.finalize(sample, complete, 'mix.wav 砍掉尾部 1.5s：%d -> %d 样本'
                      % (len(frames) // 2, keep // 2))

    def make_mp4_tail_silenced(self):
        """The published MP4 loses its audible tail: the picture keeps its length,
        the last word's Chinese stage goes silent."""
        sample, complete = self.base_sample('bad-mp4-tail-silenced')
        path = sample / 'video' / 'video.mp4'
        probe = subprocess.run([FFPROBE, '-v', 'error', '-show_entries',
                                'format=duration', '-of', 'csv=p=0', str(path)],
                               capture_output=True, text=True, check=True)
        duration = float(probe.stdout.strip())
        temporary = path.with_suffix('.silenced.mp4')
        subprocess.run([FFMPEG, '-v', 'error', '-nostdin', '-y', '-i', str(path),
                        '-c:v', 'copy', '-af', 'atrim=0:10,apad', '-t', '%.3f' % duration,
                        '-c:a', 'aac', '-b:a', '192k', str(temporary)], check=True)
        path.unlink()
        os.rename(temporary, path)
        self.finalize(sample, complete, '总时长 %.3fs，10 秒之后音频静音' % duration)

    def make_unreadable(self):
        sample, complete = self.base_sample('bad-timeline-unreadable')
        write_bytes(sample / 'timeline.json', b'{ this is not json')
        self.finalize(sample, complete, 'timeline.json 写成非法 JSON')

    def make_draft_unreadable(self):
        sample, complete = self.base_sample('bad-draft-unreadable')
        write_bytes(sample / 'editable-draft' / 'draft_content.json',
                    b'\x00\x01 not a draft at all')
        self.finalize(sample, complete, 'draft_content.json 写成非 JSON 字节')

    def make_timeline_only_changed(self):
        """A related hole at a different level: the *timeline* is changed while the
        published SRT and draft still carry the delivered text.

        This is the "timeline says something the word list does not" case.  It is
        caught by the timeline-versus-word-list comparison, which runs before any
        face, so it proves that comparison is wired up rather than merely present.
        """
        sample, complete = self.base_sample('bad-timeline-only-changed')
        timeline_path = sample / 'timeline.json'
        timeline = json.loads(timeline_path.read_text(encoding='utf-8'))
        victim = timeline['words'][0]
        old = victim['word']
        victim['word'] = old + 'x'
        write_bytes(timeline_path, json.dumps(timeline, ensure_ascii=False,
                                              indent=1).encode('utf-8'))
        self.finalize(sample, complete,
                      '只有 timeline 的第 1 个词 %r -> %r，SRT/草稿未跟随'
                      % (old, old + 'x'))

    # --- archive-level roots used by accept_all ---------------------------

    def root_missing_batch(self):
        """One group holding only part of the expected ranges."""
        root = self.faults / 'root-missing-batch'
        if root.exists():
            shutil.rmtree(root)
        (root / 'wv-synthetic').mkdir(parents=True)
        link_tree(self.faults / 'good' / self.batch_name,
                  root / 'wv-synthetic' / self.batch_name)
        self.repoint(root / 'wv-synthetic' / self.batch_name,
                     self.faults / 'good', root / 'wv-synthetic')
        self.notes['root-missing-batch'] = '只有 1 包，--expect 要求 2 个范围'

    def root_extra_batch(self):
        root = self.faults / 'root-extra-batch'
        if root.exists():
            shutil.rmtree(root)
        (root / 'wv-synthetic').mkdir(parents=True)
        link_tree(self.faults / 'good' / self.batch_name,
                  root / 'wv-synthetic' / self.batch_name)
        self.repoint(root / 'wv-synthetic' / self.batch_name,
                     self.faults / 'good', root / 'wv-synthetic')
        self.notes['root-extra-batch'] = '发布的 151-200 不在 --expect 里'

    def root_empty(self):
        root = self.faults / 'root-empty'
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
        self.notes['root-empty'] = '空目录'

    def root_duplicate_batch(self):
        """The same range published twice: a stale delivery next to a new one."""
        root = self.faults / 'root-duplicate-batch'
        if root.exists():
            shutil.rmtree(root)
        for group in ('wv-old', 'wv-new'):
            (root / group).mkdir(parents=True)
            link_tree(self.faults / 'good' / self.batch_name,
                      root / group / self.batch_name)
            self.repoint(root / group / self.batch_name,
                         self.faults / 'good', root / group)
        self.notes['root-duplicate-batch'] = '151-200 出现在两个 group 下'

    def root_stale_report(self):
        """A crashed validator left no fresh report: the old PASS must not speak.

        The batch is readable, so ``accept_all`` really calls ``accept_range``;
        the caller points that child at a word list that does not exist, so the
        child dies *before* it can write a report.  Exactly then a pre-positioned
        ``acc-*.json`` still saying PASS must be recognised as stale.
        """
        root = self.faults / 'root-stale-report'
        if root.exists():
            shutil.rmtree(root)
        (root / 'wv-crash').mkdir(parents=True)
        link_tree(self.faults / 'good' / self.batch_name,
                  root / 'wv-crash' / self.batch_name)
        self.repoint(root / 'wv-crash' / self.batch_name,
                     self.faults / 'good', root / 'wv-crash')
        (root / ('acc-wv-crash-%s.json' % self.batch_name)).write_text(
            json.dumps({'batch': 'stale', 'verdict': 'PASS', 'stale': True}),
            encoding='utf-8')
        self.notes['root-stale-report'] = '子验收器崩溃前留下的旧 PASS 报告（诱饵）'

    def root_stale_pass_without_batch(self):
        """The same trap without a readable batch: report present, delivery gone."""
        root = self.faults / 'root-stale-pass-no-batch'
        if root.exists():
            shutil.rmtree(root)
        (root / 'wv-gone').mkdir(parents=True)
        (root / ('acc-wv-gone-%s.json' % self.batch_name)).write_text(
            json.dumps({'batch': 'stale', 'verdict': 'PASS', 'stale': True}),
            encoding='utf-8')
        self.notes['root-stale-pass-no-batch'] = '没有批次，只有一份旧 PASS 报告'

    def repoint(self, sample, old_root, new_root):
        """Rewrite complete.json paths from one root to another (no hashing:
        only the path text changes and the files are the same bytes)."""
        complete_path = sample / 'complete.json'
        complete = json.loads(complete_path.read_text(encoding='utf-8'))
        for item in complete.get('files', []):
            item['path'] = item['path'].replace(str(old_root), str(new_root))
        for key in ('draft', 'video'):
            value = complete.get('batch', {}).get(key)
            if isinstance(value, str):
                complete['batch'][key] = value.replace(str(old_root), str(new_root))
        write_bytes(complete_path, json.dumps(complete, ensure_ascii=False,
                                             indent=1).encode('utf-8'))

    # --- driver -----------------------------------------------------------

    BUILDERS = ('make_good', 'make_missing_track', 'make_phonetic_shifted',
                'make_word2_changed', 'make_timeline_only_changed', 'make_mix_truncated',
                'make_mp4_tail_silenced', 'make_unreadable', 'make_draft_unreadable',
                'root_missing_batch', 'root_extra_batch', 'root_empty',
                'root_duplicate_batch', 'root_stale_report',
                'root_stale_pass_without_batch')

    def build(self, only=None):
        self.faults.mkdir(parents=True, exist_ok=True)
        for name in self.BUILDERS:
            if only and name not in only:
                continue
            print('-> %s' % name)
            getattr(self, name)()
        return self.notes

    def verify_original_untouched(self, hashes=False):
        """Hard links are the easiest way to corrupt the source archive.

        Cheap part: the original bookkeeping must still point at itself and it
        must still be the only complete.json in the batch.  With ``hashes`` the
        sha256 of every listed file is recomputed - the real proof that no hard
        link was written through.
        """
        problems = []
        complete = json.loads((self.good / 'complete.json').read_text(encoding='utf-8'))
        stray = [i['path'] for i in complete.get('files', [])
                 if not str(i['path']).startswith(str(self.good))]
        if stray:
            problems.append('原归档 complete.json 里有 %d 条路径不在原批次下，例如 %s'
                            % (len(stray), stray[:1]))
        for key in ('draft', 'video'):
            value = complete.get('batch', {}).get(key)
            if isinstance(value, str) and not value.startswith(str(self.good)):
                problems.append('原归档 complete.json 的 batch.%s 指向了别处' % key)
        if list(self.good.rglob('complete.json')) != [self.good / 'complete.json']:
            problems.append('批次目录里出现了不止一份 complete.json')
        if hashes:
            bad = []
            for item in complete.get('files', []):
                path = Path(item['path'])
                if not path.exists():
                    bad.append('missing %s' % path.name)
                elif sha256(path) != item['sha256']:
                    bad.append('hash changed %s' % path.name)
            if bad:
                problems.append('原归档 %d 个文件与 complete.json 不符，例如 %s'
                                % (len(bad), bad[:3]))
        return problems


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['build', 'list'])
    parser.add_argument('--good-batch', help='已交付的好批次目录（只读）')
    parser.add_argument('--faults', help='坏样本根目录（可写，建议放在 runtime/tmp 下）')
    parser.add_argument('--wordlist', help='只读原始词表（可选，仅作记录）')
    parser.add_argument('--range', default='151-200')
    parser.add_argument('--verify-original', action='store_true',
                        help='额外重算原归档每个文件的 sha256（慢，最硬的证据）')
    args = parser.parse_args(argv)

    if args.command == 'list':
        print(json.dumps(EXPECTED, ensure_ascii=False, indent=1))
        return 0
    if not (args.good_batch and args.faults):
        parser.error('build 需要 --good-batch 与 --faults')
    first, last = (int(part) for part in args.range.split('-'))
    builder = Faults(args.good_batch, args.faults, args.wordlist, (first, last))
    before = builder.verify_original_untouched(hashes=args.verify_original)
    if before:
        print(json.dumps({'ok': False, '原归档本就不一致': before}, ensure_ascii=False, indent=1))
        return 1
    notes = builder.build()
    problems = builder.verify_original_untouched(hashes=args.verify_original)
    if problems:
        print(json.dumps({'ok': False, '原归档被改动': problems}, ensure_ascii=False, indent=1))
        return 1
    print('自检：原归档未被触碰%s' % ('（含全量 sha256 复核）' if args.verify_original else ''))
    print(json.dumps({'ok': True, 'faults': str(builder.faults), 'notes': notes},
                     ensure_ascii=False, indent=1))
    return 0


# What every sample must make the validator say, and which face must catch it.
EXPECTED = {
    'good': 'PASS（对照组：未改动的好样本必须仍然 PASS）',
    'bad-missing-phonetic-track': 'FAIL，draft 面：缺必需轨道 text/phonetic',
    'bad-phonetic-start-shifted': 'FAIL，draft 面：音标段起点比时间线晚 100ms',
    'bad-word2-text-changed': 'FAIL，wordlist 面：第 2 个词与只读词表不符（timeline/SRT/草稿三者一起改，互相自证）',
    'bad-timeline-only-changed': 'FAIL，wordlist 面：只有 timeline 改了词，SRT/草稿未跟随',
    'bad-mix-truncated': 'FAIL，mix 面：mix.wav 样本数少于时间轴',
    'bad-mp4-tail-silenced': 'FAIL，audio_e2e 面：成品 MP4 尾部静音，末词中文阶段无可闻样本',
    'bad-timeline-unreadable': 'FAIL（不是崩溃）：timeline.json 不可读必须写出报告；accept_all 不得读旧报告冒充',
    'bad-draft-unreadable': 'FAIL，draft 面：draft_content.json 不可读',
    'root-missing-batch': 'FAIL，accept_all：--expect 里的范围没有对应批次',
    'root-extra-batch': 'FAIL，accept_all：发布了 --expect 之外的批次',
    'root-empty': 'FAIL，accept_all：空目录不是绿色',
    'root-duplicate-batch': 'FAIL，accept_all：同一范围出现两次',
    'root-stale-report': 'FAIL，accept_all：子验收器崩溃时旧 PASS 报告不得代表本次运行',
    'root-stale-pass-no-batch': 'FAIL，accept_all：没有批次时旧 PASS 报告不得冒充交付',
}

# name -> (sample directory below the faults root, expected verdict,
#          face(s) that must appear in report['failures'])
CASES = [
    ('good', 'good', 'PASS', ()),
    ('bad-missing-phonetic-track', 'bad-missing-phonetic-track', 'FAIL', ('draft',)),
    ('bad-phonetic-start-shifted', 'bad-phonetic-start-shifted', 'FAIL', ('draft',)),
    ('bad-word2-text-changed', 'bad-word2-text-changed', 'FAIL', ('wordlist',)),
    ('bad-timeline-only-changed', 'bad-timeline-only-changed', 'FAIL', ('wordlist',)),
    ('bad-mix-truncated', 'bad-mix-truncated', 'FAIL', ('mix',)),
    ('bad-mp4-tail-silenced', 'bad-mp4-tail-silenced', 'FAIL', ('audio_e2e',)),
    ('bad-timeline-unreadable', 'bad-timeline-unreadable', 'FAIL', ('timeline',)),
    ('bad-draft-unreadable', 'bad-draft-unreadable', 'FAIL', ('draft',)),
]

# Archive-level cases: (name, root below the faults root, --expect,
#                       expected verdict, failure key that must be present)
#
# Every archive case writes its summary through ``--json`` into a path that
# already holds a JSON file saying ``{"verdict": "PASS", "stale": true}``, so the
# whole group also proves that no run can hand back a previous run's verdict.
ARCHIVE_CASES = [
    ('root-missing-batch', 'root-missing-batch', '151-200,201-250', 'FAIL', 'missing'),
    ('root-extra-batch', 'root-extra-batch', '1-50', 'FAIL', 'unexpected'),
    ('root-empty', 'root-empty', '151-200', 'FAIL', 'batches'),
    ('root-duplicate-batch', 'root-duplicate-batch', '151-200', 'FAIL', 'duplicate'),
    ('root-stale-report', 'root-stale-report', '151-200', 'FAIL', 'validator'),
    ('root-stale-pass-no-batch', 'root-stale-pass-no-batch', '151-200', 'FAIL', 'batches'),
]

# The stale-report case needs a word list that does not exist, so the child
# validator dies *before* it can write a report - which is exactly the accident
# being reproduced.  The only acceptable outcome then is ``validator``: "no fresh
# report", not the PASS that is already lying at the report path.
STALE_REPORT_CASE = 'root-stale-report'


if __name__ == '__main__':
    sys.exit(main())

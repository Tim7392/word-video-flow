"""The slice renderer: bounds, per-slice captions, and pixels in every slice.

Slicing is the riskiest part of the renderer - a slice with the wrong caption
offset would silently show the wrong word, and one that lost them entirely would
look exactly like the bug that shipped three caption-less videos.  So these
tests check the pixels, not just the file sizes.
"""
from pathlib import Path
import subprocess
import tempfile
import unittest
import wave

from word_video.contracts import LessonSpec, SpeechAsset, WordEntry
from word_video.media import executable, run
from word_video.render import render_video, slice_bounds, slice_count
from word_video.render.ass import build_ass
from word_video.timing import build_timeline

FFMPEG = executable('ffmpeg')


def gray_frame(video, seconds, width=320, height=180):
    result = subprocess.run([str(a) for a in
                             [FFMPEG, '-v', 'error', '-nostdin', '-ss', '%.4f' % seconds,
                              '-i', str(video), '-frames:v', '1', '-vf',
                              'scale=%d:%d,format=gray' % (width, height),
                              '-f', 'rawvideo', '-']], capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.decode('utf-8', 'replace')[-400:])
    return result.stdout[:width * height]


def ink(video, background, seconds):
    """Fraction of pixels where the rendered frame differs from the bare背景."""
    left, right = gray_frame(video, seconds), gray_frame(background, seconds)
    changed = sum(1 for a, b in zip(left, right) if abs(a - b) > 24)
    return changed / max(1, len(left))


class SliceBoundsTests(unittest.TestCase):
    def test_bounds_cover_everything_without_gaps(self):
        for total, slices in ((1200, 2), (1000, 4), (10950, 8), (7, 1), (601, 3)):
            with self.subTest(total=total, slices=slices):
                bounds = slice_bounds(total, slices)
                self.assertEqual(slices, len(bounds))
                self.assertEqual(0, bounds[0][0])
                self.assertEqual(total, bounds[-1][1])
                for index, (start, end) in enumerate(bounds):
                    self.assertLess(start, end)
                    if index:
                        self.assertEqual(bounds[index - 1][1], start)

    def test_small_videos_are_not_sliced(self):
        class Tiny:
            total_frames = 600
        class Long:
            total_frames = 10950
        self.assertEqual(1, slice_count(Tiny()))
        self.assertGreater(slice_count(Long()), 1)


class SliceCaptionTests(unittest.TestCase):
    def test_offset_shifts_and_clips_events(self):
        """A slice's captions must be re-based to its own zero, nothing else."""
        word = WordEntry(1, 'apple', '/ˈæpəl/', 'n. 苹果', '苹果')
        lesson = LessonSpec([word], width=320, height=180, intro_s=0.2,
                            background='unused.mp4')
        asset = SpeechAsset(1, 'female', 'apple', 'unused.wav', .1, .1, 'test')
        manifest = build_timeline(lesson, [asset, SpeechAsset(1, 'male', 'apple', 'unused.wav', .1, .1, 'test'),
                                           SpeechAsset(1, 'chinese', '苹果', 'unused.wav', .1, .1, 'test')])
        whole = build_ass(manifest)
        word = manifest.words[0]
        self.assertIn('四级1500高频词', whole)
        self.assertIn('不积小流', whole)
        # A slice over the intro: the global title block is there, the word that
        # has not started yet is not.
        head = build_ass(manifest, offset_frames=0, frame_limit=10)
        self.assertIn('四级1500高频词', head)
        self.assertNotIn('apple', head)
        # A slice over the word: its own line is kept and re-based to the
        # slice's zero, and an event that ended before the slice is dropped
        # rather than shifted into it.
        start = word['start_frame']
        length_frames = manifest.total_frames - start
        tail = build_ass(manifest, offset_frames=start, frame_limit=length_frames)
        self.assertIn('apple', tail)
        self.assertIn('不积小流', tail)  # the footer spans the whole lesson
        # Every event now lives inside the slice's own window, and the block that
        # began before the slice starts at its zero.
        def seconds(stamp):
            hours, minutes, rest = stamp.split(':')
            return int(hours) * 3600 + int(minutes) * 60 + float(rest)

        length_s = length_frames / manifest.fps
        starts = []
        for line in tail.splitlines():
            if not line.startswith('Dialogue'):
                continue
            start_at = seconds(line.split(',')[1])
            end_at = seconds(line.split(',')[2])
            starts.append(start_at)
            self.assertGreaterEqual(start_at, 0.0, line)
            self.assertLessEqual(end_at, length_s + 0.02, line)
        self.assertEqual(0.0, min(starts))


class SliceRenderTests(unittest.TestCase):
    """A two-slice render must keep its captions in *both* halves."""

    def _lesson(self, root, background_seconds):
        background = root / ('bg-%d.mp4' % background_seconds)
        run([FFMPEG, '-v', 'error', '-nostdin', '-n', '-f', 'lavfi', '-i',
             'color=c=blue:s=320x180:r=60:d=%d' % background_seconds, '-c:v', 'libx264',
             '-pix_fmt', 'yuv420p', background])
        audio = root / 'speech.wav'
        with wave.open(str(audio), 'wb') as stream:
            stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(48000)
            stream.writeframes(b'\x01\x00' * 4800)
        words = [WordEntry(1, 'apple', '/ˈæpəl/', 'n. 苹果', '苹果'),
                 WordEntry(2, 'banana', '/bəˈnɑːnə/', 'n. 香蕉', '香蕉'),
                 WordEntry(3, 'cherry', '/ˈtʃeri/', 'n. 樱桃', '樱桃')]
        lesson = LessonSpec(words, width=320, height=180, intro_s=0.2,
                            background=str(background))
        assets = [SpeechAsset(entry.index, role,
                              entry.spoken_meaning if role == 'chinese' else entry.word,
                              str(audio), .1, .1, 'test')
                  for entry in words for role in ('female', 'male', 'chinese')]
        return build_timeline(lesson, assets), background

    def _frames(self, path):
        out = subprocess.run([str(a) for a in
                              [executable('ffprobe'), '-v', 'error', '-count_frames',
                               '-select_streams', 'v:0', '-show_entries',
                               'stream=nb_read_frames', '-of', 'csv=p=0', str(path)]],
                             capture_output=True, text=True)
        return int(out.stdout.strip())

    def test_captions_survive_in_every_slice(self):
        """Each half must paint its own words - a lost slice looks like the bug
        that shipped three caption-less videos."""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            # Longer than the timeline, so the slices have nothing to loop and a
            # reference frame exists for every comparison.
            manifest, background = self._lesson(root, 12)
            paths = render_video(manifest, root / 'video', slices=2)
            self.assertEqual(manifest.total_frames, self._frames(paths[0]))
            for word in (manifest.words[0], manifest.words[2]):
                at = (word['chinese_frame'] + word['end_frame']) / 2 / manifest.fps
                painted = ink(paths[0], background, at)
                self.assertGreater(painted, 0.01,
                                   'slice lost the captions at word %d (%.4f)'
                                   % (word['index'], painted))
            # The published ASS is the whole timeline, not a slice.
            published = Path(paths[1]).read_text(encoding='utf-8-sig')
            self.assertIn('不积小流', published)
            self.assertEqual(build_ass(manifest).count('Dialogue'),
                             published.count('Dialogue'))

    def test_background_shorter_than_timeline_is_looped_for_slices(self):
        """A slice that ran out of background would silently come up short."""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest, _ = self._lesson(root, 4)  # 6.95 s timeline on 4 s of背景
            self.assertGreater(manifest.total_frames / manifest.fps, 4)
            paths = render_video(manifest, root / 'video', slices=2)
            self.assertEqual(manifest.total_frames, self._frames(paths[0]))
            probe = subprocess.run([str(a) for a in
                                    [executable('ffprobe'), '-v', 'error', '-show_entries',
                                     'format=duration', '-of', 'csv=p=0', str(paths[0])]],
                                   capture_output=True, text=True)
            self.assertAlmostEqual(manifest.total_frames / manifest.fps,
                                   float(probe.stdout.strip()), delta=0.05)


if __name__ == '__main__':
    unittest.main()

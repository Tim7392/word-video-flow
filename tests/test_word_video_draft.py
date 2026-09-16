import json
from pathlib import Path
import shutil
import tempfile
import unittest

from word_video.contracts import LessonSpec,WordEntry,SpeechAsset
from word_video.media import run,executable,prepare_audio
from word_video.timing import build_timeline
from word_video.draft import (export_draft,relocate_draft,draft_state,
                              DraftEncryptedError,DraftCorruptError)
from word_video.validate import validate_draft


class DraftTests(unittest.TestCase):
    def test_editable_materials_loop_and_relocation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); bg=root/'short.mp4'; raw=root/'raw.wav'; prepared=root/'prepared.wav'
            run([executable('ffmpeg'),'-v','error','-f','lavfi','-i','color=c=blue:s=320x180:r=60:d=0.5','-c:v','libx264','-pix_fmt','yuv420p',bg])
            run([executable('ffmpeg'),'-v','error','-f','lavfi','-i','sine=frequency=440:duration=0.3',raw])
            original,rendered=prepare_audio(raw,prepared,1.25)
            word=WordEntry(51,'apple','[ˈæpəl]','n. 苹果','苹果')
            lesson=LessonSpec([word],width=320,height=180,background=str(bg))
            assets=[SpeechAsset(51,role,word.spoken_meaning if role=='chinese' else word.word,
                                str(prepared),original,rendered,'test') for role in ('female','male','chinese')]
            manifest=build_timeline(lesson,assets)
            draft=root/'new';export_draft(manifest,draft)
            report=validate_draft(draft,manifest)
            self.assertEqual(3,report['speech_segments'])
            data=json.loads((draft/'draft_content.json').read_text(encoding='utf-8'))
            background=next(x for x in data['tracks'] if x['name']=='背景')
            self.assertGreater(len(background['segments']),1)
            self.assertTrue(all(s['speed']==1 for t in data['tracks'] if t['type']=='audio' for s in t['segments']))
            with self.assertRaises(FileExistsError):export_draft(manifest,draft)
            copied=root/'moved';shutil.copytree(draft,copied);relocate_draft(copied)
            self.assertTrue(validate_draft(copied,manifest)['structural_ok'])

    def test_intro_track_keeps_the_draft_on_the_rendered_timeline(self):
        """The draft must show the same head as the MP4.

        The rendered video draws the background from frame 0 and the transparent
        countdown over it.  Putting the intro and the background on one track
        forced the background to start after the intro, so the draft and the MP4
        disagreed for the whole countdown.  The intro therefore owns a track.
        """
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);bg=root/'bg.mp4';raw=root/'raw.wav'
            run([executable('ffmpeg'),'-v','error','-f','lavfi','-i','color=c=blue:s=320x180:r=60:d=0.5',
                 '-c:v','libx264','-pix_fmt','yuv420p',bg])
            run([executable('ffmpeg'),'-v','error','-f','lavfi','-i','sine=frequency=440:duration=0.3',
                 '-ar','48000','-ac','1',raw])
            clip=root/'clear.mov'
            run([executable('ffmpeg'),'-v','error','-f','lavfi','-i',
                 'color=c=black:s=320x180:r=60:d=0.3,format=argb','-c:v','qtrle',
                 '-pix_fmt','argb',clip])
            word=WordEntry(1,'apple','/ˈæpəl/','n. 苹果','苹果')
            lesson=LessonSpec([word],width=320,height=180,background=str(bg),
                              intro={'video':str(clip)})
            assets=[SpeechAsset(1,role,'苹果' if role=='chinese' else 'apple',
                                str(raw),.1,.1,'test') for role in ('female','male','chinese')]
            manifest=build_timeline(lesson,assets)
            draft=root/'draft';export_draft(manifest,draft)
            data=json.loads((draft/'draft_content.json').read_text(encoding='utf-8'))
            tracks={t['name']:t for t in data['tracks'] if t['type']=='video'}
            self.assertEqual(0,tracks['背景']['segments'][0]['target_timerange']['start'])
            self.assertEqual(1,len(tracks['片头']['segments']))
            self.assertEqual(0,tracks['片头']['segments'][0]['target_timerange']['start'])
            # The intro must composite over the background, not under it.
            order=[t['name'] for t in data['tracks'] if t['type']=='video']
            self.assertLess(order.index('背景'),order.index('片头'))
            self.assertTrue(validate_draft(draft,manifest)['structural_ok'])

    def test_encrypted_draft_is_refused_untouched(self):
        """Jianying 11.4 rewrites the draft; patching it blind would destroy it."""
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'draft';root.mkdir()
            payload='DeIAqREcEg8BKs6xoSEsJ3JeYUe4f2bT0g8Ad6YM6g9FV4PfI9igLEB8aSF4'
            content=root/'draft_content.json'
            content.write_text(payload,encoding='utf-8')
            (root/'draft_meta_info.json').write_text('{}',encoding='utf-8')
            (root/'word_video_relocation.json').write_text('{}',encoding='utf-8')
            before=sorted(p.name for p in root.iterdir())
            with self.assertRaises(ValueError) as caught: relocate_draft(root)
            self.assertIsInstance(caught.exception,DraftEncryptedError)
            self.assertIn('encrypted',str(caught.exception))
            self.assertEqual(payload,content.read_text(encoding='utf-8'))
            self.assertEqual(before,sorted(p.name for p in root.iterdir()))
            report=draft_state(root)
            self.assertEqual('ENCRYPTED_BY_APP',report['state'])
            self.assertFalse(report['relocatable'])

    def test_corrupt_draft_is_reported_as_corrupt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'draft';root.mkdir()
            (root/'draft_content.json').write_text('{"tracks": [',encoding='utf-8')
            self.assertEqual('CORRUPT',draft_state(root)['state'])

    def test_plain_draft_state_is_relocatable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'draft';root.mkdir()
            (root/'draft_content.json').write_text('{"tracks": []}',encoding='utf-8')
            report=draft_state(root)
            self.assertEqual('PLAIN_JSON',report['state'])
            self.assertTrue(report['relocatable'])

    def test_a_prepared_file_is_measured_without_spawning_ffprobe(self):
        """The draft asks 150 times "is this file longer than its window?".

        Prepared speech is a PCM WAV this package wrote, so the answer is in the
        header: ffprobe was spawned once per audio item for it, measured at 22 s of a
        44 s draft stage on a 50-word lesson.  A file that is not a readable WAV still
        goes to ffprobe, so nothing else changes.
        """
        from unittest.mock import patch

        from word_video.draft import prepared_seconds
        from word_video.media import run, executable, duration

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wav = root / 'prepared.wav'
            run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-f', 'lavfi',
                 '-i', 'sine=frequency=440:duration=0.5:sample_rate=48000',
                 '-ac', '1', '-c:a', 'pcm_s16le', str(wav)])
            with patch('word_video.media.core.run') as spawned:
                seconds = prepared_seconds(wav)
            self.assertEqual(0.5, seconds)
            spawned.assert_not_called()
            self.assertAlmostEqual(duration(wav), seconds, places=6)

            # Not a WAV: the header cannot answer, so ffprobe still does.
            other = root / 'tone.mp3'
            run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-f', 'lavfi',
                 '-i', 'sine=frequency=440:duration=0.5:sample_rate=44100',
                 '-c:a', 'libmp3lame', '-b:a', '64k', str(other)])
            self.assertAlmostEqual(duration(other), prepared_seconds(other), delta=0.05)

if __name__=='__main__':unittest.main()

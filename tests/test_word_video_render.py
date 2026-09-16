from pathlib import Path
import tempfile
import unittest
import wave

from word_video.contracts import WordEntry,SpeechAsset,LessonSpec
from word_video.timing import build_timeline
from word_video.media import executable,run,prepare_audio
from word_video.render import render_video,RenderCancelled,video_graph
from word_video.render.mix import build_mix
from word_video.validate import validate_video


class IntroGraphTests(unittest.TestCase):
    """An intro clip must not cost the captions.

    The countdown is composited first and the captions are burnt on afterwards,
    so the subtitle stage has to be in the graph both for a transparent and for
    an opaque intro.  Leaving it out produced a video with a countdown and no
    words at all, which no file-level check could see.
    """

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.bg=self.root/'bg.mp4'
        run([executable('ffmpeg'),'-v','error','-f','lavfi','-i','color=c=blue:s=320x180:r=60:d=0.5',
             '-c:v','libx264','-pix_fmt','yuv420p',self.bg])
        self.raw=self.root/'raw.wav'
        run([executable('ffmpeg'),'-v','error','-f','lavfi','-i','sine=frequency=440:duration=0.3',
             '-ar','48000','-ac','1',self.raw])

    def _manifest(self,clip):
        word=WordEntry(1,'apple','/ˈæpəl/','n. 苹果','苹果')
        lesson=LessonSpec([word],width=320,height=180,background=str(self.bg),
                          intro={'video':str(clip)})
        assets=[SpeechAsset(1,role,'苹果' if role=='chinese' else 'apple',str(self.raw),.1,.1,'test')
                for role in ('female','male','chinese')]
        return build_timeline(lesson,assets)

    def _clip(self,name,alpha):
        path=self.root/name
        run([executable('ffmpeg'),'-v','error','-f','lavfi','-i',
             'color=c=black:s=320x180:r=60:d=0.3,format=%s'%('argb' if alpha else 'yuv420p'),
             '-c:v','qtrle' if alpha else 'libx264','-pix_fmt','argb' if alpha else 'yuv420p',
             str(path)])
        return path

    def test_transparent_intro_still_burns_captions(self):
        manifest=self._manifest(self._clip('clear.mov',True))
        self.assertGreater(manifest.intro_frames,0)
        graph=video_graph(manifest,'scale=320:180')
        self.assertIn('overlay=',graph)
        self.assertIn('ass=filename=captions.ass',graph)
        # The caption stage consumes the composite and feeds the encoder, so a
        # countdown can never be painted over a word.
        self.assertIn('[ov]ass=',graph)
        self.assertTrue(graph.endswith('[vout]'))

    def test_opaque_intro_still_burns_captions(self):
        manifest=self._manifest(self._clip('black.mp4',False))
        graph=video_graph(manifest,'scale=320:180')
        self.assertIn('blend=all_mode=lighten',graph)
        self.assertIn('[ov]ass=filename=captions.ass',graph)
        self.assertTrue(graph.endswith('[vout]'))


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.raw=self.root/'raw.wav'
        with wave.open(str(self.raw),'wb') as out:
            out.setnchannels(1);out.setsampwidth(2);out.setframerate(48000);out.writeframes(b'\x01\x00'*4800)
        self.bg=self.root/'short.mp4'
        run([executable('ffmpeg'),'-v','error','-f','lavfi','-i','color=s=320x180:r=60:d=0.5','-c:v','libx264',self.bg])
        word=WordEntry(1,'apple','/ˈæpəl/','n. 苹果','苹果')
        lesson=LessonSpec([word],width=320,height=180,intro_s=.2,background=str(self.bg))
        assets=[SpeechAsset(1,role,'苹果' if role=='chinese' else 'apple',str(self.raw),.1,.1,'test') for role in ('female','male','chinese')]
        self.manifest=build_timeline(lesson,assets)

    def test_mix_exact_samples_and_no_overwrite(self):
        path=self.root/'mix.wav';build_mix(self.manifest,path)
        with wave.open(str(path),'rb') as source:
            self.assertEqual(self.manifest.total_frames*800,source.getnframes())
            self.assertEqual(b'\0'*(self.manifest.intro_frames*800*2),source.readframes(self.manifest.intro_frames*800))
            self.assertEqual(b'\x01\x00',source.readframes(1))
        with self.assertRaises(FileExistsError):build_mix(self.manifest,path)

    def test_silent_intro_clip_mixes_and_stays_silent(self):
        """An intro clip with no audio track is valid input, not a failed job.

        Before M0 the mixer handed the clip to ffmpeg and asked for an audio
        stream that was not there: "Output file does not contain any stream",
        and the whole batch failed at the mix stage.  The draft side had been
        fixed, this side had not.
        """
        silent=self.root/'silent-intro.mp4'
        run([executable('ffmpeg'),'-v','error','-nostdin','-n','-f','lavfi','-i',
             'color=c=black:s=320x180:r=60:d=0.3','-an','-c:v','libx264',
             '-pix_fmt','yuv420p',silent])
        word=WordEntry(1,'apple','/ˈæpəl/','n. 苹果','苹果')
        lesson=LessonSpec([word],width=320,height=180,background=str(self.bg),
                          intro={'video':str(silent)})
        assets=[SpeechAsset(1,role,'苹果' if role=='chinese' else 'apple',
                            str(self.raw),.1,.1,'test')
                for role in ('female','male','chinese')]
        manifest=build_timeline(lesson,assets)
        self.assertGreater(manifest.intro_frames,0)
        self.assertTrue(manifest.intro_video.endswith('silent-intro.mp4'))
        path=self.root/'silent-mix.wav'
        build_mix(manifest,path)
        with wave.open(str(path),'rb') as source:
            self.assertEqual(manifest.total_frames*800,source.getnframes())
            self.assertEqual(b'\0'*(manifest.intro_frames*800*2),
                             source.readframes(manifest.intro_frames*800))
            self.assertEqual(b'\x01\x00',source.readframes(1))

    def test_short_video_and_cancel(self):
        output=self.root/'video';paths=render_video(self.manifest,output)
        self.assertTrue(validate_video(paths[0],self.manifest)['structural_ok'])
        with self.assertRaises(FileExistsError):render_video(self.manifest,output)
        with self.assertRaises(RenderCancelled):render_video(self.manifest,self.root/'cancel',cancel=lambda:True)
        self.assertFalse((self.root/'cancel/video.mp4').exists())

    def test_cancel_after_progress_does_not_publish(self):
        flag=[False]
        def progress(value):
            if value['stage']=='render': flag[0]=True
        self.manifest.total_frames=600
        with self.assertRaises(RenderCancelled):
            render_video(self.manifest,self.root/'mid-cancel',progress=progress,cancel=lambda:flag[0])
        self.assertFalse((self.root/'mid-cancel/video.mp4').exists())

if __name__=='__main__':unittest.main()

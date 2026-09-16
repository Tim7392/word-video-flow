"""Font resolution must degrade honestly instead of aborting a render.

Jianying purges ``Cache/effect`` on its own schedule, which previously deleted
the two reference faces and made every render and draft export fail.  These
tests pin the documented behaviour: pick the reference face when it exists,
otherwise substitute a readable face, record the substitution, and only fail
when no face at all can be found.

The reference faces come and go on this machine (Jianying re-downloads them), so
no test may depend on their current presence.  Tests that need a specific state
build it explicitly inside a temporary HOME.
"""
import contextlib
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch
import unittest

from word_video import template
from word_video.contracts import LessonSpec,SpeechAsset,WordEntry
from word_video.media import executable,run
from word_video.render.ass import build_ass
from word_video.render import render_video
from word_video.timing import build_timeline

# A face that exists on Windows and is not the reference face for any role.
SUBSTITUTE_FONT = Path('C:/Windows/Fonts/tahoma.ttf')


def clear_font_cache():
    template._FONT_CACHE=None


def _substitute_names(role):
    """Candidate file names for a role, excluding the preferred reference face."""
    return [name for name, _ in template._FONT_CANDIDATES[role][1:]]


@contextlib.contextmanager
def fake_home(root):
    """Point ''Path.home()'' inside the template module at ``root``.

    The effect-cache branch of ``resolve_fonts`` only ever looks under
    ``Path.home()/AppData/...``, so this hides the real Jianying cache without
    touching it.
    """
    class FakePath(type(Path())):
        @classmethod
        def home(cls):
            return Path(root)

    with patch.object(template, 'Path', FakePath):
        yield


class FontResolutionTests(unittest.TestCase):
    def setUp(self):
        clear_font_cache()
        self.addCleanup(clear_font_cache)

    def test_report_names_cover_every_role(self):
        report=template.font_report()
        self.assertEqual(set(template.FONT_ROLES),set(report))
        for role,item in report.items():
            self.assertIn(item['source'],('preferred','substitute','missing'))
            if item['source']!='missing':
                self.assertTrue(Path(item['path']).is_file(),role)

    def test_preferred_face_wins_when_present(self):
        """Reference faces are used when they exist; that is the required look."""
        with tempfile.TemporaryDirectory() as home:
            reference=template._FONT_CANDIDATES['heading'][0][0]
            target=Path(home)/'AppData/Local/Microsoft/Windows/Fonts'
            target.mkdir(parents=True)
            shutil.copy2(SUBSTITUTE_FONT,target/reference)
            with fake_home(home):
                clear_font_cache()
                report=template.font_report()
                self.assertEqual('preferred',report['heading']['source'])
                self.assertEqual(reference,report['heading']['name'])

    def test_substitution_is_recorded_not_hidden(self):
        """With the reference face absent, a substitute is used and named."""
        with tempfile.TemporaryDirectory() as home:
            target=Path(home)/'AppData/Local/Microsoft/Windows/Fonts'
            target.mkdir(parents=True)
            # Every non-reference candidate exists, so the role must substitute
            # (rather than report missing) and must say which file it chose.
            for name in _substitute_names('heading'):
                shutil.copy2(SUBSTITUTE_FONT,target/name)
            with fake_home(home):
                original_roots=template._font_roots
                template._font_roots=lambda:[target]
                self.addCleanup(setattr,template,'_font_roots',original_roots)
                clear_font_cache()
                report=template.font_report()
                self.assertEqual('substitute',report['heading']['source'])
                self.assertIn(report['heading']['name'],_substitute_names('heading'))
                # The family name is read from the file itself, not from the name.
                self.assertEqual('Tahoma',report['heading']['family'])
                self.assertNotEqual(report['heading']['name'],
                                    template._FONT_CANDIDATES['heading'][0][0])

    def test_unusable_override_falls_back_to_resolved(self):
        with tempfile.TemporaryDirectory() as home:
            target=Path(home)/'Fonts';target.mkdir(parents=True)
            for role in ('bold','heading'):
                for name in _substitute_names(role):
                    shutil.copy2(SUBSTITUTE_FONT,target/name)
            with fake_home(home):
                original_roots=template._font_roots
                template._font_roots=lambda:[target]
                self.addCleanup(setattr,template,'_font_roots',original_roots)
                clear_font_cache()
                styles=template.default_styles({'bold':'C:/definitely/missing-font.ttf'})
                self.assertNotEqual(styles['english']['font'],'C:/definitely/missing-font.ttf')
                self.assertTrue(Path(styles['english']['font']).is_file())

    def test_no_available_face_fails_loudly(self):
        with tempfile.TemporaryDirectory() as empty:
            empty=Path(empty)
            with fake_home(empty):
                original_roots=template._font_roots
                template._font_roots=lambda:[empty]
                self.addCleanup(setattr,template,'_font_roots',original_roots)
                clear_font_cache()
                self.assertEqual({},template.resolve_fonts())
                styles=template.default_styles()
                self.assertTrue(all(styles[name]['font'] is None for name in styles))
                manifest=type('Manifest',(),{'styles':{}})()
                with self.assertRaises(ValueError):build_ass(manifest)

    def test_default_styles_never_return_missing_paths(self):
        for name,style in template.default_styles().items():
            font=style.get('font')
            self.assertTrue(font,msg=name)
            self.assertTrue(Path(font).is_file(),msg=name)


class FontFallbackRenderTests(unittest.TestCase):
    """A real render must still produce a video with only substitute faces."""

    def setUp(self):
        clear_font_cache()
        self.addCleanup(clear_font_cache)
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.raw=self.root/'raw.wav'
        run([executable('ffmpeg'),'-v','error','-f','lavfi','-i',
             'sine=frequency=440:duration=0.1','-ar','48000','-ac','1',self.raw])
        self.bg=self.root/'bg.mp4'
        run([executable('ffmpeg'),'-v','error','-f','lavfi','-i',
             'color=c=navy:s=320x180:r=60:d=0.5','-c:v','libx264','-pix_fmt','yuv420p',self.bg])

    def test_render_with_substituted_fonts(self):
        # Prefer the substitute path by removing the preferred face from the table.
        original=template._FONT_CANDIDATES['bold']
        template._FONT_CANDIDATES['bold']=original[1:]
        self.addCleanup(template._FONT_CANDIDATES.__setitem__,'bold',original)
        clear_font_cache()
        resolved=template.resolve_fonts()
        self.assertIn('bold',resolved,msg='no usable font on this machine: %r'%(
            template._font_roots(),))
        word=WordEntry(1,'apple','/ˈæpəl/','n. 苹果','苹果')
        lesson=LessonSpec([word],width=320,height=180,intro_s=.2,
                          background=str(self.bg),styles=template.default_styles())
        assets=[SpeechAsset(1,role,'苹果' if role=='chinese' else 'apple',
                            str(self.raw),.1,.1,'test') for role in ('female','male','chinese')]
        manifest=build_timeline(lesson,assets)
        document=build_ass(manifest)
        self.assertIn('apple',document)
        video=render_video(manifest,self.root/'video')
        self.assertTrue(Path(video[0]).is_file())
        self.assertTrue(any(Path(p).name=='captions.ass' for p in video))


if __name__=='__main__':
    unittest.main()

"""Pure tests for the ASS exporter (no filesystem, no ffmpeg)."""
import unittest

from word_video.contracts import TimelineManifest
from word_video.render import ass
from word_video import template
from word_video.template import text_events


def make_manifest(**overrides):
    values = dict(
        schema_version=1, title='Title', subtitle='Sub', footer='Foot',
        first_index=1, last_index=1, fps=60, width=1920, height=1080,
        speed=1.25, intro_frames=120, total_frames=600, background='',
        intro_audio='', intro_video='', video_codec='h264', styles={},
        words=[dict(index=1, word='hello', phonetic='həˈloʊ', meaning='你好',
                    spoken_meaning='你好', start_frame=120, male_frame=200,
                    chinese_frame=260, end_frame=600)],
        audio=[],
    )
    values.update(overrides)
    return TimelineManifest(**values)


class NumTests(unittest.TestCase):
    def test_frame_to_centisecond_half_up(self):
        self.assertEqual(ass._cs(1, 8), 13)   # 12.5 -> 13
        self.assertEqual(ass._cs(4, 8), 50)
        self.assertEqual(ass._cs(1, 60), 2)   # 1.66 -> 2

    def test_timestamp(self):
        self.assertEqual(ass._timestamp(13), '0:00:00.13')
        self.assertEqual(ass._timestamp(366105), '1:01:01.05')


class EscapeTests(unittest.TestCase):
    def test_escapes_backslash_braces_newline(self):
        self.assertEqual(ass._escape_text('a{b}c\\d\ne'), 'a\\{b\\}c\\\\d\\Ne')

    def test_text_cannot_inject_override_tags(self):
        out = build_with_word('{\\p1}m 0 0 l 1 1{\\p0}')
        self.assertNotIn('{\\p1}', out)
        self.assertIn('\\{\\\\p1\\}', out)


class BuildTests(unittest.TestCase):
    def test_header_resolution_and_footer(self):
        text = ass.build_ass(make_manifest())
        self.assertIn('[Script Info]', text)
        self.assertIn('ScriptType: v4.00+', text)
        self.assertIn('PlayResX: 1920', text)
        self.assertIn('PlayResY: 1080', text)
        self.assertIn('[V4+ Styles]', text)
        self.assertIn('[Events]', text)
        self.assertTrue(text.endswith('\n'))

    def test_default_style_scaled_and_font_named(self):
        text = ass.build_ass(make_manifest())
        # The emitted family must be the one recorded inside the resolved file:
        # a hardcoded name would silently stop matching after a font substitute.
        family = template.font_name(template.resolve_fonts()['bold']['path'])
        self.assertIn('Style: english,%s,225,' % family, text)
        self.assertIn('Style: countdown,%s,337.5,' % family, text)

    def test_default_family_matches_the_resolved_file(self):
        from word_video.template import resolve_fonts, font_name
        text = ass.build_ass(make_manifest())
        for style, role in (('english', 'bold'), ('phonetic', 'arial'),
                            ('meaning', 'heavy')):
            line = [r for r in text.splitlines()
                    if r.startswith('Style: %s,' % style)][0]
            self.assertEqual(line.split(',')[1],
                             font_name(resolve_fonts()[role]['path']))

    def test_style_override_wins(self):
        manifest = make_manifest(styles={'english': dict(size=600, font_name='Arial',
                                                         color='#FF0000', bold=False)})
        text = ass.build_ass(manifest)
        self.assertIn('Style: english,Arial,300,&H000000FF&,', text)
        line = [row for row in text.splitlines() if row.startswith('Style: english')][0]
        self.assertIn(',0,0,0,100,100,0,0,1,0,2.5,5,0,0,0,1', line)

    def test_position_uses_normalized_center(self):
        manifest = make_manifest(styles={'english': dict(x=0.25, y=0.5)})
        text = ass.build_ass(manifest)
        self.assertIn('{\\an5\\pos(480,540)}hello', text)

    def test_one_dialogue_per_template_event(self):
        manifest = make_manifest()
        text = ass.build_ass(manifest)
        dialogues = [r for r in text.splitlines() if r.startswith('Dialogue:')]
        self.assertEqual(len(dialogues), len(text_events(manifest)))

    def test_countdown_pulse_only_on_countdown(self):
        text = ass.build_ass(make_manifest(styles={'countdown':{'animation':'candidate_pulse'}}))
        countdown = [r for r in text.splitlines()
                     if r.startswith('Dialogue:') and ',countdown,' in r]
        self.assertEqual(len(countdown), 2)
        for row in countdown:
            self.assertIn('\\fscx125\\fscy125\\alpha&HFF&', row)
            self.assertIn('\\t(0,150,\\fscx100\\fscy100\\alpha&H00&)', row)
        english = [r for r in text.splitlines()
                   if r.startswith('Dialogue:') and ',english,' in r][0]
        self.assertNotIn('\\fscx125', english)

    def test_default_does_not_substitute_a_generic_animation(self):
        self.assertNotIn('\\fscx125', ass.build_ass(make_manifest()))

    def test_event_timing_uses_frames(self):
        text = ass.build_ass(make_manifest())
        english = [r for r in text.splitlines()
                   if r.startswith('Dialogue:') and ',english,' in r][0]
        self.assertTrue(english.startswith('Dialogue: 0,0:00:02.00,0:00:10.00,english,'))


def build_with_word(word):
    return ass.build_ass(make_manifest(words=[dict(
        index=1, word=word, phonetic='p', meaning='m', spoken_meaning='m',
        start_frame=120, male_frame=120, chinese_frame=120, end_frame=600)]))


if __name__ == '__main__':
    unittest.main()

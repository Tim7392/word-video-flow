"""The intro as project data: one role, one resolver, media-derived length.

Before this, a lesson with a real countdown could only be produced by passing
``intro_video``/``intro_audio`` to the exporter: the same ``project.json`` plus a
different parameter made a different film, and the member could neither see nor
select the intro.  These tests pin the replacement:

* the intro is a clip of the project (role ``intro``) and the plan carries its
  stage — media asset, frame length, and *which file sounds*;
* its length comes from that media, never from the ``intro_s`` constant, so
  "template says 2.0 s, clip runs 1.867 s" cannot both be true;
* the sound verdict is the one ``word_video.media.resolve_intro_audio`` returns —
  checked here against the resolver itself on real files, not re-judged;
* ``wv-project@1`` documents still load and behave exactly as before.
"""
from dataclasses import replace
import math

import pytest

from test_wv_media_intro import _clip, _extract
from test_wv_project_golden import golden_base, golden_media, golden_project, golden_record

from word_video import timing
from word_video.application import (MoveClip, apply, instantiate, measure_intro)
from word_video.contracts import SpeechAsset, WordEntry
from word_video.domain import (DEFAULT_LESSON_TEMPLATE, INTRO_ROLE, SCHEMA, SCHEMA_V1,
                               IntroMeasurement, IntroMediaError, MediaSlice, Project,
                               SchemaError, TeachingOverlapError, TimeExpr, cue_plan,
                               render_plan, solve)
from word_video.domain.rhythm import intro_ticks
from word_video.media import resolve_intro_audio
from word_video.timing import build_timeline

INTRO_SECONDS = 1.867
INTRO_FRAMES = 113                      # ceil(1.867 * 60); the constant 2.0 s is 120
INTRO_TICKS = INTRO_FRAMES * 12000      # 1356000
SECOND = 720000
FALLBACK = 'D:/fixtures/intro-fallback.wav'
VIDEO = 'D:/fixtures/intro.mp4'
SILENT = 'SILENT_CLIP_NO_INTRO_AUDIO'


def measurement(seconds=INTRO_SECONDS, *, asset=VIDEO, sound_asset=VIDEO,
                sound_source='FROM_CLIP', from_clip=True, picture=None):
    return IntroMeasurement(asset_id=asset, seconds=seconds, sound_asset=sound_asset,
                            sound_source=sound_source, from_clip=from_clip,
                            picture_seconds=seconds if picture is None else picture)


def intro_project(seconds=INTRO_SECONDS, *, asset=VIDEO, sound_asset=VIDEO,
                  sound_source='FROM_CLIP', from_clip=True, fallback='',
                  duration_ticks=None, base=None, template=None):
    """A golden lesson plus an intro layer, without touching the file system."""
    template = template or replace(DEFAULT_LESSON_TEMPLATE, intro=True)
    project = instantiate(
        template, (golden_record(),), golden_media(),
        base if base is not None else golden_base(intro_s=2.0),
        intro=MediaSlice(asset, 0, 112), intro_audio=fallback,
        intro_measure=measurement(seconds, asset=asset, sound_asset=sound_asset,
                                  sound_source=sound_source, from_clip=from_clip))
    if duration_ticks is not None:
        clip = replace(project.clip('layer.intro'), duration_ticks=duration_ticks)
        project = project.with_clips(tuple(clip if item.id == clip.id else item
                                           for item in project.clips))
    return project


@pytest.fixture(scope='module')
def media_files(tmp_path_factory):
    """The three tiny real files the resolver cases need, encoded once.

    Real media (not stubs) is what makes "the plan says what the resolver decided"
    mean something; encoding them per test would cost more than the whole suite
    saves, so they are built once for the module.
    """
    root = tmp_path_factory.mktemp('intro-media')
    return {'talking': _clip(root, 'talking.mp4', 0.4, level=15000),
            'quiet': _clip(root, 'quiet.mp4', 0.3),
            'fallback': _extract(root, 'fallback', 0.25, 9000)}


# ---------------------------------------------------------------------------
# Versioning: the old document keeps working
# ---------------------------------------------------------------------------
def test_a_v1_document_still_loads_and_solves_identically():
    v2 = golden_project()
    v1 = v2.with_schema(SCHEMA_V1)
    document = v1.to_dict()
    assert document['schema'] == SCHEMA_V1
    # Nothing the intro added leaks into a document that has no intro layer.
    assert all('audio_asset' not in clip for clip in document['clips'])
    loaded = Project.from_dict(document)
    assert loaded.schema == SCHEMA_V1
    assert loaded.to_dict() == document
    assert loaded.clips == v1.clips and loaded.records == v1.records
    # Same plan before and after the version field changed: @1 is compat only.
    media = golden_media()
    steps = lambda plan: [(item.clip_id, item.start_ticks, item.end_ticks)
                          for item in plan.video + plan.audio]
    assert steps(render_plan(loaded, media)) == steps(render_plan(v2, media))
    assert solve(loaded, media).render.total_ticks == solve(v2, media).render.total_ticks
    # A revision we do not know is still refused instead of half-read.
    document['schema'] = 'wv-project@4'
    with pytest.raises(SchemaError):
        Project.from_dict(document)


def test_a_v1_document_cannot_carry_an_intro_layer():
    project = intro_project()
    with pytest.raises(SchemaError) as error:
        project.with_schema(SCHEMA_V1)
    assert error.value.code == 'SCHEMA'
    assert 'intro' in error.value.message


def test_creating_the_intro_layer_promotes_the_document_to_v2():
    v1_base = golden_base(intro_s=2.0).with_schema(SCHEMA_V1)
    assert v1_base.schema == SCHEMA_V1
    project = intro_project(base=v1_base)
    assert project.schema == SCHEMA          # the layer is what @2 exists for
    assert project.clip('layer.intro') is not None
    # A lesson without an intro layer keeps the revision it was loaded with.
    assert golden_project().with_schema(SCHEMA_V1).schema == SCHEMA_V1


# ---------------------------------------------------------------------------
# The length comes from the media
# ---------------------------------------------------------------------------
def test_intro_stage_length_comes_from_the_media_not_from_intro_s():
    project = intro_project()
    clip = project.clip('layer.intro')
    assert project.intro_s == 2.0                     # the template's constant
    assert clip.start == TimeExpr.at(0)
    assert clip.duration_ticks == INTRO_TICKS         # the clip's own 1.867 s
    assert project.intro_ticks == 120 * 12000         # what intro_s alone would say
    plan = render_plan(project, golden_media(), measurement())
    intro = plan.intro_item
    assert (intro.clip_id, intro.start_ticks, intro.end_ticks) == \
        ('layer.intro', 0, INTRO_TICKS)
    assert intro.source.asset_id == VIDEO
    assert intro.duration_ticks == INTRO_TICKS
    # The body starts where the intro ended, on the frame grid, and the SRTs agree.
    # Golden stage lengths: female 1.0 s, male 0.8 s, chinese 2.7 s.
    assert [item.start_ticks for item in plan.audio] == \
        [INTRO_TICKS, INTRO_TICKS + 720000, INTRO_TICKS + 1296000]
    cues = cue_plan(project, golden_media(), measurement())
    assert cues.by_track()['02'][0].start_ticks == INTRO_TICKS
    assert cues.total_ticks == plan.total_ticks


def test_a_silent_clip_takes_its_length_from_the_picture():
    project = intro_project(seconds=1.5, sound_asset='', sound_source=SILENT)
    plan = render_plan(project, golden_media(),
                       measurement(1.5, sound_asset='', sound_source=SILENT,
                                   from_clip=False))
    intro = plan.intro_item
    assert intro.end_ticks == 90 * 12000              # ceil(1.5 * 60)
    assert intro.sound.silent is True
    assert intro.sound.source == SILENT
    # Silence cannot make a lesson ambiguous, so nothing is reported for it.
    assert [conflict.code for conflict in plan.conflicts] == []


def test_an_external_audio_fallback_is_recorded_with_its_verdict():
    project = intro_project(seconds=0.4, sound_asset=FALLBACK,
                            sound_source='FROM_INTRO_AUDIO', from_clip=False,
                            fallback=FALLBACK)
    plan = render_plan(project, golden_media(),
                       measurement(0.4, sound_asset=FALLBACK,
                                   sound_source='FROM_INTRO_AUDIO', from_clip=False))
    sound = plan.intro_item.sound
    assert (sound.asset_id, sound.fallback_asset, sound.source) == \
        (FALLBACK, FALLBACK, 'FROM_INTRO_AUDIO')
    assert sound.silent is False


def test_a_trimmed_intro_keeps_its_verdict_but_uses_the_users_length():
    project = intro_project(duration_ticks=60 * 12000)
    plan = render_plan(project, golden_media(), measurement())
    assert plan.intro_item.end_ticks == 720000        # the user's 1.0 s
    assert plan.intro_item.sound.source == 'FROM_CLIP'


# ---------------------------------------------------------------------------
# Refusals: structured, with the object they are about
# ---------------------------------------------------------------------------
def test_a_missing_measurement_is_refused_with_the_clip_it_is_about():
    project = intro_project()
    with pytest.raises(IntroMediaError) as error:
        render_plan(project, golden_media())
    assert error.value.code == 'INTRO_MEDIA'
    assert error.value.path == 'clip:layer.intro'
    assert 'measure' in error.value.hint


def test_a_stale_measurement_is_refused():
    project = intro_project()
    stale = IntroMeasurement(asset_id='D:/fixtures/another-intro.mp4', seconds=1.0)
    with pytest.raises(IntroMediaError) as error:
        render_plan(project, golden_media(), stale)
    assert error.value.code == 'INTRO_MEDIA'
    assert 'another-intro' in error.value.message
    # A measurement for a lesson that has no intro layer is a caller bug too.
    with pytest.raises(IntroMediaError) as error:
        render_plan(golden_project(), golden_media(), stale)
    assert error.value.path == 'project'


def test_unusable_intro_media_is_a_structured_refusal(tmp_path):
    with pytest.raises(IntroMediaError) as error:
        measure_intro(str(tmp_path / 'not-there.mp4'), clip_id='layer.intro')
    assert error.value.code == 'INTRO_MEDIA'
    assert error.value.path == 'clip:layer.intro'
    assert error.value.hint
    # No video asset named at all is refused the same way, not with a KeyError.
    with pytest.raises(IntroMediaError):
        measure_intro('')


# ---------------------------------------------------------------------------
# One resolver: the plan says what resolve_intro_audio decided
# ---------------------------------------------------------------------------
def test_the_measurement_echoes_the_single_resolver_on_real_media(media_files):
    talking = media_files['talking']
    quiet = media_files['quiet']
    fallback = media_files['fallback']
    for video, fallback_audio in ((talking, fallback), (quiet, fallback), (quiet, '')):
        choice = resolve_intro_audio(video, None, fallback_audio or None)
        measured = measure_intro(video, fallback_audio=fallback_audio)
        assert measured.sound_asset == str(choice.path), video
        assert measured.sound_source == choice.source, video
        assert measured.from_clip == choice.from_clip, video
        assert measured.silent == choice.silent, video
        if choice.silent:
            # Silent: the stage is the picture, which is what the engine reserves.
            assert measured.seconds > 0
            assert math.isclose(measured.picture_seconds, measured.seconds, rel_tol=1e-9)
        else:
            assert math.isclose(measured.seconds, choice.duration_s, rel_tol=1e-9)


def test_the_intro_frames_match_the_verified_engine_on_real_media(media_files):
    """Same media, same frames: the project reserves what the engine reserved."""
    cases = ((media_files['talking'], 'sound'), (media_files['quiet'], 'picture'))
    for clip, method in cases:
        measured = measure_intro(clip)
        lesson = timing.LessonSpec(entries=[WordEntry(index=1, word='promise',
                                                      phonetic='p', meaning='m',
                                                      spoken_meaning='承诺')],
                                   fps=60, intro={'video': clip})
        frames, _ = timing.intro_frames_for(lesson)
        assert measured.seconds > 0, method
        assert frames == math.ceil(measured.seconds * 60), method
        # ... and the frame grid the project model uses agrees with that number.
        assert intro_ticks(measured.seconds, 60) == frames * 12000, method


def test_a_measured_intro_reaches_the_timeline_the_engine_builds(media_files):
    """The media path end to end: measure, place the body, build the manifest."""
    video = media_files['talking']
    measured = measure_intro(video)
    project = intro_project(seconds=measured.seconds, asset=video,
                            sound_asset=measured.sound_asset,
                            sound_source=measured.sound_source)
    plan = render_plan(project, golden_media(), measured)
    frames = plan.intro_item.end_ticks // 12000
    entries = [WordEntry(index=1, word='promise', phonetic='pr', meaning='n. 承诺',
                         spoken_meaning='承诺诺言誓言')]
    assets = []
    for role, seconds in (('female', 1.15), ('male', 0.85), ('chinese', 3.275)):
        text = entries[0].spoken_meaning if role == 'chinese' else entries[0].word
        assets.append(SpeechAsset(word_index=1, role=role, text=text, path='unused.wav',
                                  duration_s=seconds, rendered_duration_s=0.0))
    lesson = timing.LessonSpec(entries=entries, fps=60, intro={'video': video})
    manifest = build_timeline(lesson, assets)
    assert manifest.intro_frames == frames               # the clip's media, both ways
    assert manifest.intro_frames != 120                  # never the intro_s constant
    # The lesson body itself is exactly what it would be with no intro slot at all,
    # so the intro can only ever add its own stage in front of it.
    body = build_timeline(timing.LessonSpec(entries=entries, fps=60, intro_s=0), assets)
    assert body.total_frames == 270                      # 60 + 48 + 162 frames
    assert manifest.total_frames - frames == body.total_frames
    assert manifest.words[0]['start_frame'] == frames


# ---------------------------------------------------------------------------
# Template, and the teaching rules that must not move
# ---------------------------------------------------------------------------
def test_the_template_creates_the_intro_layer_once():
    project = intro_project()
    assert [clip.id for clip in project.clips][0] == 'layer.intro'
    assert [clip.role for clip in project.clips][0] == INTRO_ROLE
    # The paint order puts the intro over the background and under the text.
    plan = render_plan(project, golden_media(), measurement())
    assert [item.role for item in plan.video][:3] == ['intro', 'title', 'subtitle']
    # A second instantiation is refused: expansion happens once, at creation.
    with pytest.raises(SchemaError):
        instantiate(replace(DEFAULT_LESSON_TEMPLATE, intro=True),
                    (golden_record(),), golden_media(), project,
                    intro=MediaSlice(VIDEO, 0, 112), intro_measure=measurement())


def test_the_intro_layer_needs_media_and_a_template_that_declares_it():
    records, media = (golden_record(),), golden_media()
    with pytest.raises(SchemaError) as error:
        instantiate(replace(DEFAULT_LESSON_TEMPLATE, intro=True), records, media,
                    golden_base())
    assert 'no intro media' in error.value.message
    with pytest.raises(SchemaError) as error:
        instantiate(DEFAULT_LESSON_TEMPLATE, records, media, golden_base(),
                    intro=MediaSlice(VIDEO, 0, 112), intro_measure=measurement())
    assert 'no intro layer' in error.value.message
    with pytest.raises(IntroMediaError) as error:
        instantiate(replace(DEFAULT_LESSON_TEMPLATE, intro=True), records, media,
                    golden_base(), intro=MediaSlice(VIDEO, 0, 112))
    assert error.value.path == 'clip:layer.intro'
    with pytest.raises(IntroMediaError):
        instantiate(replace(DEFAULT_LESSON_TEMPLATE, intro=True), records, media,
                    golden_base(), intro=MediaSlice(VIDEO, 0, 112),
                    intro_measure=IntroMeasurement(asset_id='other.mp4', seconds=1.0))


def test_the_teaching_rules_still_hold_with_an_intro():
    project = intro_project()
    solution = solve(project, golden_media(), measurement())
    # The word block is untouched, only moved later by the intro's own length.
    assert solution.render.audio[0].start_ticks == INTRO_TICKS
    assert solution.cues.total_ticks == solution.render.total_ticks
    # The intro's sound counts as sound: moving it over the first stage is an
    # overlap, and the teaching delivery refuses it by name.
    shifted = apply(project, MoveClip('layer.intro', SECOND)).project
    assert shifted.clip('layer.intro').start.ticks == SECOND
    with pytest.raises(TeachingOverlapError) as error:
        cue_plan(shifted, golden_media(), measurement())
    assert error.value.code == 'TEACHING_OVERLAP'
    assert 'layer.intro' in str(error.value)
    plan = render_plan(shifted, golden_media(), measurement())
    assert [conflict.code for conflict in plan.conflicts] == ['OVERLAP_ACCEPTED']


def test_saving_and_reopening_keeps_the_intro_measurement_usable(tmp_path):
    from word_video.storage import load_project, save_project
    project = intro_project(fallback=FALLBACK, sound_asset=FALLBACK,
                            sound_source='FROM_INTRO_AUDIO', from_clip=False)
    path = save_project(project, tmp_path)
    reopened = load_project(path)
    assert reopened.schema == SCHEMA
    assert reopened.clip('layer.intro').audio_asset == FALLBACK
    again = render_plan(reopened, golden_media(),
                        measurement(sound_asset=FALLBACK,
                                    sound_source='FROM_INTRO_AUDIO', from_clip=False))
    assert again.intro_item.sound.asset_id == FALLBACK
    # A stale measurement for the reopened document is still refused.
    with pytest.raises(IntroMediaError):
        render_plan(reopened, golden_media(),
                    IntroMeasurement(asset_id='other.mp4', seconds=1.0))

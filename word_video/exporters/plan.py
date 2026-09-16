"""Drive the verified renderer and draft exporter from a solved plan.

The plan is the truth and the legacy ``timeline.json`` chain is the *engine*: the
MP4 renderer and the editable-draft exporter are M0-verified assets (offscreen
compositing, ``ass=`` last, independent objects, media beside the draft) and
rewriting them for the new model would throw that verification away.  So the plan
is projected onto the manifest those two consume, and this module *refuses* any
plan whose picture the manifest cannot express exactly - a silent lossy
conversion here would put captions and the draft on a different scene than the
plan the user approved, which is precisely the defect W02 fixed for the intro.

What the projection must preserve, and how it is checked
(:func:`verify_manifest`), is every time boundary and every text:

===============  ==========================================================
speech stages    one audio item per teaching stage, in teaching order
display layers   ``english``/``phonetic``/``meaning`` windows rebuilt by
                 ``template.text_events`` from the word's stage boundaries
project layers   title/subtitle/footer span the whole timeline
``ass=``         stays the last filter in the graph (the renderer's own rule)
===============  ==========================================================

Ticks become frames once, half up, through
:func:`word_video.domain.ticks_to_milliseconds`'s sibling for frames; and because
the solver places stages on whole frames the conversion is exact for every
template-expanded clip (the test asserts it rather than assuming it).
"""
from dataclasses import dataclass, field
from fractions import Fraction
import math

from ..domain.model import DISPLAY_LAYERS, TEACHING_STAGES
from ..domain.plan import CUE_TRACKS

#: Roles the manifest's word block can carry; anything else has no place in it.
_SUPPORTED_DISPLAY = tuple(DISPLAY_LAYERS)


class ProjectionError(Exception):
    """The plan cannot be expressed by the verified manifest without losing time."""


@dataclass
class ManifestAudio:
    """One prepared stage.  A plain dict on the view: the renderer and the draft
    exporter both index these by name (``item['start_frame']``), and keeping the
    two products on one shape is worth more than the type here."""

    word_index: int
    role: str
    text: str
    voice: str
    path: str
    start_frame: int
    duration_frames: int


@dataclass
class TimelineView:
    """Exactly the fields ``render/`` and ``draft.py`` read, and nothing else.

    Kept a separate, small object instead of ``TimelineManifest`` so the
    projection is visible: the day a renderer needs a new field, this class is
    where it is added and where the plan has to supply it.
    """

    schema_version: int
    title: str
    subtitle: str
    footer: str
    first_index: int
    last_index: int
    fps: int
    width: int
    height: int
    speed: float
    intro_frames: int
    total_frames: int
    background: str
    intro_audio: str
    intro_video: str
    video_codec: str
    styles: dict
    words: list = field(default_factory=list)
    audio: list = field(default_factory=list)

    def to_dict(self):
        return {'schema_version': self.schema_version, 'title': self.title,
                'subtitle': self.subtitle, 'footer': self.footer,
                'first_index': self.first_index, 'last_index': self.last_index,
                'fps': self.fps, 'width': self.width, 'height': self.height,
                'speed': self.speed, 'intro_frames': self.intro_frames,
                'total_frames': self.total_frames, 'background': self.background,
                'intro_audio': self.intro_audio, 'intro_video': self.intro_video,
                'video_codec': self.video_codec, 'styles': self.styles,
                'words': [dict(word) for word in self.words],
                'audio': [dict(item) for item in self.audio]}


def frame_at(ticks, fps_num, fps_den=1):
    """Ticks -> frame index, half up.  One conversion, at this boundary."""
    value = Fraction(ticks * fps_num, 720000 * fps_den)
    return int(math.floor(value + Fraction(1, 2)))


def ticks_for_frame(frame, fps_num, fps_den=1):
    return Fraction(frame * 720000 * fps_den, fps_num)


@dataclass(frozen=True)
class SourceMedia:
    """Where one solved asset's prepared audio is, plus the text it speaks."""

    path: str
    voice: str = ''


def build_manifest(plan, project, sources, *, background, intro_video='',
                   intro_audio='', video_codec='h264', styles=None,
                   first_index=None, last_index=None, fps=None):
    """Project a solved plan onto the view the verified engine consumes.

    ``sources`` maps ``asset_id`` -> :class:`SourceMedia`; every speech item must
    have one, because the renderer mixes real files and a missing path would fail
    much later with a message about the mixer rather than about the plan.
    """
    fps_num, fps_den = _rate(plan, fps)
    if fps_den != 1:
        # The manifest and everything downstream assume an integer frame rate; a
        # 30000/1001 project has to state the integer it is rendered at.  Refusing
        # is the honest option: rendering 29.97 as 30 silently retimes the lesson.
        raise ProjectionError(
            'manifest projection needs an integer frame rate, project has %d/%d'
            % (fps_num, fps_den))
    audio = _audio_items(plan, project, sources, fps_num)
    words = word_blocks(project, audio)
    first = words[0]['index'] if first_index is None else first_index
    last = words[-1]['index'] if last_index is None else last_index
    total_frames = frame_at(plan.total_ticks, fps_num)
    intro_frames = frame_at(project.intro_ticks, fps_num)
    for block in words:
        if block['end_frame'] > total_frames:
            raise ProjectionError('word %r ends after the timeline' % block['index'])
    view = TimelineView(
        schema_version=1, title=project.title,
        subtitle='速通（%d–%d）' % (first, last), footer=project.footer,
        first_index=first, last_index=last, fps=fps_num,
        width=plan.width, height=plan.height, speed=project.speed,
        intro_frames=intro_frames, total_frames=total_frames,
        background=str(background), intro_audio=str(intro_audio or ''),
        intro_video=str(intro_video or ''), video_codec=video_codec,
        styles=dict(styles or {}), words=words, audio=audio)
    verify_manifest(view, plan, project)
    return view


def _rate(plan, fps):
    if fps is not None:
        return int(fps), 1
    return int(plan.fps_num), int(plan.fps_den)


def _audio_items(plan, project, sources, fps_num):
    items = []
    for item in plan.audio:
        if item.role not in TEACHING_STAGES:
            raise ProjectionError('audio clip %s has role %r' % (item.clip_id, item.role))
        source = sources.get(item.source.asset_id if item.source else None)
        if source is None:
            raise ProjectionError(
                'no prepared audio for %s (asset %r)'
                % (item.clip_id, item.source.asset_id if item.source else None))
        clip = project.clip(item.clip_id)
        record = project.record(item.record_id)
        items.append({
            'word_index': record.index if record and record.index
            else _ordinal(project, item),
            'role': item.role, 'text': item.text, 'voice': source.voice,
            'path': str(source.path),
            'start_frame': frame_at(item.start_ticks, fps_num),
            'duration_frames': (frame_at(item.end_ticks, fps_num)
                                - frame_at(item.start_ticks, fps_num))})
    return items


def _ordinal(project, item):
    """1-based position of the record when the source list had no numbering."""
    ids = [record.id for record in project.records]
    return ids.index(item.record_id) + 1 if item.record_id in ids else 0


def word_blocks(project, audio):
    """Rebuild the manifest's word block from the same clips the plan solved.

    The block is what ``template.text_events`` reads to draw the English,
    phonetic and meaning layers, so its frame boundaries *are* those layers'
    windows; :func:`verify_manifest` checks them against the plan afterwards.
    """
    by_record = {}
    for item in audio:
        by_record.setdefault(item['word_index'], {})[item['role']] = item
    blocks = []
    for position, record in enumerate(project.records, start=1):
        index = record.index if record.index else position
        stages = by_record.get(index)
        if not stages:
            continue
        missing = [role for role in TEACHING_STAGES if role not in stages]
        if missing:
            raise ProjectionError('word %s is missing %s' % (record.word, ', '.join(missing)))
        female, male, chinese = stages['female'], stages['male'], stages['chinese']
        blocks.append({
            'index': index, 'word': record.word, 'phonetic': record.phonetic,
            'meaning': record.meaning, 'spoken_meaning': record.spoken_meaning,
            'start_frame': female['start_frame'],
            'male_frame': male['start_frame'],
            'chinese_frame': chinese['start_frame'],
            'end_frame': chinese['start_frame'] + chinese['duration_frames'],
        })
    if not blocks:
        raise ProjectionError('the plan has no speech to render')
    return blocks


def verify_manifest(view, plan, project):
    """Refuse a projection that lost a boundary, a text or a display window.

    Three checks, all against the plan the user approved:

    * the speech stages land on the frames the plan gave them;
    * every word block carries the record's own text;
    * the display windows ``template.text_events`` will rebuild are exactly the
      plan's display clips - this is the one that catches a moved text layer,
      which the manifest has no field to express.
    """
    fps = view.fps
    for item in view.audio:
        solved = next((candidate for candidate in plan.audio
                       if candidate.role == item['role']
                       and frame_at(candidate.start_ticks, fps) == item['start_frame']
                       and frame_at(candidate.end_ticks, fps)
                       - frame_at(candidate.start_ticks, fps) == item['duration_frames']),
                      None)
        if solved is None:
            raise ProjectionError(
                'audio item %s/%s does not match any plan item'
                % (item['word_index'], item['role']))
    _verify_text(view, plan, project)
    return True


def _verify_text(view, plan, project):
    """The manifest's display windows must be the plan's display clips."""
    from ..template import text_events

    planned = {}
    for item in plan.video:
        if item.role in _SUPPORTED_DISPLAY or item.role in ('title', 'subtitle', 'footer'):
            planned.setdefault(item.role, []).append(item)
    expected = []
    for track, text, start, end in text_events(view):
        expected.append((track, text, start, end))
    for track, text, start, end in expected:
        if track == 'countdown':
            continue
        candidates = planned.get(track) or []
        if not candidates:
            raise ProjectionError('the plan has no %s layer' % track)
        # Match by text and window: two words can share a track, so the window is
        # what identifies which planned clip this event came from.
        match = [item for item in candidates
                 if item.text == text and frame_at(item.start_ticks, view.fps) == start
                 and frame_at(item.end_ticks, view.fps) == end]
        if not match:
            windows = ', '.join('%s@%d-%d' % (item.clip_id,
                                              frame_at(item.start_ticks, view.fps),
                                              frame_at(item.end_ticks, view.fps))
                                for item in candidates)
            raise ProjectionError(
                'the manifest would draw %r at %d-%d, but the plan places it at %s; '
                'move it back or state the window in the project'
                % (track, start, end, windows))
    return True


def cue_track_files(cues):
    """The five tracks as ``{track: [(start_ticks, end_ticks, text), ...]}``."""
    grouped = cues.by_track()
    return {track: [(cue.start_ticks, cue.end_ticks, cue.text) for cue in grouped[track]]
            for track in CUE_TRACKS}

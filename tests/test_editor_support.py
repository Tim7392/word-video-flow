"""Shared support for the editor tests: a real project folder on disk.

Kept as a ``test_editor_*`` module so it stays inside this round's write scope,
and so the model, project-folder and window tests all edit *the same* lesson
instead of three drifting fixtures.

Two deliberate choices:

* **the folder is a real folder.**  ``project.json``, ``media.json`` and
  ``delivery.json`` are written by the same code the member's editor writes, and
  the audio is real 48 kHz WAV on disk, because "the edit survived a save and a
  reopen" and "the missing file is reported" cannot be tested against an
  in-memory stand-in;
* **the words are tones, the times are the verified rhythm.**  The words go
  through ``instantiate`` with the real template, so the ticks under test are the
  ones the delivery uses - and the audio is synthesised here, so the default suite
  stays offline and independent of any cache.
"""
from pathlib import Path

from word_video.domain.lesson import DEFAULT_LESSON_TEMPLATE

from desktop.editor_model import EditorState
from desktop.editor_project import Delivery, ProjectFolder

from test_preview_support import RATE, write_tone  # noqa: F401  (RATE re-exported)

__all__ = ['RATE', 'write_tone', 'words_of', 'tiny_folder', 'tiny_state', 'assets_of',
           'revision_snapshot']

#: Two Hanzi floors the Chinese stage at 0.8 s; the tones are 1.0 s so every stage
#: is long enough to trim in both directions.
DEFAULT_MEANINGS = {'promise': '承诺', 'apple': '苹果', 'elephant': '大象'}
DEFAULT_PHONETIC = {'promise': 'ˈprɒmɪs', 'apple': 'ˈæpl', 'elephant': 'ˈelɪfənt'}


def words_of(count=1):
    """The first ``count`` words of the fixture, in a stable order."""
    table = [('promise', 'ˈprɒmɪs', '承诺'), ('apple', 'ˈæpl', '苹果'),
             ('elephant', 'ˈelɪfənt', '大象')]
    return tuple(table[:count])


def assets_of(folder=None, *, seconds=1.0, count=1):
    """``{asset_id: (path, voice)}`` and the tone files, one per word and role.

    Distinct frequencies per word keep "which word is this" answerable by ear and
    by measurement, the same trick the preview fixture uses.
    """
    base = Path(folder)
    assets, tones = {}, {}
    for index, (word, _, _) in enumerate(words_of(count), start=1):
        for slot, role in enumerate(('female', 'male', 'chinese')):
            asset_id = 'w%d:%s' % (index, role)
            path = base / ('%s.wav' % asset_id.replace(':', '_'))
            if not path.is_file():
                write_tone(path, seconds, 300.0 * (index * 3 + slot))
            assets[asset_id] = (str(path), 'BV50%d_streaming' % slot)
            tones[asset_id] = str(path)
    return assets, tones


def write_intro_with_audio(path, seconds=1.0, fps=30, size='160x120', rate=48000):
    """A tiny MP4 with a picture *and* a packetised audio track (AAC).

    The reference countdown is exactly this shape, and the shape matters: an AAC
    stream's ``nb_frames`` is a packet count, so any measurement that trusts it as a
    sample count is off by a factor of 1024.  A fixture without audio would let that
    pass unnoticed.
    """
    from word_video.media.core import executable, run
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file():
        run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n',
             '-f', 'lavfi', '-i', 'testsrc=size=%s:rate=%d:duration=%.4f'
             % (size, fps, seconds),
             '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=%d:duration=%.4f'
             % (rate, seconds),
             '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
             '-c:a', 'aac', '-shortest', '-movflags', '+faststart', str(path)])
    return path


def refs_of(folder, *, seconds=1.0, count=1):
    """``(refs, voices)`` for a synthetic project, measured through the media layer."""
    from word_video.exporters import catalog as catalog_module
    from word_video.exporters.catalog import Asset
    from word_video.storage.assets import AssetRef

    assets, _ = assets_of(folder, seconds=seconds, count=count)
    refs, voices = [], {}
    for asset_id, (path, voice) in sorted(assets.items()):
        info = catalog_module.measure(Asset(asset_id=asset_id, path=path, voice=voice))
        refs.append(AssetRef(asset_id=asset_id, path=path, units=info.units,
                             unit_num=info.unit_num, unit_den=info.unit_den,
                             kind='audio'))
        voices[asset_id] = voice
    return tuple(refs), voices


def tiny_folder(folder, *, count=1, seconds=1.0, speed=1.25, delivery=None,
                background='', intro_video=''):
    """Write a complete project folder for ``count`` words and return it opened.

    The returned object is the same :class:`~desktop.editor_project.ProjectFolder`
    the window opens, so a test and the member are looking at the same files: a
    ``project.json``, A's ``assets.json`` registry, B's ``media.json`` catalogue and
    the delivery sidecar, all written by the same code the editor uses.
    """
    from word_video.application import instantiate
    from word_video.domain.model import Project, Record

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    refs, voices = refs_of(folder, seconds=seconds, count=count)
    media = {ref.asset_id: ref.media_info for ref in refs}
    records = tuple(Record(id='w%d' % index, word=word, phonetic=phonetic,
                           meaning=meaning, spoken_meaning=meaning, index=index)
                    for index, (word, phonetic, meaning)
                    in enumerate(words_of(count), start=1))
    project = instantiate(DEFAULT_LESSON_TEMPLATE, records, media,
                          Project(project_id='editor-fixture', speed=speed,
                                  fps_num=60, width=1920, height=1080, intro_s=1.0))
    settings = delivery or Delivery(background=background, intro_video=intro_video)
    return ProjectFolder.create(folder, project, refs, voices, settings)


def tiny_state(folder, **kwargs):
    """An :class:`EditorState` over a freshly written folder."""
    project_folder = tiny_folder(folder, **kwargs)
    state = EditorState(project_folder.project, media=project_folder.media_table(),
                        path=project_folder.project_path)
    return project_folder, state


def revision_snapshot(state):
    """Every clip as stored: id -> (start, duration).  The per-object oracle."""
    return {clip.id: (clip.start.to_dict(), clip.duration_ticks)
            for clip in state.project.clips}


def solved_snapshot(state):
    """Every clip as solved: id -> (start_ticks, end_ticks)."""
    return dict(state.ranges())

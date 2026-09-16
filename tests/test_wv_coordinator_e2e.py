"""The coordinator driving the *real* renderer: submit, run, verify, publish.

Marked ``acceptance_media`` because it encodes a video, so it stays out of the
default suite while remaining runnable on demand::

    python -m pytest -m acceptance_media tests/test_wv_coordinator_e2e.py

Everything else about coordination (concurrency, receipts, cancel, reconcile) is
covered with a fake runner in ``tests/test_wv_coordinator_store.py``; this file is
here to prove the wiring to B's exporter, which no fake can.
"""
import json
from pathlib import Path

import pytest

from word_video.application import instantiate
from word_video.application.batches import (BatchSelection, ExportProfile, plan_batch,
                                           submission_for)
from word_video.domain import DEFAULT_LESSON_TEMPLATE, Project, Record
from word_video.exporters import catalog
from word_video.media import executable, run
from word_video.storage import AssetIndex, AssetRef
from word_video.storage.coordinator import Coordinator
from word_video.storage.project_store import save_project

pytestmark = pytest.mark.acceptance_media

FIXTURE = Path(r'D:\1\1-AI_workflow\word_video_flow\data\fixtures\p1-有片头.json')
ARCHIVE = Path(r'D:\单词速记自动化_测试归档_0915')
BACKGROUND = ARCHIVE / '_prepared-1080p' / 'background-from-reference-1080p.mp4'
CANVAS = {'width': 320, 'height': 180}
FPS = 24


@pytest.fixture
def project(tmp_path):
    """A one-word project with real cached audio, registered and measurable."""
    if not (FIXTURE.is_file() and BACKGROUND.is_file()):
        pytest.skip('p1 fixtures or the 1080p reference media are absent')
    root = tmp_path / 'root'
    folder = root / 'projects' / 'p1'
    folder.mkdir(parents=True)
    fixture = json.loads(FIXTURE.read_text(encoding='utf-8'))
    items = [item for item in fixture['provider']['items'] if item['index'] == 152]
    text = {item['role']: item['text'] for item in items}
    records = (Record(id='w152', word=text['female'], phonetic='ˈdepjuti',
                      meaning='n. 副手；代理', spoken_meaning=text['chinese'],
                      index=152),)
    assets = {'w152:%s' % item['role']: catalog.Asset(
        asset_id='w152:%s' % item['role'], path=item['path'], voice=item['voice'])
        for item in items}
    media = catalog.measure_all(assets)
    base = Project(project_id='p1', intro_s=0.0, fps_num=FPS,
                   width=CANVAS['width'], height=CANVAS['height'], speed=1.25)
    built = instantiate(DEFAULT_LESSON_TEMPLATE, records, media, base)
    save_project(built, folder)
    AssetIndex.of(tuple(AssetRef(item.asset_id, item.path,
                                 units=media[item.asset_id].units,
                                 unit_num=media[item.asset_id].unit_num,
                                 unit_den=media[item.asset_id].unit_den,
                                 kind='audio', voice=item.voice)
                        for item in assets.values()),
                  project_id='p1', folder=str(folder)).save(folder)
    background = root / 'background.mp4'
    run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-i', str(BACKGROUND),
         '-t', '8', '-vf', 'scale=%d:%d' % (CANVAS['width'], CANVAS['height']),
         '-r', str(FPS), '-an', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '30',
         '-pix_fmt', 'yuv420p', str(background)], timeout=900)
    return root, folder, background


def test_a_real_batch_runs_through_the_coordinator_and_publishes(project):
    root, folder, background = project
    coordinator = Coordinator(root)
    loaded = coordinator.load_project('p1')
    index = AssetIndex.load(folder)
    plan = plan_batch(loaded, index.media_map(), BatchSelection(record_ids=('w152',)),
                      ExportProfile(background=str(background)))
    assert plan.ok, [problem.message for problem in plan.problems]
    assert plan.delivery.ready, plan.delivery.to_dict()
    submission = submission_for(plan, (str(folder / 'project.json'),
                                       str(folder / 'assets.json'), str(background)))
    job = coordinator.submit(submission, 'e2e-152')['job']
    finished = coordinator.run(job)
    assert finished['state'] == 'succeeded', finished['error']
    paths = [item['path'] for item in finished['artifacts']]
    # Published under runs/<job>, not staging, and every receipt still verifies.
    assert paths and all(str(root / 'runs' / job) in path for path in paths)
    assert any(path.endswith('video.mp4') for path in paths)
    assert len([path for path in paths if path.endswith('.srt')]) == 5
    assert any(path.endswith('draft_content.json') for path in paths)
    assert coordinator.reconcile()['partial_failed'] == []
    assert not (root / 'staging' / job).exists()
    assert coordinator.artifacts(job)['published'] is True
    # The receipt names the plan that produced them, so the run is reproducible.
    receipt = coordinator.receipts()[0]
    assert receipt['plan_identity'] == plan.plan_identity
    assert receipt['idempotency_key'] == 'e2e-152'
    # H0's red line: every path the delivery's own documents carry must resolve, and
    # none of them may name a folder the run no longer lives in.
    run_dir = root / 'runs' / job
    checked = 0
    for name in ('complete.json', 'timeline.json',
                 'editable-draft/draft_content.json'):
        document = json.loads((run_dir / name).read_text(encoding='utf-8'))
        found = [item['path'] for item in document.get('files', [])]
        if not found:                       # timeline/draft carry paths, not a file list
            found = _paths_in(document)
        assert found, name
        assert all(Path(path).exists() for path in found), name
        assert not [path for path in found if 'staging' in path], name
        checked += len(found)
    assert checked > 40                    # 43 files in H0's own run; not a smoke test


def _paths_in(value):
    """Every path-shaped string in a JSON document, whatever its shape."""
    found = []
    if isinstance(value, dict):
        for item in value.values():
            found.extend(_paths_in(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_paths_in(item))
    elif isinstance(value, str) and len(value) > 3 and value[1:3] == ':\\':
        found.append(value)
    return found

"""Package development sources only; excludes media, credentials and job databases."""
from pathlib import Path
import zipfile

root=Path(__file__).resolve().parent
target=root/'dist/word-video-dev-0.1.0.zip'
target.parent.mkdir(exist_ok=True)
files=list((root/'word_video').rglob('*.py'))+list((root/'tests').glob('test_word_video_*.py'))
files += [root/name for name in ('word_video_cli.py','word_video_package.py','subtitle_factory_core.py',
          'subtitle_factory_api.py','requirements-word-video.txt','单词视频使用说明.md','单词视频_执行验收记录.md')]
skill=Path.home()/'.codex/skills/word-video-factory/SKILL.md'
with zipfile.ZipFile(target,'x',compression=zipfile.ZIP_DEFLATED) as archive:
    for path in files: archive.write(path,path.relative_to(root))
    archive.write(skill,'skill/word-video-factory/SKILL.md')
with zipfile.ZipFile(target) as archive:
    if archive.testzip(): raise RuntimeError('Invalid archive')
    print(str(target),len(archive.namelist()),target.stat().st_size)

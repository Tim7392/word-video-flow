"""QA guard: the audio sample count of a delivered asset, recomputed independently.

Why this exists: a packet/frame count is not a sample count.  For
``4级1500开头_透明通道-1080p.mov`` the container reports 88 AAC frames; 88 * 1024
= 90112 samples = 1.8773 s, while the stream's own duration is 89088 samples =
1.856 s - exactly one frame less, because the audio is trimmed to the video length
and the last packet is partial.  Any code that multiplies the packet count by the
frame size is wrong by 21 ms, and a check that only reads one field cannot see it.

So the guard recomputes the count three independent ways and says which method it
trusts and why:

  decode      the decoder's own output, counted here (authoritative for "how many
              samples does playing this give me");
  container   ``duration_ts`` on the audio stream with its ``time_base`` (what the
              engine's ``audio_sample_count`` reports);
  packets     ``nb_frames * frame_samples`` for AAC, reported only to show it is a
              different number and must not be used as a sample count.

Usage:
  python aac_samples.py --input FILE [--json OUT] [--expect-samples N]
                        [--rate 48000] [--frame-samples 1024]
"""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import wave

sys.stdout.reconfigure(encoding='utf-8', errors='replace')


def tool(name):
    found = shutil.which(name)
    if found:
        return found
    for candidate in (Path.home() / '.local' / 'bin' / (name + '.EXE'),
                      Path.home() / '.local' / 'bin' / name):
        if candidate.exists():
            return str(candidate)
    raise SystemExit('cannot find %s' % name)


def probe(path):
    out = subprocess.run([tool('ffprobe'), '-v', 'error', '-print_format', 'json',
                          '-show_format', '-show_streams', str(path)],
                         capture_output=True)
    return json.loads(out.stdout.decode('utf-8', 'replace'))


def count_by_decode(source, rate, target):
    """Pipe the audio to raw PCM and count the samples that come out."""
    result = subprocess.run([tool('ffmpeg'), '-v', 'error', '-nostdin', '-i',
                             str(source), '-vn', '-map', '0:a:0', '-ac', '1',
                             '-ar', str(rate), '-c:a', 'pcm_s16le', '-f', 'wav',
                             str(target)], capture_output=True)
    if result.returncode != 0:
        raise SystemExit('decode failed: %s'
                         % result.stderr.decode('utf-8', 'replace')[-400:])
    with wave.open(str(target), 'rb') as stream:
        return stream.getnframes(), stream.getframerate()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', required=True)
    parser.add_argument('--rate', type=int, default=48000)
    parser.add_argument('--frame-samples', type=int, default=1024)
    parser.add_argument('--expect-samples', type=int, default=None)
    parser.add_argument('--json')
    parser.add_argument('--work', default=None, help='解码中间 WAV 的落点')
    args = parser.parse_args(argv)

    source = Path(args.input).resolve(strict=True)
    info = probe(source)
    audio = next((s for s in info['streams'] if s['codec_type'] == 'audio'), None)
    if audio is None:
        raise SystemExit('%s 没有音频流' % source)
    container_samples = None
    if audio.get('duration_ts') is not None:
        num, den = (audio['time_base'].split('/') + ['1'])[:2]
        container_samples = round(float(audio['duration_ts']) * float(num) / float(den)
                                  * args.rate)
    packets = int(audio['nb_frames']) if str(audio.get('nb_frames', '')).isdigit() else None
    warm = Path(args.work) if args.work else source.parent / (source.stem + '.qa-decode.wav')
    decoded, rate = count_by_decode(source, args.rate, warm)
    if args.work is None:
        try:
            warm.unlink()
        except OSError:
            pass

    report = {
        'input': str(source),
        'codec': audio['codec_name'],
        'sample_rate': int(audio.get('sample_rate', args.rate)),
        'channels': int(audio.get('channels', 0)),
        'stream_seconds': float(audio.get('duration', 0)),
        'counts': {
            'decode': decoded,
            'container_duration_ts': container_samples,
            'packets_times_frame_samples': (packets * args.frame_samples
                                            if packets is not None else None),
        },
        'packet_frames': packets,
        'frame_samples': args.frame_samples,
        'decoded_rate': rate,
    }
    counts = report['counts']
    problems = []
    if counts['container_duration_ts'] != decoded:
        problems.append('container 报 %s 个采样，解码得到 %d 个'
                        % (counts['container_duration_ts'], decoded))
    if counts['packets_times_frame_samples'] is not None \
            and counts['packets_times_frame_samples'] == decoded:
        problems.append('包数*帧长 恰好等于解码采样数：这不能证明口径正确，需要重新选素材')
    if abs(decoded / rate - report['stream_seconds']) > 1.5 / args.rate:
        problems.append('解码时长 %.6fs 与容器时长 %.6fs 不符'
                        % (decoded / rate, report['stream_seconds']))
    if args.expect_samples is not None and decoded != args.expect_samples:
        problems.append('解码采样数 %d != 期望 %d' % (decoded, args.expect_samples))
    report['exact'] = counts['container_duration_ts'] == decoded
    report['problems'] = problems
    report['ok'] = not problems
    text = json.dumps(report, ensure_ascii=False, indent=1)
    print(text)
    if args.json:
        target = Path(args.json)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

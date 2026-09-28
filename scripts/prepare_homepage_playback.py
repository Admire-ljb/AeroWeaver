"""Create smaller 720p playback copies; leave the published 1080p files intact.

Requires ffmpeg with libsvtav1, libopus and libx264. Run from any directory:
    python scripts/prepare_homepage_playback.py
"""
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VIDEOS = ROOT / 'site' / 'assets' / 'videos'
OUTPUT = VIDEOS / 'playback'


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    manifest = []
    for name in ('teaser', 'cockpit', 'simulation', 'pursuit', 'aeroweaver-demo'):
        source = VIDEOS / f'{name}.mp4'
        audio = name == 'aeroweaver-demo'
        common = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(source),
                  '-map', '0:v:0', '-map', '0:a?', '-vf', 'scale=1280:-2:flags=lanczos',
                  '-pix_fmt', 'yuv420p', '-g', '120']
        variants = [
            ('webm', ['-c:v', 'libsvtav1', '-preset', '6', '-crf', '42',
                      '-svtav1-params', 'lp=4:mbr=450', '-c:a', 'libopus', '-b:a', '48k',
                      '-cues_to_front', '1']),
            ('mp4', ['-c:v', 'libx264', '-preset', 'slow', '-crf', '27',
                     '-maxrate', '450k', '-bufsize', '900k', '-threads', '4',
                     '-c:a', 'aac', '-b:a', '48k', '-movflags', '+faststart']),
        ]
        for extension, codec in variants:
            target = OUTPUT / f'{name}-720.{extension}'
            command = common + codec + ([] if audio else ['-an']) + [str(target)]
            subprocess.run(command, check=True)
            item = {'file': target.relative_to(VIDEOS).as_posix(), 'bytes': target.stat().st_size,
                    'original_bytes': source.stat().st_size}
            manifest.append(item)
            print(json.dumps(item), flush=True)
    (OUTPUT / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()

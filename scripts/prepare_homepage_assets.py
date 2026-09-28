"""Export the existing paper figures and approved demo cuts for the static site.

Usage: python scripts/prepare_homepage_assets.py --paper-dir <published-package>
Requires Pillow, PyMuPDF and ffmpeg. No source media is altered.
"""
import argparse
import json
import shutil
import subprocess
from pathlib import Path

import fitz
from PIL import Image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--paper-dir', type=Path, required=True)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    destination = args.root / 'site' / 'assets'
    figures = destination / 'images'
    videos = destination / 'videos'
    figures.mkdir(parents=True, exist_ok=True)
    videos.mkdir(parents=True, exist_ok=True)
    manifest = []

    def export_image(image, name, maximum=2400):
        image = image.convert('RGBA')
        canvas = Image.new('RGBA', image.size, 'white')
        canvas.alpha_composite(image)
        canvas = canvas.convert('RGB')
        canvas.thumbnail((maximum, maximum), Image.Resampling.LANCZOS)
        canvas.save(figures / f'{name}.webp', quality=92, method=6)
        return canvas

    for source, name in [('Aeroweaver.png', 'framework'), ('prompt.png', 'skill-activation'),
                         ('distributed_swarm_execution_p.png', 'coordination'), ('update.png', 'experience')]:
        export_image(Image.open(args.paper_dir / source), name)
        manifest.append({'asset': f'images/{name}.webp', 'source': source, 'type': 'published paper figure'})

    for source, name in [('main_token_comparison.pdf', 'tokens'),
                         ('additional_final/adaptation.pdf', 'adaptation'),
                         ('additional_final/backbone.pdf', 'backbones'),
                         ('additional_final/prompt.pdf', 'prompts')]:
        with fitz.open(args.paper_dir / 'figures' / 'experiments' / source) as document:
            pixmap = document[0].get_pixmap(matrix=fitz.Matrix(3, 3), alpha=False)
            export_image(Image.frombytes('RGB', (pixmap.width, pixmap.height), pixmap.samples), name)
        manifest.append({'asset': f'images/{name}.webp', 'source': source, 'type': 'published paper figure'})

    demo = args.root / 'results' / 'demo-cinematic-20260919'
    teaser = args.root / 'results' / 'demo-teaser-20260919' / 'deliverables'
    full = demo / 'deliverables' / 'AeroWeaver_Submission_Cinematic.mp4'
    shutil.copy2(full, videos / 'aeroweaver-demo.mp4')
    manifest.append({'asset': 'videos/aeroweaver-demo.mp4', 'source': full.name, 'type': 'complete demo'})

    def encode(source, name, start=0, duration=None, width=1920, crf=24):
        command = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-ss', str(start), '-i', str(source)]
        if duration:
            command += ['-t', str(duration)]
        command += ['-an', '-vf', f'scale={width}:-2', '-c:v', 'libx264', '-preset', 'fast',
                    '-crf', str(crf), '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(videos / f'{name}.mp4')]
        subprocess.run(command, check=True)
        frame = figures / f'{name}-frame.png'
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-ss', '1.8',
                        '-i', str(videos / f'{name}.mp4'), '-frames:v', '1', str(frame)], check=True)
        export_image(Image.open(frame), f'{name}-poster', maximum=1600)
        frame.unlink()
        manifest.append({'asset': f'videos/{name}.mp4', 'source': source.name,
                         'start_seconds': start, 'duration_seconds': duration, 'type': 'demo excerpt'})
        print(f'Prepared {name}', flush=True)

    encode(teaser / 'AeroWeaver_Teaser_9s_silent.mp4', 'teaser', duration=9, crf=24)
    body = demo / 'work' / 'clean-body.mp4'
    encode(body, 'cockpit', 20, 8)
    encode(body, 'simulation', 43, 12)
    encode(body, 'formation', 78, 8)
    encode(args.root / 'results' / 'demo-twin-20260919' / 'deliverables' / 'AeroWeaver_Physical_Twin_50s_QHD.mp4',
           'pursuit', 0, 50, crf=25)
    export_image(Image.open(demo / 'deliverables' / 'final-review-005.00.jpg'), 'demo-poster', maximum=1600)
    export_image(Image.open(figures / 'teaser-poster.webp'), 'social-preview', maximum=1200)
    manifest_path = args.root / 'results' / 'homepage-20260928' / 'asset-manifest.json'
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, indent=2), encoding='utf-8')
    print('Asset export complete.', flush=True)


if __name__ == '__main__':
    main()

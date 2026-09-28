"""Export responsive WebP previews, keeping full-size paper figures for zoom."""
import json
from pathlib import Path
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
IMAGES = ROOT / 'site' / 'assets' / 'images'
OUTPUT = IMAGES / 'preview'


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    manifest = []
    for source in sorted(IMAGES.glob('*.webp')):
        if source.stem == 'social-preview':
            continue
        poster = source.stem.endswith('-poster')
        with Image.open(source) as original:
            for width in ((960,) if poster else (640, 960, 1440)):
                if width >= original.width:
                    continue
                preview = original.copy()
                preview.thumbnail((width, 10000), Image.Resampling.LANCZOS)
                target = OUTPUT / f'{source.stem}-{width}.webp'
                preview.save(target, 'WEBP', quality=80 if poster else 88, method=6)
                manifest.append({'file': target.relative_to(IMAGES).as_posix(),
                                 'width': preview.width, 'height': preview.height,
                                 'bytes': target.stat().st_size, 'original_bytes': source.stat().st_size})
    (OUTPUT / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()

# AeroWeaver project page

A dependency-free, responsive academic project page. All local assets use relative paths and work at the GitHub Pages project path `/AeroWeaver/`.

The layout follows a continuous academic reading order, with a single text column, standard Arial typography, unframed paper figures, and full-width results tables. Mechanism tabs, figure enlargement, and video chapters remain available without adding dashboard-style cards.

## Local preview

From the repository root:

```sh
python -m http.server 8766 --bind 127.0.0.1 --directory site
```

Open `http://127.0.0.1:8766/`.

## Contents

- `index.html`: paper metadata, method descriptions, results, video chapters, and citation.
- `styles.css`: responsive layout and reduced-motion support.
- `app.js`: accessible mechanism tabs, figure viewer, video chapters, and citation copying.
- `assets/images/`: figures exported from the September 16, 2026 arXiv source package and video posters.
- `assets/videos/`: the complete 177-second demonstration and selected excerpts from the approved September 19 demo.

Main results preserve the raw mean ± SD from Table II. Ablations reproduce Table III, including the fixed task scales and affine score definition. Physical videos are qualitative demonstrations; synchronized trajectory replays are described separately from the quantitative simulation evaluation.

The page uses no analytics, third-party JavaScript, external fonts, API services, or build dependencies. Video excerpts are silent; the full demo includes narration and burned-in English subtitles. Videos use native playback controls. Only the muted teaser starts automatically when visible, unless reduced motion or data saving is requested.

Playback defaults to smaller 720p copies (AV1/WebM when supported, with an H.264/MP4 fallback). Each player offers the original 1080p version without changing the playback position. The already-small formation clip keeps its 1080p source. Only the nearest player is preloaded; playing another video cancels competing background transfers. Offscreen videos are not all fetched on page load. Without JavaScript, the native players still use the 720p MP4 files. All video files remain on the same GitHub Pages origin.

Run `python scripts/prepare_homepage_playback.py` after exporting or replacing the original video assets. This generates `assets/videos/playback/` using ffmpeg (libsvtav1, libopus, libx264), without modifying the 1080p files. Encoding uses 4-second keyframe intervals and a constrained bitrate; the MP4 index and WebM cues are placed before the video data to support early playback and seeking. The generated manifest records sizes for comparison.

BibTeX keeps one field per source line. The page wraps long fields to the available width; copying the citation preserves the field structure without manually inserted continuation lines.

Paper figures use 640/960/1440-pixel responsive previews with native lazy loading and reserved aspect ratios. Figure enlargement loads the original asset. Posters use smaller 960-pixel previews, with the teaser poster prioritized on first load. Run `python scripts/prepare_homepage_images.py` after changing the full-size images.

## Updating the site

Edit the three source files and replace media under `assets/`. Keep paper numbers, captions, and the BibTeX entry aligned with the published paper. Figure links remain usable without JavaScript. To regenerate the assets from the original local paper/demo packages, use `scripts/prepare_homepage_assets.py` from the main project checkout.

For GitHub Pages, publish these directory contents at the root of the `gh-pages` branch and select that branch as the Pages source. The `.nojekyll` file disables unnecessary processing.

Layout reference: [VoLN](https://admire-ljb.github.io/VoLN-UAV/). This implementation is independently written; no reference-page code or assets are bundled.

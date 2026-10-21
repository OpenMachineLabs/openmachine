## Context is part of retrieval

Editable Remotion pilot for Spec #204. One 40-second English film, 1920×1080 at 30 fps, using original React/SVG artwork and a procedural soundtrack.

Reproduce with Node 24 and npm 11: run `npm ci`, `python3 scripts/generate_soundtrack.py`, `npm run typecheck`, `npm run studio`, `npm run render`, and `npm run still`. Outputs are ignored in out/ and public/score.wav. The complete render must be reviewed at normal speed with sound; automated checks do not establish editorial acceptance.

Scene copy and timing live in src/Film.tsx. Change a local string or scene boundary, render a still, then rerender without rewriting unrelated shots.

## Review artifacts

The checked-in review bundle is in the repository-level `artifacts/motion-studio/` directory: the complete 40-second MP4, a nine-second mechanism preview, poster frame, and contact sheet. The package soundtrack is generated locally by `scripts/generate_soundtrack.py` and intentionally not tracked.

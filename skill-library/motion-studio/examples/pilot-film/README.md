# The Lost Address — editable Motion Studio pilot

Spec #204 / one 40-second English technical short, 1920×1080, 30 fps, authored directly in React/SVG with Remotion. The film explains a concrete passage's lost source context and its restoration before semantic/BM25 indexing, then shows a measured result from Anthropic's 2024 Contextual Retrieval article.

This is a demonstration of the production *method*, not an alternate editor, slideshow renderer, visual-scoring framework or official Anthropic branding.

## Reproduce locally

Use Node.js 24 and npm 11, Python 3.12+, and a working Chrome/Chromium rendering environment.

1. Run `npm ci` in this directory.
2. Run `python3 scripts/generate_soundtrack.py` to synthesize the original 40-second stereo score (no audio downloads).
3. Run `npm run typecheck`.
4. Run `npm run studio` for editable timeline inspection.
5. Render the **nine-second core mechanism**: `npm run preview` (frames 310–579).
6. Render the full film: `npm run render`.
7. Render a representative poster: `npm run still`; generate a contact sheet from the film with `python3 scripts/review_sheet.py`.
8. Render the alternative art-direction still: `npm run direction-b`.

If the Remotion-managed Chrome binary is unavailable on your machine, point the CLI to a locally installed browser with the documented `--browser-executable` option, for example `npm run render -- --browser-executable="/path/to/chrome"`. It must be your own available browser, not a machine-specific path committed to the project.

All generated files live in ignored `out/` or ignored `public/score.wav`, never in the tracked Skill package. The review copies live in the repository-level `artifacts/motion-studio/`.

## Edit the film

- `src/Film.tsx`: the seven authored shots, frame windows, screen copy and movement timing. One global Remotion frame clock; no hidden timers or scene scripting layer.
- `src/Art.tsx`: the same reusable financial filing, excerpt, context band, woven semantic index and lexical register across shots. These are layered original vector drawings, not static cards with transforms.
- `src/DirectionB.tsx`: a materially different query-first keyframe, considered and rejected as the film's main direction.
- `brief.md`: sourced claims, the two-direction decision, shot purposes, time windows and sound cues.
- `scripts/generate_soundtrack.py`: deterministic original stereo tones synchronized to beats.
- `scripts/review_sheet.py`: representative source frames and contact sheet.

Change the line of copy or a frame boundary in Film.tsx, inspect the affected frame in Studio, and rerender the relevant preview/full film. All substantive evidence claims must be compared with the source article, not inferred from the animated diagram.

## Review contract

The final deliverables are the actual MP4, nine-second preview, poster, contact sheet, art-direction comparison JPEG outside the package, editable source, lockfile and this reproduction note. The score is audible but optional for comprehension; there is **no narration**, so captions are unnecessary.

A render, codec inspection, sampled-frame audit or CI PASS is *not* the same as watching the whole film at normal speed with sound at the intended viewing size. The accepting reviewer should check shot-to-shot legibility, temporal relationships, sound texture, exact claims and the full end hold. Preserve this distinction in the PR review and in Spec #204.

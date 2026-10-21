# Usage and specification

Run commands from the skill directory, or use absolute paths to `scripts/motion.py`.
Python 3.10+ provides the offline CLI using only its standard library. Rendering
uses Node.js 20+ and the exact packages in `assets/renderer/package-lock.json`.
Check Remotion's [current license terms](https://www.remotion.dev/docs/license)
when choosing it for a team or automated service.

## Quick start with the original article example

```bash
python scripts/motion.py search "routing"
python scripts/motion.py show EC01
python scripts/motion.py validate examples/article-motion.json
python scripts/motion.py prepare examples/article-motion.json --output runs/article-review
```

Install dependencies once from the renderer directory:

```bash
cd assets/renderer
npm ci
```

Then, from the skill directory:

```bash
python scripts/motion.py render examples/article-motion.json --output runs/article-video
python scripts/motion.py render examples/repository-motion.json --output runs/repository-video
```

Outputs must not already exist. Choose a new directory for revisions; commands
do not overwrite earlier work. The first render may download Chrome Headless
Shell. If the download is unavailable, select an installed compatible browser:

```bash
python scripts/motion.py render examples/article-motion.json --output runs/article-video-local --browser-executable /path/to/chrome
```

`--concurrency 1` reduces simultaneous frame rendering on constrained machines.
Use a project-local dependency install. Source analysis needs no browser, network,
Remotion installation or credentials. Runtime font availability affects appearance;
inspect every requested language, especially non-Latin glyphs.

## Capture an article or repository

```bash
python scripts/motion.py ingest --article /path/article.md --output runs/article-source.json
python scripts/motion.py ingest --repo /path/project --ref HEAD --include docs/workflow.md --include src/router.py --output runs/repo-source.json
```

For an article URL, first retrieve its readable content with an available reader,
then ingest that local UTF-8 text. Retain the actual URL and date in its locator
or working notes. For a GitHub URL, use an authorized repository reader or local
clone and pin a commit. The CLI intentionally does not clone, fetch or execute
input projects.

Repository ingestion reads the root README plus explicitly included committed
files at the resolved revision. It rejects path traversal and symlink blobs.
Dirty and untracked changes are excluded. Limits are 256 KiB per source and
1 MiB total text. Include only the code needed to substantiate the chosen scope.

Copy the bundle's `sources` into a newly authored motion specification. Ingestion
does not create a storyboard: the host agent must understand the source and
choose the explanation. The original article example and the pinned Open Machine
documentation example demonstrate different evidence scopes.

## Specification version 1

The full runnable examples are the schema examples. Required top-level fields:

| Field | Meaning |
|---|---|
| `schema_version` | `1` |
| `title`, `audience`, `goal` | Project name, viewer and communication objective |
| `format` | `landscape` or `portrait`; layouts reflow independently |
| `fps` | `30` |
| `theme` | `paper` or `midnight` |
| `sources` | Evidence snapshots: `id`, `kind`, `locator`, `sha256`, `text` |
| `claims` | `id`, `text`, `evidence`: one or more `{source_id, quote}` anchors |
| `entities` | Stable `id`, `label`, optional `detail` |
| `scenes` | Ordered scenes, described below |
| `notes` | Optional list of scope assumptions and limitations |
| `language` | Optional `en` (default) or `zh` for built-in microcopy; author scene text in the desired language |

Source hashes use UTF-8 after CRLF/CR normalization to LF, with all other content
unchanged (`sha256-utf8-lf-v1`). Each exact quote must occur in its identified
source. Short identified excerpts may be used when sufficient; label them as
excerpts and clarify that their hash is not the upstream file's hash. Do not
silently truncate a source or imply full-repository coverage.

Each scene requires `id`, `kind`, `duration` in seconds, `title`, `caption`,
`narration`, `claim_ids` and `entities`. Narration can be an empty string.
`routing` additionally requires `active_branch`. Scene IDs and entity IDs are
lowercase slugs (a letter followed by lowercase letters, digits or hyphens),
not display text. Reuse an entity ID for the same object.

The built-in composition accepts 3–8 scenes, 3–18 seconds each, totaling 15–90
seconds. Durations must land on whole frames at 30 fps. Titles are at most 70
characters, captions 150, labels 30 and details 90. These are upper bounds, not
targets; long captions can still be hard to read. Audience is limited to 240
characters, goal and narration to 500, and the project contains at most 30 entities.
Captions are required; narration may be empty.

The scene entity ordering defines the mechanism:

- `hook`, `outcome`: empty entity list.
- `pipeline`: 2–5 entities in dependency order.
- `routing`: `[input, router, alternativeA, alternativeB]`; only A or B may be active.
- `parallel`: `[input, workerA, workerB, optionalWorkerC, join]`.
- `feedback`: `[draft, evaluator, revision]`, a bounded illustrative cycle.
- `compare`: two entities with nonempty details, under matched conditions.

Mechanism scenes require supporting claim IDs. Hook/outcome text may ask a
question or invite action; factual promises there also need supporting claims.
All claims must have evidence. Unknown fields, IDs, invalid timing and mismatched
source hashes are rejected rather than silently ignored.

## Outputs and review scope

`prepare` writes `spec.json`, `storyboard.md`, `captions.srt`, `narration.txt`,
`evidence-manifest.json` and `review.md`. Its review checklist starts pending.

The renderer writes the exact validated `motion.json`, `video.mp4`, early/middle/late storyboard PNGs,
`storyboard.html`, `captions.srt`, `narration.txt` and `render-manifest.json`.
Keep the original validated specification with these outputs. The renderer's
advanced entry point is:

```bash
node assets/renderer/render.mjs --spec /path/motion.json --output /path/new-render --stills-only
```

`--stills-only` is for visual inspection and deliberately does not produce an MP4.
The built-in output is silent. A narration script does not imply generated audio.
The initial themes use system fonts and code-native SVG/React shapes; there are
no remote runtime images or licensed reference assets in the composition.

For a custom scene, copy the skill into the run directory and keep exact
dependencies and its canonical Python validator together with the renderer. Animate from the frame number, not wall-clock CSS transitions or
random values. Add explicit semantic tests and inspect the actual output. Keep
input repositories and the distributable skill separate from one project's edits.

## Search the compact library

```bash
python scripts/motion.py search "object continuity" --kind case --limit 4 --json
python scripts/motion.py show PC01 --json
```

Source/case IDs are stable within this version. Searches return a bounded result
set. Evidence level belongs to the particular reference, never to the generated
video. Consult `frame-observations.json` for exact sampled-media provenance.

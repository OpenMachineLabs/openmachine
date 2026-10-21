# Compact reference research

Store discovery metadata and original bounded analysis, not source videos, GIFs,
audio, screenshots, transcripts, webpages, fonts or logos. Temporary research
downloads stay outside the package. Generated examples use original text/graphics.

## Two retrieval levels

`source-index.json` holds stable IDs, official URLs, publishers, actual medium,
topics, learning targets, dates and evidence levels. `motion-cases.json` holds
selected deeper analyses separating source statements, observations and our
proposed transfers. Search first; read relevant cases; reopen the original when
an interpretation matters. References are not evidence about the user's project.

## Evidence levels

| Level | Means | Does not establish |
|---|---|---|
| `page_verified` | Official page/text/transcript or link retrieved | Playback or pixel-level appearance |
| `frames_inspected` | Specific decoded frames visually inspected | Unseen intervals, audio, temporal quality |
| `segment_watched` | Recorded segment actually played and inspected | Entire video or general viewer outcomes |

Keep a scope note. For sampled media retain official URL, date, SHA256, duration,
frame/time samples and original observations. Source timing and proposed output
timing are different. A video player does not prove viewing. Retrieval failures
are gaps, never permission to invent contents.

## Add a source

1. Prefer the maker's page/channel, design explanation or source code.
2. Classify it: video, animation, interactive explanation, article or guideline.
3. Identify the concrete teaching problem it can help solve.
4. Inspect relevant media before making motion-specific claims.
5. Write short original notes; distinguish fact, observation and inference. Add a
   limitation and a counterexample to naive imitation.
6. Validate unique IDs/URLs and case-to-source links.
7. Keep original media and downloaded pages outside distributable directories.

Prefer diverse explanatory strategies over a count of similar launches: product
proof, UI cause/effect, algorithm state, scientific models, architecture, motion
semantics, data-driven personalization and concise typography.

Recheck when a case is used or revised. Historical work can teach a method without
describing a current product. Remove duplicates/empty entries. Keep the runnable
skill independent of reference network access. New patterns should improve a real
video before becoming mandatory. Readability, causality and truthful scope are
reviewable; taste still requires looking at the output.

## Using the official AI video studies

The 2026-10-21 addition covers eight official videos: two from OpenAI, three from
Anthropic and three from Google DeepMind. EX28–EX35 link to eight cases,
EC13–EC20, and eight inspected media assets. All use `frames_inspected`:
**zero continuously watched segments and zero audio inspections**. Related
screening clips are not counted as additional studies. Presenter or live-action
bridges are identified separately from the reusable explanatory sequences.

Search by the communication problem, then use `show` to read a case and follow
its `asset_ids` into `frame-observations.json`:

| Case | Search intent | Design question |
|---|---|---|
| EC13 | agent progress delegation handoff | What is active, what is delivered, and what still needs the user? |
| EC14 | coding agent diff result | How can a viewer inspect a change and its visible consequence? |
| EC15 | model planning causal intervention | How can a planned later word influence earlier output? |
| EC16 | prompt artifact interaction feedback | How does one request become an interactive result? |
| EC17 | cross application persistent context | How do several tools contribute to one task? |
| EC18 | sequence structure known unknown | Why is the problem difficult before a solution is named? |
| EC19 | prediction workflow confidence result | How should a completed prediction and its uncertainty be read? |
| EC20 | whole detail reference overlay | Where does a local detail belong in a registered comparison? |

`observed_timeline` records an asset, inspected start/end timestamps, an
observation and a separate design inference. These are selected decoded-frame
presentation timestamps, with the precision documented in each asset. They are
**sample brackets, not exact cut boundaries**. Intermediate actions, easing,
sound synchronization and real task latency remain unverified. Source seconds
also do not prescribe the timing of a new 30–60 second animation.

The seven YouTube assets use video-only format 136 renditions; their SHA256 values
identify the downloaded video bytes, not an audio track, editorial master or
every encoding at the same watch URL. The AlphaFold 7PNM asset instead identifies
the directly linked MP4. Container durations for YouTube are reported to 0.01s; PTS come from FFmpeg showinfo.
Stable URLs, retrieval dates, format identity and sample
scope make the notes auditable; expiring delivery URLs and media files stay out
of the package.

Choose a transfer rule, name the persistent object and meaningful state change,
then write an original scene for the project's evidence. Preserve the case's
limitations and counterexample. UI states and overlays explain what was shown;
they do not independently verify product reliability or hidden model reasoning.

EX24 and EX25 remain page-verified conceptual references for agent patterns.
Pair them with EC13 or EC17 for observed presentation techniques; the new videos
do not upgrade those articles' evidence levels. Search existing URLs and media
locators before adding a source; none of these eight media URLs was already indexed.

# Pilot brief — The lost address

**Question:** What gets lost when a relevant passage is cut out of a document, and how does Contextual Retrieval restore it?

**Audience / output:** Engineers, technical product teams and curious readers. A 40-second, English, 1920×1080, 30 fps, 16:9 film; comprehensible muted. One original procedural stereo score, no narration or licensed samples.

**Primary source:** Anthropic, [Introducing Contextual Retrieval](https://www.anthropic.com/research/contextual-retrieval), September 19, 2024. The article's illustrative SEC-filing example says a detached chunk contains “The company's revenue grew by 3% over the previous quarter.” Without document context it does not name ACME Corp or Q2 2023. Contextual Retrieval uses the whole document to generate concise chunk-specific context, then prepends it before embedding and BM25 indexing. The generated context is a preprocessing step, not a new explanation fabricated in response to the search query.

**Measured claim:** Anthropic reports an average top-20-chunk retrieval failure rate (1 − recall@20) of 5.7% for its baseline and 2.9% with Contextual Embeddings + Contextual BM25, a **49% relative reduction** across the evaluated domains under the article's setup. The film deliberately does not imply universal retrieval performance, a specific ACME test measurement, a causal downstream task improvement, or an evaluation of our simulated search illustration.

## Two real directions

- **A — document-first (selected):** Begin with an authored financial filing. A physical excerpt loses its return address, acquires an explanatory source band and travels into meaning and lexical index materials. The same excerpt remains legible throughout. This makes the causal mechanism visible without turning abstract boxes into the protagonist.
- **B — consequence-first (rejected):** Begin with a precise query looking through an archive field of scattered papers. A different dark composition and radial search visual are rendered as a Remotion still. It creates curiosity but obscures *why* the excerpt is difficult to find.

Both frames come from editable compositions (ContextualRetrieval and DirectionB); see the review artifact at artifacts/motion-studio/direction-study.jpg. A is not merely a recoloring of B.

## Shot plan and sound

| Approximate time | Viewer question / visible transformation | Sound intent |
|---|---|---|
| 00–05 s | Where is the answer? An authored SEC filing moves into the opening field. | Warm tone and single felt arrival |
| 05–11 s | What did chunking remove? The excerpt separates from the filing; its company/period become unknown. | Paper-cut cue and unresolved pluck |
| 11–19 s | What restores the lost relationship? The **same** strip receives a band naming filing, company and quarter. | Soft context-attachment chime and settling tone |
| 19–26 s | What gets indexed? Colored pathways animate from the contextualized text to an original woven semantic field and a token-matching ribbon. | Two spaced material notes, no mechanical stock sample |
| 26–32 s | Can the query retrieve the passage? A query selects a recognizable fragment; the illustration retains its provenance. | Search cue and quiet resolution |
| 32–37.5 s | What was actually measured? A calibrated 0–6% ruler reveals the sourced 5.7% versus 2.9% failure rates. | Restrained bed with two accents |
| 37.5–40 s | What remains? The contextualized strip becomes the closing visual signature. | Decaying harmonic close |

The selected **nine-second moving proof is frames 310–579**, covering the lost clue, the growing context band and the first two indexing paths. It is part of the full composition, not a disconnected title animation.

## Visual evidence and limitations

The supplied 39-second Bella TTS, 38-second OM-v2 and 39-second Open Machine comparison films informed the bar for original line art, persistent objects, changing compositions and authored typography. No graphics, code, recording, or brand artwork were copied from them. The supplied media files had video streams but no audio streams, so they do not establish a reference soundtrack.

All ACME financial details, physical documents, tokens, semantic wavefields and query paths in this pilot are **illustrative**; only the published failure-rate comparison is presented as measured evidence. The first production attempt relied on generic cards and repeated equal-weight layouts; the revised artwork replaces those with distinct archival and indexing materials. Before merging, a maintainer must still assess the complete video at actual playback speed with sound and at intended viewing size.

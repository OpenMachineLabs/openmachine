# Source-bound figure analyses

Run `analyze-packet` and open its source image link, or its PDF page locator,
using the host's multimodal inspection capability. An official article outbound
citation is not proof that the cited paper is an official Anthropic article.
Packets classify incidental linked papers as supporting literature; verified official papers need source-link and authorship provenance. For inline/dynamic
figures the link may lead to the article; use the supplied DOM/media locator.
Do not treat a link alone as proof that any pixels were inspected.

An analysis JSONL row requires the original `figure_id`, `article_id` and exact
`source_hash` from the packet. The hash binds the retained article content fingerprint and stable figure fields.
Refresh timestamps and HTTP-only wrapper changes do not invalidate it; changes to
content, captions, locators or asset identity do. It is not a
hash of remote image bytes when collection did not download them. Inspect again
when the source changes. A same-URL external asset change can remain undetected
until collection/inspection supplies new evidence.

Required fields:

| Field | Meaning |
| --- | --- |
| `family` | One of the 18 `ANALYSIS_FAMILIES` in `library.py`; separate from six `RENDER_FAMILIES` |
| `relationship` | The information relationship actually communicated |
| `editorial_purpose` | Reader problem, article argument and why this placement matters |
| `information_structure` | Entities, relationships, exclusions and reading order |
| `composition`, `palette`, `typography` | Specific inspected properties, with limitations |
| `production_hypothesis` | Plausible reconstruction; never asserted author tooling |
| `production_certainty` | Must be `uncertain` |
| `pixel_inspection` | Actual `locator`, ISO `date`, `inspector`, and pixel `method`; PDF also requires `page` and `region` |
| `evidence.observed` | Nonempty list of `{claim, locator}` objects; locator matches inspected pixels |
| `evidence.source_stated` | Exact retained `quote`, or explicit `method: context_review_paraphrase` with a source-bound `context_review`; never invent a quote |
| `evidence.inferred` | List of `{claim, locator, basis}`; basis identifies `observed:0` or `source_stated:0` etc. |
| `reusable_rules` | Nonempty list of concrete, conditional design rules |
| `counterexamples` | Nonempty list of cases where those rules fail |
| `cross_domain_transfer` | Object with `domain`, `mapping`, `changes`, `limits` |

Source-stated and inferred lists may be empty when there is no evidence of that
kind. Source excerpts outside the retained packet require an explicitly expanded
source workflow; do not substitute guessed quotes to pass validation. Describe
what a color, edge, size or label encodes, not merely that it exists. Explain a
concrete transfer and what cannot transfer (data, causality, scales, authority).
Avoid repeated generic prose across different figures.

Import checks the evidence shape, links, identities and freshness. It cannot
establish that an inspector is truthful, that a statement is visually correct,
or that inferred rationale was intended by the author. The 59 bundled studies preserve source reviewer inspection evidence; this implementation
pass did not repeat their pixel review. Test-only synthetic analysis fixtures stay
in disposable test stores and must never enter the knowledge index.

Accepted pixel methods are `official_image_pixels`, `official_pdf_screenshot`,
`rendered_page_screenshot` and `live_interaction`. `source_markup` is rejected.
A context paraphrase's `context_review` carries `source_hash`, the article `locator`,
`reviewer`, ISO `date` and `notes_origin`. It is an explicit reviewer statement
about source context, not a verbatim quotation or a schema-generated endorsement.
If `quote` is supplied, it must match the retained excerpt even when a paraphrase
receipt is also present. source reviewer's original note, group context and file hash
remain in `original_notes`. See coverage for the narrower Q1–Q3 PDF fingerprint.

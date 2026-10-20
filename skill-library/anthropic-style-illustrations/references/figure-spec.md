# Editable figure specifications

Use the complete executed JSON examples as working templates. Required common
fields are `schema_version: 1`, `input_hash` (SHA-256 of the exact UTF-8 article),
`title`, `family`, `caption`, `alt`, `claims`, and `references`. Optional fields
are `subtitle`, `annotations` (strings), `palette`, and an actual `semantic_review` receipt.
`palette` accepts `engineering` (restrained green-white), `scientific` (white with
neutral gray-blue structure), or the existing `editorial` cool palette.
The renderer never invents chart data, labels, edges or editorial claims.

Each claim has unique `id`, `section_id`, exact input `quote` and original `text`.
Each node/item/data point binds to nonempty `claim_ids`. An exact quote proves
an anchor exists, not that a paraphrase follows from it: host review owns that.
Each reference binds original `figure_id`, `article_id`, `source_hash`, exact
`source_url`, `evidence_level`, and a concrete `transfer` description.
`validated_analysis` references must also embed the validated `analysis` record.
Metadata-only references cannot support claims about uninspected visual details.

| Family | Content |
| --- | --- |
| `flow` | Explicit `nodes`: id, label, row (0–12), column (0–4), claim_ids. `edges`: from, to, meaning, claim_ids. Fork/join/feedback are explicit edges. |
| `comparison` | Two to five `columns`; `items` each contain id, a matching `cells` array, claim_ids. Rows align across alternatives. |
| `matrix` | Same cell schema, with criteria represented consistently in columns/rows. |
| `bar` | `unit` and 1–30 `data` objects: id, label, finite nonnegative value, evidence, claim_ids. Zero baseline. |
| `line` | Bar fields plus `x_unit` and each point's numeric `x`; at least two strictly increasing x values. |
| `cover` | One to four `items`: id, original label, claim_ids. Labeled conceptual, without quantitative encoding. |

Charts require each label, value and unit in an exact narrow `evidence` span
within one bound claim. After removing the label, the span must contain exactly
one number (the bar value), or exactly x then y for a line point; this rejects
swapped values borrowed from a neighboring observation. Use one sentence or table
cell per observation. Avoid dates and unrelated numbers in that span; line x and
x_unit must also appear. Keep each point bound to its own narrow claim to avoid
ambiguous numeric associations. NaN, infinity, booleans, negative values, missing
units, unordered x and unknown claims are rejected. Negative-domain charts need
a different explicitly reviewed design, not an undocumented shifted baseline.

The vector composition uses a cool neutral background, dark hierarchy, teal/blue
accents, aligned tables, direct quantitative marks and explicit handoff routes.
There are no bundled or copied fonts. Text is escaped, wrapped without ellipsis
and never silently truncated. Long unbroken words are wrapped by character.
Canvas height grows within 12,000 SVG units; oversized diagrams fail rather than
clip. Complex skip/feedback routes that cross nodes fail with a rearrangement
message; this is a bounded explicit layout engine, not an arbitrary graph router.
Edge meanings are fully retained in an annotation key below the graph.

SVG text remains editable. HTML embeds the same SVG with a restrictive CSP and
no scripts or external resources. PNG uses existing PyMuPDF. Cross-platform font
fallback and complex-script shaping can differ; non-English text preservation is
not a claim of typographic acceptance. Inspect actual outputs at intended size.

Machine QA revalidates input/claims/references/data and recomposes SVG; it rejects
artifact mismatch and checks canvas/node/connector geometry. It does not prove
source truth, semantic entailment, contrast, all glyph bounds, interaction,
visual balance or all label-to-label collisions. `visual_review: NOT RUN` is
intentional until a real separate review is recorded outside machine QA.

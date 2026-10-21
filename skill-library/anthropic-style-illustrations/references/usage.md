# Offline usage

Run from this Skill with an environment containing `PyMuPDF==1.26.3`:

```sh
.venv/bin/python -B scripts/illustrate.py verify-index
.venv/bin/python -B scripts/illustrate.py catalog 'technical evaluation' --limit 5
.venv/bin/python -B scripts/illustrate.py search 'negative branch' --family flow
.venv/bin/python -B scripts/illustrate.py show X2
.venv/bin/python -B scripts/illustrate.py search E8 --include-contrast
.venv/bin/python -B scripts/illustrate.py plan examples/article.md --budget 2 --out runs/plan.json
.venv/bin/python -B scripts/illustrate.py illustrate examples/article.md --out runs/draft
.venv/bin/python -B scripts/illustrate.py render examples/article.md examples/figure.json --out runs/example
.venv/bin/python -B scripts/illustrate.py qa examples/article.md examples/figure.json --out runs/example
.venv/bin/python -B -m unittest discover -s ../../test/component/anthropic_illustrations -p 'test_*.py'
```

`plan --out` names a JSON file; other output arguments name directories. QA also
prints JSON without an output directory. Exit status: 0 success, 1 failed QA,
2 invalid input/render error. Rendering emits `figure.svg`, `figure.png`,
`preview.html`, editable `figure.json`, and `qa.json`. All writes must resolve
inside the Skill; symlink paths and existing hard-linked outputs are rejected.
Do not use a directory another process is concurrently replacing.

## Two tiers of source knowledge

- `article-index.json` is the **broad** public-source discovery catalog: official article URLs, topical categories and figure/page locators. Each `catalog` result has `evidence_level: metadata_only`; an image URL or embedded-page locator is not a claim of source-pixel inspection. Follow the links online at design time to confirm relevance and current content.
- `source-index.json` is the **deep** linked figure-analysis library. Use `search` to shortlist information relationships and `show CASE_ID` to inspect what was observed, what the source said, what was inferred, why the composition helps and when not to reuse it. Evidence marked `retained_prior_review` came from earlier attributed AI reviews; `current_pixel_review` denotes bounded later AI pixel inspection. Neither denotes human sign-off.
- `verify-index` gives actual current counts and rejects malformed links, unreviewed cases masquerading as pixel analyses, duplicate URLs and invalid structure. No source HTML, PDF or raster bytes are shipped. The local index is a **snapshot**, not a live availability guarantee; source website indexes may change.
- More analyzed visual forms do not enlarge the deterministic renderer automatically; a complex network, multi-panel, statistical uncertainty chart or photographic comparison may require a separate original graphic guided by those references.

## Host-reviewed, whole-article illustration plan

`plan` reads every Markdown section, ignores fenced-code headings and returns a heuristic inventory. To turn it into a deliberate article-wide editorial plan, the host should read the complete article first and create a reviewed JSON decision for **every** section:

```json
{
  "article_sha256": "<SHA256 of the entire original article bytes>",
  "audience": "Technical engineers evaluating the proposed system",
  "thesis": "One sentence stating the article's actual main claim",
  "reviewer": "Host article-context semantic review",
  "rationale": "Why these images, and why the remaining sections stay as prose",
  "decisions": [
    {
      "section_id": "section-2",
      "decision": "image",
      "reason": "Explicitly compare two source-described paths",
      "reader_question": "When does each path apply?",
      "family": "comparison",
      "quotes": ["An exact substantive excerpt from section 2."],
      "case_ids": ["C1"]
    },
    {"section_id": "section-1", "decision": "no-image", "reason": "Already clear in prose"}
  ]
}
```

The example illustrates fields, **not** a valid complete plan for any particular article; include all of that article's section IDs with exact matching excerpts. Validate using `plan ARTICLE --reviewed runs/plan-reviewed.json --out runs/plan-validated.json`. A targeted `--section section-N` still requires the whole article and every-section decisions. Semantic truth remains the host's responsibility.

## Editable spec, version 1

See [the workflow example](../examples/figure.json). Required fields:

- `version`: 1; `article_sha256`: SHA-256 of the **complete original bytes**,
  including line endings; `title`: nonempty text, at most 160 characters.
- `family`: `flow`, `comparison`, `matrix`, `bar`, `line`, or `cover`.
- `nodes`: 1–16 objects with unique `id`, `label` (up to 600 characters), and
  `quote` (an exact nonempty substring of the article, up to 4000 characters).
- `case_refs`: nonempty list of `{case_id, article_url}` matching the local
  index exactly. E8 cannot be a design target. Case families need not equal
  render families: explain the structural adaptation during host review.
- `edges`: optional, flow only; up to 32 `{id, from, to, label, quote}` objects.
  Endpoints must exist; edge IDs are unique and distinct from node IDs.
  Quotes must support the relationship, not merely mention both endpoints.
- `citations`: optional `{label, url, quote}` objects. HTTP(S) URLs and exact
  quotes must occur in the article. Markdown links in node/edge/cell quotes are
  also retained as clickable SVG and HTML links.

IDs begin with an ASCII letter and contain up to 64 letters, digits, `_` or `-`.
Labels are editable paraphrases; code cannot determine whether they are true.
To rebind after an article edit, calculate its SHA with `hashlib.sha256` on
`Path(...).read_bytes()`, then recheck every claim before updating the spec.

Family-specific fields:

| Family | Additional fields and interpretation |
| --- | --- |
| flow | Edges show explicitly authored dependencies, including returns. |
| comparison | Equal-weight peer cards; no inferred causality. |
| cover | Single-column concept/excerpt cards; no external artwork. |
| matrix | `columns`: 1–4 labels; each node has matching `cells`: `{text, quote}`. |
| bar | `unit`, `baseline: 0`; each node has numeric `value`. |
| line | Same as bar; at least two nodes; ordered categories with equal spacing. |

Charts accept only finite nonnegative values. Each value must appear next to
its literal unit in its own exact claim quote (for example `12 ms`). Different
numeric spellings with the same value are accepted; derived values, unit
conversion, commas in numbers, negative values, scientific notation, and
percentage-point arithmetic are unsupported. Line spacing is ordinal, not a
time or numeric x-axis. Review ordering and comparability manually.

## Limits and approval

The index is a preserved summary of prior attributed source reviews. Validation
checks its structure and counts offline, not link availability or image pixels.
Search ranks token matches across case evidence and boundaries; E8 is excluded
unless explicitly requested. Plan reads all sections, including text after
fences; fenced headings cannot create sections. Selection is a length/budget
heuristic. Automatic drafts take up to four prose lines and invent no edges.

Text is escaped and wrapped with conservative character estimates. Complex
scripts/font coverage, long URLs, dense return paths, and unusual glyph widths
still need PNG inspection. A successful PNG export does not prove legibility.
QA verifies SHA, anchors, IDs, links, and numeric evidence; it cannot prove
entailment, correct emphasis, chart comparability, or publication readiness.
No automatic network access, source-media copying, crawling, or external image generation occurs. To update the source catalogs, independently check official article/image links and retain short attributed metadata and observed-versus-inferred design notes; do not copy full webpages, PDFs, image bytes, screenshots or runtime collection journals into this Skill.

# Offline usage

Run from this Skill with an environment containing `PyMuPDF==1.26.3`:

```sh
.venv/bin/python -B scripts/illustrate.py verify-index
.venv/bin/python -B scripts/illustrate.py search 'negative branch' --family flow
.venv/bin/python -B scripts/illustrate.py search E8 --include-contrast
.venv/bin/python -B scripts/illustrate.py plan examples/article.md --budget 2 --out runs/plan.json
.venv/bin/python -B scripts/illustrate.py illustrate examples/article.md --out runs/draft
.venv/bin/python -B scripts/illustrate.py render examples/article.md examples/figure.json --out runs/example
.venv/bin/python -B scripts/illustrate.py qa examples/article.md examples/figure.json --out runs/example
.venv/bin/python -B -m unittest discover -s tests -p 'test_*.py'
```

`plan --out` names a JSON file; other output arguments name directories. QA also
prints JSON without an output directory. Exit status: 0 success, 1 failed QA,
2 invalid input/render error. Rendering emits `figure.svg`, `figure.png`,
`preview.html`, editable `figure.json`, and `qa.json`. All writes must resolve
inside the Skill; symlink paths and existing hard-linked outputs are rejected.
Do not use a directory another process is concurrently replacing.

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
No network, source-media copying, crawling, or external image generation occurs.

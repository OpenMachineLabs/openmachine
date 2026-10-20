# Usage

Run from this Skill directory with the existing environment. No package install,
service, browser, network or Core integration is needed for local commands.

```sh
.venv/bin/python -B scripts/illustrations.py --help
.venv/bin/python -B scripts/illustrations.py audit
.venv/bin/python -B scripts/illustrations.py search "orchestrator workers workflow" --family flow --limit 3
.venv/bin/python -B scripts/illustrations.py show figure_cc54e65f66be5075d3a5652c
.venv/bin/python -B scripts/illustrations.py analyze-packet figure_cc54e65f66be5075d3a5652c --out runs/packet.json
.venv/bin/python -B scripts/illustrations.py plan examples/cross-domain/article.md --budget 1 --section section-2 --out runs/plan.json
.venv/bin/python -B scripts/illustrations.py demo all --out runs/demo
.venv/bin/python -B scripts/illustrations.py render examples/executed/cross-domain/figure.json --article examples/cross-domain/article.md --out runs/render --png
.venv/bin/python -B scripts/illustrations.py qa runs/render/figure.json --article examples/cross-domain/article.md --out runs/render
```

`--knowledge PATH` precedes the command. JSONL indexes are read line by line.
BM25 uses two passes, document-length normalization (k1=1.2, b=0.75), filters English stopwords, weights explicit relationship matches, ranks by relevance
and deduplicates asset IDs. A weak reviewed match does not outrank a strong metadata
match merely because it was reviewed.
Relationship filtering on an analysis uses its explicit relationship; on metadata
it is a lexical filter, not a visual judgment. Family inference from metadata is
also only a lexical hint. `--reviewed-only` means current shape-valid imported
records; it does not mean independent approval. `--include-supporting` opts in
to incidental literature and landing-page metadata. Negative contrast E8 stays excluded. `--analyses runs/validated.jsonl` selects an import output.
The article join and analysis map are held in memory; JSONL file bytes are not
read wholesale. Collection keeps mutable metadata tables in memory for recovery.

The CLI planner is a draft heuristic. It preserves every Markdown section,
fenced blocks, full input text, line anchors and SHA-256, even with `--section`.
ATX and Setext headings are supported; this is not a complete Markdown AST.
A host LLM should read the whole article, reconsider no-image decisions, explain
placement and family selection, and preserve uncertainty. Non-English input is
retained verbatim; lexical cues cannot replace semantic review in any language.

To submit an actual reviewed plan, keep `input_hash`, `full_context` and one
`decisions` entry for every section. Add `review` with nonempty `reviewer`, `date`
and `rationale`; each illustration needs `family` and exact section-text
`evidence`. Run `plan ARTICLE --reviewed PLAN --out OUTPUT`. The CLI checks the
receipt and anchors; it cannot certify that the named reviewer performed review.
A figure spec can independently carry a `semantic_review` receipt with the same
fields. Do not add either receipt merely because generation succeeded.

For actual host visual analyses, follow the [rubric](analysis-rubric.md), then:

```sh
.venv/bin/python -B scripts/illustrations.py import-analyses runs/incoming.jsonl --out runs/validated.jsonl
.venv/bin/python -B scripts/illustrations.py search "delegation" --analyses runs/validated.jsonl --reviewed-only
```

Import validates incoming rows under an exclusive destination lock, upserts atomically,
retains unrelated rows and replacement history, detects duplicate occurrence/case IDs,
and is a byte-preserving no-op on identical input. The default destination is
`knowledge/analyses.jsonl`; `--out` may select a separate store. Validation does not
endorse inspection claims. Outputs, collector stores and temporary siblings must resolve
inside this Skill; symlink escapes and hard-linked existing output files fail.

`discover` and `collect` are working network commands, disabled unless
`--allow-network` is explicitly supplied under a separate collection authorization.
Do not run them during an offline task. Defaults cap discovery/collection at 20;
`collect --include-pdfs --max-linked-pdfs 20` additionally bounds linked PDF fanout.
No source image or full article is stored. Discovery refresh currently rescans
indexes; it does not implement conditional index caching.

Executed examples are in `examples/executed/`. The article example is a complete
original reading guide grounded in recovered metadata from a real publication;
it does not represent full-source-text ingestion or pixel review. The repair
case transfers a relationship question across domains. Family examples use real
local index counts and original conceptual designs. Every output remains a draft
until semantic and visual review. See [implementation review](implementation-review.md).

## Full publication and original cross-domain examples

```sh
.venv/bin/python -B scripts/publication_examples.py
.venv/bin/python -B scripts/illustrations.py publication-context https://www.anthropic.com/engineering/building-effective-agents --allow-network --network system --out runs/publication-context.json
```

`publication-context`, `discover` and `collect` accept `--network direct|system`
(default `direct`). System mode uses the managed host proxy/DNS environment;
both modes retain TLS verification, host restrictions and robots checks.
`publication-context` uses SafeFetcher, fetches exactly one
official article, reads its editorial sections transiently and retains hashes and
heuristic decisions. It never saves the full source. Its host must review meaning;
this command is not an LLM. The original example used web-extracted editorial text. Subsequent actual native
acceptance resolved the transport gap; see `examples/full-publication/native-acceptance.json`.
The original fallback coverage remains historical evidence.

The new original SVG/PNG/HTML/spec/QA outputs are in `examples/full-publication/`
and `examples/archive-pilot/` (including `message-detail/`). The archive article is
complete original prose; the official example retains an original brief, all-section
coverage, six words of exact anchors and no full copyrighted source. Semantic
self-review and actual AI PNG review are recorded separately in `visual-review.md`.

## One complete offline use example

From this Skill directory, render the existing original repair-workflow spec
against its complete article, then open `runs/repair/figure.png` for visual review:

```sh
TMPDIR="$PWD/runs/tmp" .venv/bin/python -B scripts/illustrations.py render examples/executed/cross-domain/figure.json --article examples/cross-domain/article.md --out runs/repair --png
```

The command exports editable JSON, SVG, PNG, HTML and mechanical QA. It does not
fetch anything or confer human approval. Create `runs/tmp` inside this Skill first
if it does not exist. Runtime `runs/`, `.venv/`, raw logs, collector journals, lock
files and source images are not delivery artifacts.

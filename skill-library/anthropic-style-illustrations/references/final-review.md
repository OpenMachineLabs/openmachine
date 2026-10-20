# Final targeted review

2026-10-20. Codex AI author review; local report only, not independent human
approval. Requested targeted fixes and actual nine-PNG review are complete.
No Git operations, PR claims, network fetches, services, Core, root configuration
or CI changes were performed.

## Changes and focused acceptance

- PASS: CLI `discover`, `collect` and `publication-context` expose and dispatch
  `--network direct|system`, default `direct`. Publication transport receipts
  reflect the selected mode; injected test fetchers are labeled as such.
  No TLS or robots bypass was added.
- PASS: shared offline fixtures moved to `tests/fixtures.py`; curation no longer
  imports a test module. All tests and temporary files stayed inside this Skill.
- PASS: duplicate footer URLs replaced by short deduplicated labels; complete
  URLs remain in metadata and HTML links. Explicit arrowhead geometry survives
  PNG conversion. Flow keys use displayed card names and separate lines.
  Line ticks and x-axis label added without changing source data.
- PASS: 59 offline unit tests, 0 failures, 0 errors (56 existing + 3 targeted
  tests covering dispatch, transport receipts and footer deduplication).
- PASS: nine examples regenerated, nine QA/spec/SVG equality checks passed,
  nine actual PNGs opened with the image viewer. Specific observations and
  original/final image hashes are in [visual-review.md](visual-review.md).
- PASS: all knowledge files byte-identical to the start of this pass, including
  original Q1–Q3 narrow hashes, pages, regions and source-reviewer provenance.
- PASS: documentation uses source reviewer outside preserved original provenance,
  includes one complete offline use example, and resolves the historical blocked
  verdict without deleting its history.

Commands, run from this Skill with `TMPDIR="$PWD/runs/tmp"`:

```sh
.venv/bin/python -B -m unittest discover -s tests -p 'test_*.py'
.venv/bin/python -B scripts/illustrations.py demo all --out examples/executed
.venv/bin/python -B scripts/publication_examples.py
.venv/bin/python -B scripts/illustrations.py audit
```

A final local Python check called `qa` for each of the nine specs with its own
article/brief and output directory, verified full URLs in HTML, and verified
knowledge hashes. The final test run passed after the last code changes.
The only emitted warning was the existing PyMuPDF SWIG deprecation warning.
No broad root test suite was run.

## Exact corpus audit

| Measure | Count |
| --- | ---: |
| Documents | 913 |
| Figure occurrences | 3,858 |
| Assets | 3,697 |
| Analyses / valid analyses | 59 / 59 |
| Stale / invalid analyses | 0 / 0 |
| Duplicate IDs across all audited tables and case IDs | 0 |
| Broken references / zero-data errors | 0 / 0 |
| Source publications | 849 |
| Verified official papers | 1 |
| Supporting literature | 62 |
| Landing pages | 1 |
| Reviewed sources / families | 14 / 18 |
| Negative contrasts | 1 |
| Existing collection failures, not retried | 24 |

## Actual native acceptance, retained without rerunning

Native OM execution: `c26b3a409d2e4ac29f1ba3c1e04d5096`.
The supplied actual receipts and user-verified execution resolve the previous
sandbox transport failure; they are not a new network run by this final pass.

- Prior native candidate: 56 tests PASS.
- Full article: SafeFetcher(network=system), PASS, 19 sections, 19 decisions,
  2,737 words; input SHA-256
  `abccd0508a997f989856da3654255eb49a76133d0d53c64716e54f6a34ea5ff9`.
- Article response SHA-256:
  `8f599310f14e76b15a8fe154ab131b55164b780680d7a6a970c23fe8ada11fc9`.
- PDF: PASS, 4,919,189 bytes, 62 pages, Anthropic affiliation present; full SHA-256
  `0acd3e05e40c1b703be3aed15c8cdbd5713a1840df9738b495d2bde2e8eb1e3a`.
  No PDF bytes stored. Title is blank in the receipt; no title was invented.

Sanitized compact receipts:
[article/native acceptance](../examples/full-publication/native-acceptance.json),
[PDF version evidence](../examples/full-publication/paper-version.json).
They retain no native filesystem paths, raw logs or full source. The PDF digest
is version evidence only, not fabricated new Q1–Q3 visual approval. Fresh
companion DOM inspection and source pixel review were NOT RUN. The original
fallback coverage remains distinct from subsequent native acceptance.

## Current module review and unresolved finding

Reviewed CLI dispatch and direct callers, publication extraction, renderer and
QA, planning/spec validation, retrieval/evidence validation/import/audit, and
collector network/refresh boundaries for material correctness and evidence loss.
No P0/P1 defect was identified in this bounded review; this is not an exhaustive
security or concurrency audit. No new dependency or architecture was introduced.

**Resolved P2 — preserve negative-contrast exclusion after stale validation.**
In `scripts/asi/library.py`, `search.candidates`, an invalid/stale analysis is
set to `None` before testing `usage == "contrast_not_target"`. When E8 becomes
stale after a future source refresh, default search can return its metadata as
a style candidate, contrary to the documented exclusion. Reproduced offline
with an in-memory E8 validation failure and its source-title query: E8 returned.
Current E8 is valid and excluded, so current examples and audit are unaffected.
The smallest follow-up is to retain the exclusion before discarding stale
analysis content. This retrieval-policy change was not included in the frozen
CLI/fixture/rendering fixes; no stale corpus mutation was performed.

## Delivery boundaries

All write destinations and changed delivery paths resolve within this Skill.
A local pre/post SHA-256 manifest verifies this turn's in-Skill changes; no Git
diff or claim about unrelated pre-existing changes is made. Runtime `runs/`,
`.venv/`, caches, raw logs, collector journals and lock files are excluded from
delivery. Local ignore rules cover collection journals and locks; no source
images were added. No archive, release or external publication was created.

Visual acceptance is limited to these nine English PNGs at viewer display size.
Small-size/mobile and cross-platform typography were NOT RUN. Literal plural
unit labels at value 1 remain a minor copy limitation. Mechanical QA cannot
prove general semantic entailment or human approval. No new full official-article
semantic review, network test, service test, root runner, packaging or CI run
was performed. Stop condition reached: targeted deliverables saved, focused
acceptance recorded, unresolved finding disclosed.

## Final source-reviewer repair

The stale-negative retrieval defect above is fixed: explicit negative usage is checked before freshness validation discards analysis content. Exclusion persists until replacement review. A real-corpus E8 regression verifies stale exclusion. Final scoped suite: 60 tests PASS. No corpus or PNG bytes changed in this repair; the nine hash-bound visual reviews remain applicable. Earlier counts and original findings are historical.

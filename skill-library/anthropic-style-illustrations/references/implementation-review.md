# Targeted implementation review and curation

**Current transport verdict: RESOLVED by actual native acceptance.**
See the resolution below and [final review](final-review.md) for current tests,
actual PNG review and the remaining retrieval finding. The earlier blocked
verdict is preserved below as history.

Local review of this Skill only, 2026-10-20. No Git operations, PR, Core/config/CI
or service changes. All task writes, tests and temporary files stayed here.
This is an author self-review, not independent approval.

**Historical verdict (superseded by native acceptance below): code and curation checks PASS; the exact requested full-publication
transport acceptance is BLOCKED.** The local SafeFetcher attempt failed at DNS
before retrieval. The connected native command was rejected because approval
policy is `never`; it did not execute. No confirmation can be requested under
that policy. A complete web extraction was used as an explicitly recorded
fallback, not relabeled as a successful SafeFetcher run.

## Validated corpus and curation

| Measure | Actual result |
| --- | ---: |
| Documents | 913 |
| Figure occurrences | 3,858 |
| Assets | 3,697 |
| Original source reviewer analyses preserved | 59 |
| Valid source/evidence records | 59 |
| Stale analyses | 0 |
| Invalid analyses | 0 |
| Duplicate article/figure/asset/analysis/case IDs | 0 |
| Broken references / zero-data errors | 0 / 0 |
| Reviewed source publications/papers | 14 |
| Reviewed analysis families | 18 |
| Negative contrasts | 1 (E8) |
| Default-search reference candidates | 58 |
| Existing collection failures, not retried | 24 |
| Unresolved image filename matches | 0 |

All 56 image notes matched exactly one occurrence in their stated corpus source.
Q1–Q3 were manually registered at PDF pages 2, 7 and 11 with the original exact
region descriptions. Their original inspection method, date, inspector, wording,
observations, paraphrases, inferred rationale, uncertainty and transfers remain
in `knowledge/analyses.jsonl`. The five original curation files were not edited;
complete original study objects and group context are also embedded in the index.
The [case catalog](case-catalog.md) presents the same evidence readably.

The PDF authorship was checked against its first-page Anthropic affiliation.
Official companion linkage is preserved from the supplied source reviewer review.
The source fingerprint hashes actual web-extracted PDF text lines 0–91, explicitly
not full PDF bytes. Full byte retrieval and a fresh companion DOM check remain
unverified. This is a material freshness limit for Q1–Q3, not a newly performed
pixel review. A full-PDF refresh will invalidate the narrow fingerprint while
preserving the manual locators. There are no unresolved guessed filenames.

The source classification is 849 source publications, 62 incidental supporting
records, one landing page and one explicitly verified official paper. These are
record counts, not a claim that every embedded asset was authored by Anthropic.
[Coverage](coverage.md) and `knowledge/coverage.json` report actual source/family
counts and gaps without padding. The rest of the corpus remains unreviewed.

## Implemented fixes and review scope

- `library.py`: 18 analysis families are separate from six render families.
  Stable hashes use article content and figure identity/context/locator fields,
  excluding observation timestamps and HTTP-only wrapper changes. Imports allow
  the default index, lock the destination, validate before atomic replacement,
  preserve unrelated records, reject duplicates, retain replacement history and
  perform a byte-preserving no-op for identical input.
- Evidence validation accepts explicitly source-bound context-review paraphrases
  without inventing quotes. Supplied quotes still require exact retained context.
  Wrong article/figure hashes, locators, PDF pages/regions, unsupported pixel
  methods and `source_markup` pixel claims are rejected. Validation is never
  presented as endorsement or authentication of a reviewer.
- Retrieval filters English stopwords, scores explicit relationship matches and
  ranks relevance before review status. It no longer searches the complete
  provenance JSON as relevance text. Incidental literature, landing pages and
  E8 are excluded from default style retrieval but preserved as evidence.
- Audit calculates valid/stale/invalid counts, duplicate IDs, zero-data errors,
  classifications and coverage. It reports retained inspection receipts rather
  than hardcoding that no review occurred.
- `corpus.py`: manual PDF regions and explicit official provenance survive
  refresh. A changed document still stales its analyses. No bulk crawl ran.
- `publication.py` and the bounded `publication-context` CLI read one official
  article through SafeFetcher `network=system`, parse editorial sections
  transiently, and retain coverage hashes and draft decisions. This is a
  keyword heuristic, not general LLM semantic understanding. Its parsing path
  passes fixture tests; its required native network run remains blocked.
- `rendering.py` adds explicit green-white engineering and neutral scientific
  palettes without global palette mutation. Six existing render families remain.
- `publication_examples.py` regenerates the original examples from retained
  briefs/articles and reviewed cases. Patterns link actual case and figure IDs,
  including baseline–method–results and matched useful/negative console groups.
- Review also covered CLI callers, planning/spec validation, all new behavioral
  regressions, updated documentation and the generated source/spec/QA contracts.
  No PNG was opened or visually inspected by this pass.

Final review caught and fixed a missed manual-refresh edit, a test glob that also
matched an audit JSON file, malformed context-review handling and missing exact
PDF-region comparison. The demo coverage labels now separate landing pages.
No independent reviewer, external publication or Git-based diff check was used.

## Executed commands and results

Run from this Skill directory. `TMPDIR` stayed in `runs/tmp`; Python used `-B`.

```sh
TMPDIR="$PWD/runs/tmp" .venv/bin/python -B -m unittest discover -s tests -p 'test_*.py' -v
TMPDIR="$PWD/runs/tmp" .venv/bin/python -B scripts/illustrations.py demo all --out examples/executed
TMPDIR="$PWD/runs/tmp" .venv/bin/python -B scripts/publication_examples.py
.venv/bin/python -B scripts/illustrations.py import-analyses knowledge/analyses.jsonl
.venv/bin/python -B scripts/illustrations.py audit
```

- PASS: 56 tests, zero failures/errors. Regressions include retained upserts,
  idempotency/history, incoming/existing duplicates, lock contention, atomic
  write failure, unchanged versus changed refresh, manual PDF preservation,
  paraphrases/quotes/wrong sources, pixel methods/page/region, real audit counts,
  fork/routing/feedback/population retrieval, full-context section selection,
  reasonable no-image decisions, whole-publication parsing and palette options.
- PASS: all six existing demos exported SVG/PNG/HTML/spec/QA with updated source
  hashes and real coverage. The original article/repair demos remain explicitly
  separate from the newer full-context fallback example.
- PASS: three new original compositions exported SVG/PNG/HTML/spec/QA. Semantic
  self-review is recorded separately; every QA says `visual_review: NOT RUN`.
- PASS: import validated 59 records, changed 0, total 59; no endorsement asserted.
- PASS: audit counts shown above. Exact receipts are in `runs/test-results.txt`,
  `runs/demo-results.json`, `runs/publication-results.json`,
  `runs/import-idempotency.json` and `runs/corpus-audit.json`.
- FAIL/BLOCKED: requested native SafeFetcher fetch. Local DNS failure followed by
  native tool approval rejection; no bypass, bulk crawl or repeated retry.
- NOT RUN: root runners, premerge, CI, packaging, services and Git checks, outside
  the user's scope. No broad tests or dependency installation were attempted.
- NOT RUN: generated PNG visual review, new reference pixel inspection and
  cross-platform typography. source reviewer owns the requested PNG inspection.

## Publication artifacts

The official [Building effective agents](https://www.anthropic.com/engineering/building-effective-agents)
article was read through all 19 editorial sections, including both appendices,
using web extraction after the native fetch failed. All editorial lines 13–178
were present; overlapping windows were deduplicated. The temporary complete text
was removed after review. `examples/full-publication/source-coverage.json` retains
URL, extracted-text hash, section hashes/counts, six words of exact anchors and
the transport limitation. It does not claim an HTTP response hash it lacks.
`plan.json` records a decision for every source section and one grouped figure.
`brief.md` and `design.md` are original semantic/design writing.

The complete original archive-pilot article has seven substantive sections plus
its title. Its reviewed plan chooses two figures, leaving selection, population
caveats and straightforward boundaries in prose. No pilot measurements or
permissions outcomes are fabricated. References A4/A6/P2 explain traceable
handoffs; E5/E6/E8 support a matched recoverability comparison, with E8 explicitly
negative. The article retains F4's denominator lesson without inventing a chart.

PNG paths for source reviewer:

- `examples/full-publication/figure.png` — engineering comparison, SVG 1200×679.
- `examples/archive-pilot/figure.png` — neutral workflow, SVG 1200×913.
- `examples/archive-pilot/message-detail/figure.png` — matched messages, SVG 1200×614.

Each directory also contains `figure.svg`, `figure.json`, `preview.html` and
`qa.json`. The PNGs are generated original artwork, not source-media copies.

## Remaining acceptance boundary

The exact requested TRUE SafeFetcher full-publication workflow is not complete.
Its only executed full-article version is the documented web fallback. Remaining
work is the single authorized native article fetch when execution policy permits,
and a bounded full-PDF fingerprint/provenance reconciliation for Q1–Q3. source reviewer's
visual inspection is also pending, as requested. Existing source-media changes at
unchanged URLs remain undetectable without bytes, and schema checks cannot prove
semantic correctness, aesthetic quality or inspector truthfulness.

## Resolution after the historical sandbox attempt

The actual native OM execution `c26b3a409d2e4ac29f1ba3c1e04d5096`
resolved the earlier transport blocker: 56 tests PASS, full article PASS
(19 sections, 19 decisions, 2,737 words), PDF PASS (4,919,189 bytes, 62 pages,
Anthropic affiliation present, no bytes retained). Compact sanitized receipts
are beside the examples. The old failure account above is history, not the
current verdict. See [final review](final-review.md) and [visual review](visual-review.md).

The full-PDF digest is additional version evidence only. Q1–Q3 keep their
original narrow source hash, pages, regions, dates and inspection provenance.
No new reference pixel review or fresh companion DOM check is asserted.

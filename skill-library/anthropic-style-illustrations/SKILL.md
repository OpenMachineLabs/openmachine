---
name: anthropic-style-illustrations
description: Plan, research, design and export original evidence-grounded editorial diagrams or charts for a complete technical article, using link-only Anthropic engineering, research and developer references. Use for article illustration placement, editable figure briefs, visual analysis and SVG/PNG/HTML output.
---

# Anthropic-style editorial illustrations

**The article determines the figure; a reference does not determine the claim.** Read the entire article, including the opening, method, results, caveats and conclusion, before selecting a visual. The host model performs semantic and visual judgment; the Python commands supply discovery, structured drafts, deterministic output and verifiable boundaries.

## Find the right reference

- `references/article-index.json`: wide discovery index of official articles and linked image/page locators. `catalog "agent evaluation"` returns **metadata-only** references. Treat URLs as leads, not verified current media or visual analysis.
- `references/source-index.json`: detailed inspected cases. `search "feedback and held-out evaluator"` ranks relationships; `show X2` gives observed pixels, source statements, separately identified inferences, editorial purpose, composition, misuse limits and transfer guidance.
- `references/design-patterns.md`: concise decision rules and counterexamples. Preserve the explicit negative case E8 as a contrast, never a positive template.
- Follow article links and inspect the official source image **online when needed**. Note inaccessible/changed media rather than pretending it was seen. A recorded AI visual review is not independent human endorsement. Never copy the original image, page, PDF or site branding into an output.

## Plan illustrations from the full article

1. Establish the reader, central thesis, evidence hierarchy, uncertainties and an illustration budget. Sketch the information structure of the whole article, including what is already clear as prose.
2. Give **every section** an explicit `image` or `no-image` decision. For each image, state the reader question, exact supporting article excerpts, needed relations, recommended chart/diagram family, relevant case IDs and what a visual would add. Use the whole-article context to avoid repeated/contradictory figures.
3. `plan` makes a full-section inventory and **heuristic suggestions**, not editorial decisions. Supply a host-authored `--reviewed` plan with a complete article SHA, audience, thesis and case/evidence-bound decisions for a validated editorial plan. A selected section still requires the complete article input.
4. Transfer meaningful information organization: sequence versus alternatives versus concurrency; controls and denominators in measured results; hierarchy, uncertainty, feedback and stopping conditions. Do not infer causality, ranking, performance or numeric values from layout alone.

## Produce original, editable artwork

1. Write a JSON figure spec whose labels, claims, nodes, edges and chart values bind to **exact quotes** from the user's article; link the cases consulted. Use `examples/figure.json` as an editable illustration of the contract.
2. Run `render` for original SVG, PNG, standalone HTML, JSON spec and machine QA. Six deterministic families are supported: flow, comparison, matrix, bar, line and conceptual cover. Other analyzed families (e.g. network, small multiples, waterfall or error bars) are reference *ideas*, not automatic renderer promises; use suitable graphics tools for a specialized original figure.
3. Use `illustrate` only as a conservative starting draft: it creates equal-weight excerpt cards without inventing metrics or causal edges.
4. Run `qa`; inspect the **actual PNG**, source meaning, line routes, cropping, contrast, labels, axes/units/baselines, mobile legibility and figure placement. Revise until the graphic really answers the intended reader question. Machine QA does not certify semantic entailment, aesthetic quality, or independent visual approval.

## Practical CLI

Run from this Skill directory with its pinned PyMuPDF dependency:

```sh
.venv/bin/python -B scripts/illustrate.py verify-index
.venv/bin/python -B scripts/illustrate.py catalog "technical evaluation"
.venv/bin/python -B scripts/illustrate.py search "feedback and isolated evaluator"
.venv/bin/python -B scripts/illustrate.py show X2
.venv/bin/python -B scripts/illustrate.py plan examples/article.md --out runs/article-plan.json
.venv/bin/python -B scripts/illustrate.py render examples/article.md examples/figure.json --out runs/example
.venv/bin/python -B scripts/illustrate.py qa examples/article.md examples/figure.json --out runs/example
```

See [usage and reviewed-plan schema](references/usage.md). Generated outputs, caches and source media never belong in the indexed knowledge files. Keep output under this Skill's ignored `runs/` directory. No command here fetches external pages automatically; online lookup and pixel inspection are explicitly performed by the host at use time.

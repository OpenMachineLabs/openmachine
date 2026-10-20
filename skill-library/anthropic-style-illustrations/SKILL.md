---
name: anthropic-style-illustrations
description: Create original, editable article illustrations using a curated library of design cases, exact article evidence, and local rendering.
---

# Article-bound illustrations

Read the **whole article** before choosing whether and where an image helps.
Use `references/source-index.json` as a searchable library of 23 curated cases
from 14 sources. Its observations are attributed prior reviews, not fresh
visual inspection. E8 is a negative contrast case, excluded from normal search.

1. Search for the relationship the reader needs to understand, not a decorative
   style. Read the case's rule, inference, and misuse boundary. Consult
   [design patterns](references/design-patterns.md) for grounded adaptations.
2. Visually inspect linked source figures only when the decision needs their
   actual geometry or details. Never download or copy original images into the
   output. Reuse meaningful structure; do not clone branding or surface style.
3. Author an editable, article-bound spec. Anchor claims and relationships with
   exact quotes; keep the complete article SHA. Preserve citations as links.
   Do not invent metrics, causal edges, or missing decisions.
4. Run the renderer and QA using [usage](references/usage.md). Inspect the PNG
   for legibility, wrapping, clipping, routing, and faithful emphasis. Revise the
   spec when needed. Keep SVG, JSON, and linked evidence with the result.
5. Review meaning separately: an exact quote does not prove a paraphrase or an
   edge is justified. Only the host's article reading and visual inspection can
   approve an illustration for publication.

`plan` labels every section heuristically. `illustrate` produces conservative
original excerpt cards for editing. Neither command supplies semantic approval;
QA supplies structural/evidence checks, **not semantic or visual approval**.
Use a human-authored flow when the article actually establishes dependencies.

All output paths must stay within this Skill, normally under ignored `runs/`.
The renderer is offline; Python's standard library handles everything except
PNG export, which requires the pinned PyMuPDF in `requirements.txt`.

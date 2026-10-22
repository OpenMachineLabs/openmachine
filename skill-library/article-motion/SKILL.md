---
name: article-motion
description: Turn a complete technical article into an original, editable, silent editorial motion film. Use anthropic-style-illustrations for evidence and visual relationships, then design continuous objects, camera movement, readable timing, and local video output. Use for article trailers and animated technical workflows, not general footage editing or character animation.
---

# Article Motion

Build a short visual argument that invites the viewer into the article. The host
model reads, directs, authors and visually reviews; the local renderer draws
frames and encodes them. A successful encode is not semantic or aesthetic approval.

## Compose with the illustration Skill

Before planning a film, read the existing
[anthropic-style-illustrations Skill](../anthropic-style-illustrations/SKILL.md)
and its [design patterns](../anthropic-style-illustrations/references/design-patterns.md).
Keep both Skill folders together when installing this package. If they were
installed separately, resolve the named Skill in the host's available skills;
if it is missing, report the missing prerequisite before authoring the film.
This is an authoring dependency, not an automatic call from the renderer.

Reuse an existing complete-article review, figure specification and editable
SVG when available. Otherwise apply that Skill's article/evidence analysis and
read only the relevant indexed cases. Do not duplicate its research index or
force a new set of static PNGs before making a film. Image layouts are inputs
to reconsider for time-based viewing, not pages to crossfade in sequence.

## Establish the film brief

Honor the user's language, duration, destination and visual direction. For a
silent social article trailer without other constraints, start with 30–39 seconds,
16:9, 1080p and 60fps; these are defaults, not requirements for every task. This
helper exports silent video; narration and soundtrack production are outside its
current scope. Keep existing source assets and previous accepted versions.

Read the whole article, including limitations and future work. Retain a short
project brief with:

- the source locator or local article revision, audience, one main idea and CTA;
- claims and short supporting excerpts/section locators, with reported results,
  conceptual explanations and future work kept distinct;
- the selected visual relationships and relevant illustration case IDs;
- a time budget, copy, recurring objects and their meanings; and
- for each transition: what persists, what changes, what the change explains,
  and which text must finish before the next text arrives.

A brief documents the decisions. The editable film module is the executable
source of timing and geometry; do not claim that a prose/JSON storyboard drives
rendering unless the authored module actually reads it.

## Direct continuous motion

Use [motion patterns and failure lessons](references/motion-patterns.md) when
choosing the visual mechanism. Start with the article's strongest supported
result or question. Give the viewer an understandable hook in the opening seconds.

Prefer persistent object identity and meaningful transformations: records gather,
candidates branch from the same input, selected objects carry into the next step,
a stable layer supports a changing agent, or results return to the owning stage.
Move the camera to reveal the system instead of replacing every scene. A restrained
cut or fade is useful when meaning changes; adding a moving dot to unrelated slides
does not establish continuity. Never force causality merely to connect scenes.

Keep a consistent visual hierarchy, directional logic and color meaning. Separate
screen-space captions from moving world objects. Write short copy, budget actual
reading time, and avoid overlapping outgoing/incoming labels. Do not equate more
particles, 60fps or a warm background with better direction.

## Author and preview locally

Use the [film contract and commands](references/rendering.md). Copy the
[editable starter](assets/starter.cjs) into a task-owned project and adapt its
objects, copy and timing to the real article. Its
[example brief](assets/example-brief.md) reuses the illustration Skill's existing
archive-review example; it is not a universal film template or a benchmark.

Keep assets relative to that film, load them once in `setup`, and derive each
frame from absolute film time. Use available fonts for drafts; for consistent
final output, register available/licensed font files explicitly and inspect the
actual glyphs. Do not use another task's executable path, credentials, private
infrastructure, or unpublished case-study material in shared examples.

Preview in this order, continuing autonomously within the user's request:

1. Render and inspect the main compositions at the intended aspect ratio.
2. Render the opening and the hardest transition as short clips, with enough
   time on both sides. Inspect the transition midpoint at readable resolution.
3. View one low-resolution full draft for pacing and continuity. Resolve known
   copy, font, overlap and semantic issues before the full-resolution export.
4. Export the final film and inspect its metadata and complete decode. Recheck
   changed intervals; reuse unchanged review evidence rather than repeatedly
   encoding the entire film to discover the same problem.

The CLI's `inspect` result covers only technical properties. Separately check
source fidelity, missing versus rejected branches, frozen versus updated model
state, actual units/denominators, text legibility, unintentional clipping and
object/label collisions. Abstract waves and particles are not recorded audio,
measured logits, benchmark traces, or evidence of improvement.

## Deliver and stop

Deliver the verified MP4 and editable project, with a poster when useful. State
actual duration, dimensions, frame rate, audio presence and any material unverified
boundary. Keep technical, semantic and visual review outcomes separate; do not
invent audience approval or an aesthetic score. Do not publish, deploy or upload
without the user's authorization. Stop when the requested artifact and scoped
checks are complete.

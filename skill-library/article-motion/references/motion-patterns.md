# Motion decisions from article meaning

Keep the illustration Skill's [semantic design rules](../../anthropic-style-illustrations/references/design-patterns.md)
as the authority for what a relation means. These patterns add time and attention;
they do not establish new claims.

| Article relation | Motion | Boundary |
| --- | --- | --- |
| Scattered evidence becomes reusable knowledge | Preserve record identity while gathering, sorting and compressing | Do not imply that compression alone improves correctness |
| One input yields multiple candidates | Fan objects out from one visible origin | Label whether branches are candidates, alternatives or parallel work |
| An interface is stable while a component changes | Keep the environment still and replace the component | Do not imply unsupported interoperability |
| A selection becomes the next stage's input | Carry the selected objects forward, preserving their labels | Ranking is not proof that the best candidate is good |
| Results inform another iteration | Return the result to its actual owner | A loop does not prove learning or monotonic improvement |
| Detail belongs to a larger workflow | Follow the action, then pull back | Spatial distance does not measure elapsed time |

## Give objects a history

For a recurring object, define its starting, intermediate and ending states.
Interpolate position, scale, orientation and appearance while keeping its meaning
traceable. The object's identity can persist even when its representation changes.
Use deterministic easing to enter and leave a state without abrupt speed changes.

A conceptual motion sketch, not a required implementation:

```text
progress = ease(clamp((time - transition_start) / transition_duration))
position = start_position + progress * (end_position - start_position)
```

Keep world coordinates separate from the camera transform. Camera tracking can
bring a device or operation into focus while neighbors remain part of the same
system. Layered 2D shapes and a small projection are often enough; use full 3D only
when the content requires it. Continuous motion does not require every transition
to be a literal morph. A cut may be clearer when the meaning changes.

## Make the opening earn its time

Choose one supported result, tension or question that can be understood quickly.
Reveal the important relationship in the opening seconds. Preserve a visual anchor
while the changing quantity or concept is emphasized. Retain metric names, units,
comparison conditions and the distinction between reported and verified outcomes.
Never manufacture a stronger claim to obtain a more dramatic opening.

## Avoid the slide-deck failure mode

A real article trial exposed a failure that the original starter encouraged:
a small card travelled between static labelled nodes, while captions changed in
a fixed band. It preserved factual relationships and encoded correctly, but the
user rejected it as a slide deck. A camera pan over a diagram is still largely
a diagram. The repaired starter demonstrates object expansion and recombination;
it is not a universal film design.

Before copying a composition, distinguish these mechanisms:

| Weak substitute | More expressive choice when the article supports it |
| --- | --- |
| Pan from one static node to another | Follow an object as it opens, separates or changes form |
| Crossfade two versions of the same record | Give the persistent record one owner and interpolate its state |
| Keep every object equally small | Make the current operation a close-up; reveal the wider system later |
| Repeat a headline above/below the same diagram | Place type around the action; vary the focal layout deliberately |
| Show every exception simultaneously | Select a readable action and its necessary limits; leave secondary detail in the article |
| Move particles to create activity | Make the gathering or separation express a supported relationship |

For a supplied reference, compare its opening, a transformation and a handoff
with the candidate at normal speed. Note the primary object, its actual change,
framing, depth cues and settle time. Do not prescribe a fixed zoom ratio, number
of particles, dark opening, specific font or mandatory 3D from one successful film.
A restrained film can work; the important distinction is intentional motion that
carries meaning rather than animation applied to a page.

Color, large type and high fps support direction; they do not substitute for it.
Keep captions brief and stable while complicated motion happens. Allow the actual
words to be read; do not impose a universal words-per-second rule across audiences.

## Review the motion, not just its endpoints

- Write a compact motion beat as **starting state -> action -> resulting state**.
  If the resulting state is only a new caption, reconsider the action.
- Two excellent keyframes can have a poor transition. Inspect the middle, where
  incoming and outgoing labels may share the same pixels.
- Avoid independently fading two dense diagrams into the same space. Carry a
  shared object, separate their motion paths, or move the camera instead.
- A small contact sheet can hide glyph failures and label collisions. Revisit
  the affected interval at readable size and in actual playback.
- Move recurring assets through a handoff once. Drawing an outgoing and incoming
  copy with different timing can create a visible double image.
- Inspect several intermediate frames across the difficult interval, alongside
  normal-speed playback. Sparse endpoints cannot reveal stalling, teleporting,
  duplicate ownership or an object moving without changing. A playback endpoint
  is evidence that the player advanced, not that every transition was watched.
- Use a short clip before a full encode. Repair the failed interval and its
  dependents; preserve valid article analysis and unchanged visual decisions.

## Reference use

The prior workflow sampled portions of these official videos; these are leads,
not a complete shot-by-shot study or a reusable footage library:

- [Keep thinking with Claude](https://www.youtube.com/watch?v=FDNkDBNR7AM): sampled
  around 18, 45 and 81 seconds. Contrast between conceptual imagery and human work
  environments suggested distinct visual acts and a focused closing invitation.
- [Introducing the Codex app](https://www.youtube.com/watch?v=HFM3se4lNiw): opening
  and workflow view around 90 seconds. A task is followed by visible operations.
- [Claude Opus 4.5 solves a puzzle game](https://www.youtube.com/watch?v=2MJDdzSXL74):
  opening and comparison around 28 seconds. Concrete task evidence occupies the frame.

For a new style request, inspect only the relevant current passages. Record the
observed timestamp and composition separately from your interpretation. Reuse a
verified relevant note instead of researching a whole campaign for every film.
Transfer narrative organization, not source footage, logos, music or claims.

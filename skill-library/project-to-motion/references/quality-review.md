# Review the claim, then the motion

Validation checks hashes, quotes, IDs, bounds and supported scene topology.
It cannot prove entailment, implementation fidelity or comprehension.

## Semantic review

Trace every scene to its source. For repositories, inspect the selected revision,
relevant call sites and failure conditions. Distinguish documentation promises
from implementation, and tests from proof of every production behavior.

Ask what the viewer infers with sound off. Check exclusivity, order, dependencies,
payload identity, copied state, conditions and completion. Correct captions do
not repair contradictory motion.

Useful counterexamples from the case library:

- Database branching copies structure but no data: do not duplicate visible records.
- Payment states are conditional: do not play every state as one mandatory path.
- Workers feed a join: the result must wait for every required input.
- Failed network events can update local state: no remote result is not no change.
- Feedback can fail or stop: never show guaranteed endless improvement.

## Visual and temporal review

Inspect actual early, middle and late frames at output dimensions, including
transition frames and final holds. Play the full video if available; record
"frames inspected" when that is the actual review scope.

Check clipping, glyph coverage, labels, connector overlap/direction, contrast,
hierarchy and safe margins. Review portrait independently. Check both themes only
when both are delivered. Read captions at the intended playback size.

Check event order: trigger before consequence, objects reaching destinations,
join waiting, selected route staying selected, readable holds, and no decorative
motion competing with the causal event. The story must work without narration.

## Delivery evidence

| Check | Record |
|---|---|
| Source binding | Hashes and pinned repository commit |
| Validation | Actual command and pass/fail |
| Rendering | Actual codec, dimensions and duration |
| Semantics | Claims/relationships checked and remaining simplifications |
| Frames | Actual scene/frame samples inspected |
| Playback | Watched scope, or explicitly not performed |

A render manifest records encoding, not editorial approval. Never mark an
unperformed check passed. Correct material problems and keep editable sources.

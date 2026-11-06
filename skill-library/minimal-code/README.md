# minimal-code

An [Agent Skill](https://agentskills.io) that makes coding agents deliver the
smallest correct change: start from the observable outcome and its real caller,
reuse existing owners, avoid speculative abstractions, verify proportionately,
and remove excess before reporting.

## Layout

| Path | Purpose |
| --- | --- |
| `SKILL.md` | The skill, loaded when it triggers |
| `references/ml.md` | Extra checks, read only for ML, numerical and data-pipeline code |
| `evals/` | Maintenance only: scenarios, their small projects and trigger queries |

The projects under `evals/files/` are fixtures that agents edit during evals,
not tests of this repository.

## Install

Install only `SKILL.md` and `references/`, for example into
`~/.claude/skills/minimal-code/` for Claude Code, or into a directory listed in
Open Machine's `[skills] roots`. Repository rules such as `AGENTS.md` always
take precedence.

## Change the skill

Every rule must change agent behavior; delete text that does not. Keep
`SKILL.md` under 1,000 words and move detail into `references/`.

1. Add or update a scenario that fails without the change. Keep
   `preserve-needed-validation` even though it rarely differs: it guards
   against the skill making agents over-minimize.
2. For each scenario, run the agent on a fresh copy of its `evals/files/`
   project twice: with the skill installed and `Use the minimal-code skill.`
   appended to the prompt, and without the skill. Run each trigger query as
   written with the skill installed. Use directories outside any repository
   whose own rules could affect the baseline.
3. Grade each diff and report against the expectations. Keep the change only if
   the skill's pass rate improves without regressions.

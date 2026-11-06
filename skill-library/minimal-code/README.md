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

Only `SKILL.md` and `references/` need to be installed.

## Install

Copy or link this directory into your agent's skills folder, for example
`~/.claude/skills/minimal-code/` for Claude Code. For Open Machine, add the
directory to `[skills] roots`. Repository rules such as `AGENTS.md` always take
precedence.

## Change the skill

Every rule must change agent behavior; delete text that does not. Keep
`SKILL.md` under 1,000 words and move detail into `references/`.

1. Add or update a scenario that fails without the change.
2. For each scenario, run the agent on a fresh copy of its `evals/files/`
   project twice: with only `SKILL.md` and `references/` installed, and without
   the skill. Run each trigger query with the skill installed. Use a directory
   outside any repository whose own rules could affect the baseline.
3. Grade each diff and report against the expectations. Keep the change only if
   the skill's pass rate improves without regressions.

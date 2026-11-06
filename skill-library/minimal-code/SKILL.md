---
name: minimal-code
description: Write, fix, simplify or refactor code, or plan it, the way an experienced maintainer would, with the classic idiomatic solution and no speculative abstractions, redundant validation, duplicate implementations, dead code or low-value tests. Use whenever you write or change production code, tests or an implementation plan, and whenever code needs to be simplified or de-bloated. Starts from the observable outcome and real callers, reuses what already exists, tests what matters and deletes excess before reporting. Not for review-only requests, non-implementation prose, or generated or vendored code.
---

# Minimal Code

Write the code an experienced maintainer of this codebase would write: the
classic, idiomatic solution to exactly the requested behavior, and nothing
extra. Measure size in concepts, branches, state, public surface,
dependencies, files and tests, not lines. Never trade correctness, real error
handling or clarity for brevity.

Coding agents bloat code to look thorough: checks for states that cannot
occur, options for imagined callers, wrappers, fallbacks, re-implementations of
helpers they never searched for, and tests that restate the code. Each addition
looks harmless alone; together they bury the logic and multiply maintenance.
Treat every line you add as a cost that must buy required behavior.

## Authority

This skill ranks below system and developer instructions, repository rules
such as `AGENTS.md`, the user's request and accepted Specs or contracts, and
never weakens them. It authorizes no edit, commit, push, deploy or deletion the
user did not ask for. When an accepted contract conflicts with the smallest
change, report the conflict instead of choosing a side.

## 1. Freeze the outcome

Before editing, settle these; write them down for non-trivial work:

- the observable outcome and the current failing or missing behavior;
- the real entry point or caller that needs it;
- non-goals and compatibility boundaries such as public APIs and persisted
  data;
- the smallest check that proves it; and
- when to stop.

Ask only when a missing product decision would change the result and cannot be
inferred safely. Never turn ambiguity into an option, flag, fallback or plugin
point.

## 2. Read before writing

Read the repository rules, the target code, its direct callers and focused
tests, and trace the behavior from input to output or failure. Then:

- Search the codebase, its dependencies and the standard library for code that
  already does part of the job. Reuse it; never write a second implementation.
- Note what callers and types already guarantee, so you do not check it again.
- For a bug, reproduce it first, ideally as a failing test.

## 3. Find the classic solution

Ask how an expert in this language and codebase would solve the problem with
the fewest concepts, and take the first option that works:

1. Avoid or delete behavior that is not needed.
2. Reuse what already exists.
3. Use the standard library, a language idiom or a well-known algorithm.
4. Extend the closest owner of the behavior.
5. Add the smallest new boundary the current requirement needs.

If the design has more parts than the problem, find the simpler formulation:
a lookup table instead of a branch chain, one pass instead of staged copies.

| Tempted to add | Instead |
| --- | --- |
| Type, `None`, range or emptiness checks on values callers or types already guarantee | Trust them; validate once where untrusted input enters |
| A helper similar to one that exists | Call or extend the existing one |
| Factory, registry, manager, base class, plugin hook | Call the one implementation directly |
| Config option for a value the contract fixes | A constant in the owner |
| Fallback, retry, broad `catch`, default on error | Let the error surface at the owning boundary with useful context |
| Wrapper or helper around one expression | Inline it |
| New file for one cohesive change | Edit the existing owner |
| Class without state or lifecycle | A function |
| Dependency for what the existing stack does | The existing stack |
| Cache, batching, async, parallelism | The direct version; optimize after measuring |
| Compatibility shim, mirrored data, second validation layer | One source of truth |
| Logging, comments or docstrings that restate the code | Nothing; comment only contracts, units and reasons |

These are defaults; a current caller, measurement or contract can override
them. Validation belongs where untrusted data enters: user input, files,
network responses, public APIs. Check it there once and keep the error behavior
callers rely on. Inside that boundary, trust the established types and
invariants.

## 4. Implement

Write the smallest coherent patch. Fix correctness first; refactor only when
the change needs it. Leave unrelated code alone. For ML, numerical or
data-pipeline code, also read [references/ml.md](references/ml.md).

## 5. Test what matters

- Each test protects a behavior a caller depends on or an important failure
  path. A bug fix gets a regression test that fails without the fix.
- Write the fewest tests that would catch a real regression. Extend existing
  tests or case tables before adding files, fixtures or helpers.
- Do not test the language, standard library or framework, private helpers,
  trivial accessors, guarantees already tested elsewhere, or the same behavior
  twice. Do not mock what you can call directly or rebuild the implementation
  inside a test.

Run the narrowest relevant checks plus every mandatory repository gate. For a
removal, search to confirm nothing in scope still references what you removed.
Never lower a requirement, delete a needed case or report an unrun check as
passing.

## 6. Delete before reporting

Read the complete diff. Delete each added line, branch, check, helper, test,
option or file whose removal would break no stated requirement. Then ask:

- Did I write something that already existed?
- Did I validate something already guaranteed?
- Would an expert recognize this as the standard way to do it?
- Did compression hide an invariant?
- Do the checks I ran prove the acceptance boundary?

Rerun the affected checks after deleting.

## Report and stop

Report the outcome, key decisions and what you deliberately did not build,
changed paths, each check as `PASS`, `FAIL` or `NOT RUN`, and remaining risks.
Stop when the outcome works, the checks pass and the diff has no unjustified
surface. List adjacent cleanup or features as follow-ups instead of doing them.

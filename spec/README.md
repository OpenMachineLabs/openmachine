# Spec and Survey Index

This is an index, not a second copy of Issue bodies or a product architecture.
No product Spec is approved by the repository scaffold.

## Spec Issues

| Spec Issue | Binding | Current scope |
| --- | --- | --- |
| [#2](https://github.com/OpenMachineLabs/openmachine/issues/2) | `direct_issue_contract`, [current increment authorization](https://github.com/OpenMachineLabs/openmachine/issues/2#current-increment-authorization) | Accepted M1-M6 plus the shared state/file primitive refactor form the `v0.1.0` release baseline. The current maintainer request authorizes release preparation, publication and subsequent closure; the exact release and closure receipts remain in Issue #2. Specialized/browser workflows, broader architecture, native GitHub onboarding and production migration remain separate. |
| [#6](https://github.com/OpenMachineLabs/openmachine/issues/6) | `direct_issue_contract`, [implementation authorization and acceptance](https://github.com/OpenMachineLabs/openmachine/issues/6) | Minimal GitHub provenance footers for configured Issue/PR/comment/review body writes; broader GitHub collaboration remains in #2 M4. |
| [#18](https://github.com/OpenMachineLabs/openmachine/issues/18) | `direct_issue_contract`, [approved architecture and increments](https://github.com/OpenMachineLabs/openmachine/issues/18#classification) | S0-S5 are accepted as of 2026-09-30 and form the `v0.2.0` modular-runtime release baseline. PRs #21-#27 own the architecture, final acceptance, and repository index; the Issue owns the exact A1-A10 evidence and closure receipt. The current maintainer request authorizes `v0.2.0` release preparation/publication and matching SSH-worker refresh. Production deployment and later capabilities remain separately scoped. |
| [#28](https://github.com/OpenMachineLabs/openmachine/issues/28) | `direct_issue_contract`, [E4 authorization](https://github.com/OpenMachineLabs/openmachine/issues/28#issuecomment-5911047039) | E1/PR #31, E2/PR #33 and E3/PR #34 are accepted. E4 verifies installed interactive continuity, actual managed updates/compatible rollback and real-client recovery; repair only demonstrated acceptance defects. Preserve format 5/state 3/SSH 3, original workers/receipts, leases and cursors. Self-review, merge and the Issue body update are authorized; SDK migration, conditional task integration, production deployment, release and closure remain separate. The Issue owns exact PASS/FAIL/NOT RUN evidence. |
| [#13](https://github.com/OpenMachineLabs/openmachine/issues/13) | `direct_issue_contract`, [maintainer-approved W4 scope](https://github.com/OpenMachineLabs/openmachine/issues/13) | W1-W3 are accepted through PRs #37-#39. W4 hardens receipt admission and verifies installed workflow/recovery with the existing owners. The current maintainer authorizes self-review/merge, Issue updates, local runtime deployment, v0.2.2 preparation/publication and closure after applicable evidence. Live SSH is explicitly waived for W4; offline tests and target compatibility gates remain. Retain formats 1/2, state 5, SSH 6 and original workers. Actual refreshed-client/fresh-conversation evidence is distinct from SDK/native checks; the Issue owns remaining gates and final closure. |
| [#32](https://github.com/OpenMachineLabs/openmachine/issues/32) | `direct_issue_contract`, [implementation authorization](https://github.com/OpenMachineLabs/openmachine/issues/32#issuecomment-5918946285) | A1-A7 are accepted as of 2026-10-01 through PR #43 and form the `v0.2.3` MCP migration release baseline: exact MCP SDK 2.2.0, protocol 2026-07-28 with retained 2025-11-25 compatibility, state 5, SSH 6 and preserved history. [Amended A5](https://github.com/OpenMachineLabs/openmachine/issues/32#issuecomment-5926469587) requires safe stop -> upgrade -> resume, not live-worker continuity. The current maintainer request authorizes `v0.2.3` publication; Tasks/Events, tunnel changes, live SSH migration and Connection Layer work remain separate. |
| [#29](https://github.com/OpenMachineLabs/openmachine/issues/29) | `direct_issue_contract`, [maintainer-approved S1.1](https://github.com/OpenMachineLabs/openmachine/issues/29) | S0 is delivered through #48. The current maintainer approves S1.1 canonical create/read/find, bounded derived-index recovery, legacy preservation, non-runnable structured associations, actual-reader compatibility/state 6, one PR, self-review/merge and Issue-body update. Full S1 transitions/admission, S2/S3, production activation, Events, tunnel changes, new inference, release and closure remain separate. The Issue owns exact evidence and remaining gates. |

## Survey Issues

| Survey Issue | Evidence or question | Current use |
| --- | --- | --- |

No Survey is registered. Research and experiments use the same `[SURVEY]`
workflow. Record the real question while work is open; record supported findings
and limitations when it concludes. A Survey is evidence, never design approval.

## Maintenance

Keep full bodies and results in their Issues. Do not create empty numbered
folders, duplicate design documents, or prewritten acceptance receipts.
Necessary machine-readable attachments may live in `spec/<issue-number>/`;
product code must not load them. Completed Specs remain design history, while
current code and tests describe accepted implementation.

Programs are linked from the root README when one exists. Formal Benchmarks
are found through their prefix and `benchmark` label rather than a second index.
See [Contributing](../CONTRIBUTING.md) for the workflow and closure rules.

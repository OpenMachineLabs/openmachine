# Spec and Survey Index

This is an index, not a second copy of Issue bodies or a product architecture.
No product Spec is approved by the repository scaffold.

## Spec Issues

| Spec Issue | Binding | Current scope |
| --- | --- | --- |
| [#2](https://github.com/OpenMachineLabs/openmachine/issues/2) | `direct_issue_contract`, [current increment authorization](https://github.com/OpenMachineLabs/openmachine/issues/2#current-increment-authorization) | Accepted M1-M6 plus the shared state/file primitive refactor form the `v0.1.0` release baseline. The current maintainer request authorizes release preparation, publication and subsequent closure; the exact release and closure receipts remain in Issue #2. Specialized/browser workflows, broader architecture, native GitHub onboarding and production migration remain separate. |
| [#6](https://github.com/OpenMachineLabs/openmachine/issues/6) | `direct_issue_contract`, [implementation authorization and acceptance](https://github.com/OpenMachineLabs/openmachine/issues/6) | Minimal GitHub provenance footers for configured Issue/PR/comment/review body writes; broader GitHub collaboration remains in #2 M4. |
| [#18](https://github.com/OpenMachineLabs/openmachine/issues/18) | `direct_issue_contract`, [approved architecture and increments](https://github.com/OpenMachineLabs/openmachine/issues/18#classification) | S0-S5 are accepted as of 2026-09-30 and form the `v0.2.0` modular-runtime release baseline. PRs #21-#27 own the architecture, final acceptance, and repository index; the Issue owns the exact A1-A10 evidence and closure receipt. The current maintainer request authorizes `v0.2.0` release preparation/publication and matching SSH-worker refresh. Production deployment and later capabilities remain separately scoped. |
| [#28](https://github.com/OpenMachineLabs/openmachine/issues/28) | `direct_issue_contract`, [E3 authorization](https://github.com/OpenMachineLabs/openmachine/issues/28#issuecomment-5909307488) | E1/PR #31 and E2/PR #33 are accepted. E3 adds worker-owned PTY and sequenced stdin/interrupt/resize, with execution format 5, state contract 3 and SSH protocol 3. Preserve original workers, older receipt facts, pipe defaults, leases and raw-byte cursors; no automatic replay or state migration. E4/actual Chat acceptance, deployment and release remain separate. The Issue owns exact evidence. |

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

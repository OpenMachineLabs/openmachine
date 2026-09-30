# Spec and Survey Index

This is an index, not a second copy of Issue bodies or a product architecture.
No product Spec is approved by the repository scaffold.

## Spec Issues

| Spec Issue | Binding | Current scope |
| --- | --- | --- |
| [#2](https://github.com/OpenMachineLabs/openmachine/issues/2) | `direct_issue_contract`, [current increment authorization](https://github.com/OpenMachineLabs/openmachine/issues/2#current-increment-authorization) | Accepted M1-M6 plus the shared state/file primitive refactor form the `v0.1.0` release baseline. The current maintainer request authorizes release preparation, publication and subsequent closure; the exact release and closure receipts remain in Issue #2. Specialized/browser workflows, broader architecture, native GitHub onboarding and production migration remain separate. |
| [#6](https://github.com/OpenMachineLabs/openmachine/issues/6) | `direct_issue_contract`, [implementation authorization and acceptance](https://github.com/OpenMachineLabs/openmachine/issues/6) | Minimal GitHub provenance footers for configured Issue/PR/comment/review body writes; broader GitHub collaboration remains in #2 M4. |
| [#18](https://github.com/OpenMachineLabs/openmachine/issues/18) | `direct_issue_contract`, [approved architecture and increment](https://github.com/OpenMachineLabs/openmachine/issues/18#classification) | S0-S4 are accepted. The maintainer authorized S5: complete the installed runtime dependency checks, final artifact/contract review, scoped real-workflow and old-worker activation/rollback acceptance, current documentation, self-review, merge and Issue-body/status update. Reuse valid prior evidence; record each required PASS / FAIL / NOT RUN in the Issue. Closure requires complete acceptance; releases and production deployment remain separate. |

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

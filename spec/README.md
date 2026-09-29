# Spec and Survey Index

This is an index, not a second copy of Issue bodies or a product architecture.
No product Spec is approved by the repository scaffold.

## Spec Issues

| Spec Issue | Binding | Current scope |
| --- | --- | --- |
| [#2](https://github.com/OpenMachineLabs/openmachine/issues/2) | `direct_issue_contract`, [current increment authorization](https://github.com/OpenMachineLabs/openmachine/issues/2#current-increment-authorization) | Accepted M1-M5 plus the maintainer-approved shared state/file primitive refactor. The implementing PR records the current user approval of the bounded extraction, unchanged public/persisted contracts, focused acceptance, self-review, merge and post-merge Issue update. M6, broader module/routing changes, native GitHub onboarding, production migration, release and Spec closure remain separate. |
| [#6](https://github.com/OpenMachineLabs/openmachine/issues/6) | `direct_issue_contract`, [implementation authorization and acceptance](https://github.com/OpenMachineLabs/openmachine/issues/6) | Minimal GitHub provenance footers for configured Issue/PR/comment/review body writes; broader GitHub collaboration remains in #2 M4. |

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

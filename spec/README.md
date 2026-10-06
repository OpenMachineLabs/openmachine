# Spec and Survey Index

This is an index, not a second copy of Issue bodies or a product architecture.
Current code and tests describe implemented behavior. Historical approvals and
acceptance receipts remain in the linked Issues, not this index.

## Spec Issues

| Spec Issue | Binding | Current scope |
| --- | --- | --- |
| [#2](https://github.com/OpenMachineLabs/openmachine/issues/2) | `direct_issue_contract`, [current increment authorization](https://github.com/OpenMachineLabs/openmachine/issues/2#current-increment-authorization) | Closed: standalone runtime and managed lifecycle baseline. |
| [#6](https://github.com/OpenMachineLabs/openmachine/issues/6) | `direct_issue_contract`, [implementation authorization and acceptance](https://github.com/OpenMachineLabs/openmachine/issues/6) | Closed: configurable GitHub attribution. |
| [#18](https://github.com/OpenMachineLabs/openmachine/issues/18) | `direct_issue_contract`, [approved architecture and increments](https://github.com/OpenMachineLabs/openmachine/issues/18#classification) | Closed: module ownership and dependency direction; service extraction is in PR #60. |
| [#28](https://github.com/OpenMachineLabs/openmachine/issues/28) | `direct_issue_contract`, [E4 authorization](https://github.com/OpenMachineLabs/openmachine/issues/28#issuecomment-5911047039) | Closed: original execution, pipe/PTY control, lifecycle and receipt compatibility. |
| [#13](https://github.com/OpenMachineLabs/openmachine/issues/13) | `direct_issue_contract`, [maintainer-approved W4 scope](https://github.com/OpenMachineLabs/openmachine/issues/13) | Closed: guarded reads/edits/patches and durable changes; the recorded live-SSH waiver remains historical. |
| [#32](https://github.com/OpenMachineLabs/openmachine/issues/32) | `direct_issue_contract`, [implementation authorization](https://github.com/OpenMachineLabs/openmachine/issues/32#issuecomment-5918946285) | Closed: MCP 2026-07-28 / SDK 2.2.0 with retained 2025-11-25 compatibility. |
| [#29](https://github.com/OpenMachineLabs/openmachine/issues/29) | `direct_issue_contract`, [maintainer-approved S3 and final acceptance](https://github.com/OpenMachineLabs/openmachine/issues/29) | Closed: task admission and original-result observation/recovery retained; Work Events continuation retired by current maintenance. |
| [#62](https://github.com/OpenMachineLabs/openmachine/issues/62) | `direct_issue_contract`, [PR1 authorization](https://github.com/OpenMachineLabs/openmachine/issues/62#issuecomment-5953592298) | Closed: native GitHub account lifecycle, credential renewal, stable task bindings and historical gh compatibility; PRs #63 and #64 are complete. |
| [#68](https://github.com/OpenMachineLabs/openmachine/issues/68) | `direct_issue_contract`, [PR1 authorization and live-route evidence](https://github.com/OpenMachineLabs/openmachine/issues/68#issuecomment-5970576144); [PR2 authorization](https://github.com/OpenMachineLabs/openmachine/issues/68#issuecomment-5972982461) | Bound same-Chat recovery through the existing observer and Chrome adapter, with finite retry, original receipts and distinct continuation evidence. Current maintenance makes recovery opt-in for new tasks; the Issue retains the original default-on delivery history. |
| [#76](https://github.com/OpenMachineLabs/openmachine/issues/76) | `direct_issue_contract`, [implementation authorization](https://github.com/OpenMachineLabs/openmachine/issues/76#issuecomment-5983667384) | Task-owned Chat preparation and one guarded recovery route; live client acceptance and merge evidence remain in the Issue. |
| [PR #75](https://github.com/OpenMachineLabs/openmachine/pull/75), [PR #77](https://github.com/OpenMachineLabs/openmachine/pull/77) | Maintainer-authorized implementation requests | Packaged Python/Core/Git, verified offline preparation, stable management contract, persistent runtime policy, explicit first-run grants and independent bounded install/update operations using existing activation/restoration. |
| [Desktop #7](https://github.com/OpenMachineLabs/openmachine-desktop/issues/7) | `direct_issue_contract`, [approved Desktop scope](https://github.com/OpenMachineLabs/openmachine-desktop/issues/7#classification); explicit maintainer implementation/self-review/merge request on 2026-10-06 | Preserve M3 create-only recovery-profile setup. M5 adds narrowly scoped machine list/probe/test/add/remove management commands consumed by Desktop, with validated configuration changes through original maintenance, disabled new grants, pinned identity and retained-history checks. No generic configuration API, recovery redesign, worker provisioning, network changes, deployment, release or Spec closure. |

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

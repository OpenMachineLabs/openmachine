# Spec and Survey Index

This is an index, not a second copy of Issue bodies or a product architecture.
Current code and tests describe implemented behavior. Historical approvals and
acceptance receipts remain in the linked Issues, not this index.

## Spec Issues

| Spec Issue | Binding | Current scope |
| --- | --- | --- |
| [#138](https://github.com/OpenMachineLabs/openmachine/issues/138) | `direct_issue_contract`, [current increment authority](https://github.com/OpenMachineLabs/openmachine/issues/138#classification) | Context composition and opt-in target environment/resource observation. Both increments authorized; progress and acceptance stay in the Issue. No deployment. |
| [#2](https://github.com/OpenMachineLabs/openmachine/issues/2) | `direct_issue_contract`, [current increment authorization](https://github.com/OpenMachineLabs/openmachine/issues/2#current-increment-authorization) | Closed: standalone runtime and managed lifecycle baseline. |
| [#6](https://github.com/OpenMachineLabs/openmachine/issues/6) | `direct_issue_contract`, [implementation authorization and acceptance](https://github.com/OpenMachineLabs/openmachine/issues/6) | Closed: configurable GitHub attribution. |
| [#18](https://github.com/OpenMachineLabs/openmachine/issues/18) | `direct_issue_contract`, [approved architecture and increments](https://github.com/OpenMachineLabs/openmachine/issues/18#classification) | Closed: module ownership and dependency direction; service extraction is in PR #60. |
| [#28](https://github.com/OpenMachineLabs/openmachine/issues/28) | `direct_issue_contract`, [E4 authorization](https://github.com/OpenMachineLabs/openmachine/issues/28#issuecomment-5911047039) | Closed: original execution, pipe/PTY control, lifecycle and receipt compatibility. |
| [#13](https://github.com/OpenMachineLabs/openmachine/issues/13) | `direct_issue_contract`, [maintainer-approved W4 scope](https://github.com/OpenMachineLabs/openmachine/issues/13) | Closed: guarded reads/edits/patches and durable changes; the recorded live-SSH waiver remains historical. |
| [#32](https://github.com/OpenMachineLabs/openmachine/issues/32) | `direct_issue_contract`, [implementation authorization](https://github.com/OpenMachineLabs/openmachine/issues/32#issuecomment-5918946285) | Closed: MCP 2026-07-28 / SDK 2.2.0 with retained 2025-11-25 compatibility. |
| [#29](https://github.com/OpenMachineLabs/openmachine/issues/29) | `direct_issue_contract`, [maintainer-approved S3 and final acceptance](https://github.com/OpenMachineLabs/openmachine/issues/29) | Closed: task admission and original-result observation/recovery retained; Work Events continuation retired by current maintenance. |
| [#62](https://github.com/OpenMachineLabs/openmachine/issues/62) | `direct_issue_contract`, [PR1 authorization](https://github.com/OpenMachineLabs/openmachine/issues/62#issuecomment-5953592298) | Closed: native GitHub account lifecycle, credential renewal, stable task bindings and historical gh compatibility; PRs #63 and #64 are complete. Current maintenance retires gh execution while preserving history (see [account connections](../openmachine/skills/github/connections.md#account-connections)). |
| [#86](https://github.com/OpenMachineLabs/openmachine/issues/86) | `direct_issue_contract`, maintainer-authorized implementation and self-review on 2026-10-07 | Module-owned layered test gates and change-aware local/CI verification; one implementation PR, with no product behavior change. |
| [#105](https://github.com/OpenMachineLabs/openmachine/issues/105) | `direct_issue_contract`, [current-user publication and implementation authorization](https://github.com/OpenMachineLabs/openmachine/issues/105#classification) | Centralize built-in skills and operating documentation under `openmachine/skills/`, add direct tool-to-guide discovery, preserve existing reads, and retire scattered documentation in one implementation PR. Merge and live deployment remain separate. |
| [#108](https://github.com/OpenMachineLabs/openmachine/issues/108) | `direct_issue_contract`, [current-user publication, implementation and merge authorization](https://github.com/OpenMachineLabs/openmachine/issues/108#classification) | One implementation PR: transactional Task ledger, bounded current-state/evidence reads and verified legacy migration; original effect identities and safety gates remain authoritative. |
| [#114](https://github.com/OpenMachineLabs/openmachine/issues/114) | `direct_issue_contract`, [maintainer authorization](https://github.com/OpenMachineLabs/openmachine/issues/114#classification) | One Core PR permanently retires automatic Chat recovery without altering independent Task/SSH/service observation; install/data cutover is separate. |
| [#117](https://github.com/OpenMachineLabs/openmachine/issues/117) | `direct_issue_contract`, [separately authorized Directions A, D, B and C](https://github.com/OpenMachineLabs/openmachine/issues/117#classification) | Directions A ([#125](https://github.com/OpenMachineLabs/openmachine/pull/125)), D ([#126](https://github.com/OpenMachineLabs/openmachine/pull/126)) and B ([#127](https://github.com/OpenMachineLabs/openmachine/pull/127)) are merged. Direction C is independently authorized on 2026-10-10 and implemented in [PR #129](https://github.com/OpenMachineLabs/openmachine/pull/129), pending final review, merge and post-merge Issue acceptance/closure. Prior authorizations did not authorize C; retain native execution, Workspace, SSH, persisted contracts and historical unknowns. No version bump, release, deployment, production install or restart. |
| [PR #75](https://github.com/OpenMachineLabs/openmachine/pull/75), [PR #77](https://github.com/OpenMachineLabs/openmachine/pull/77) | Maintainer-authorized implementation requests | Packaged Python/Core/Git, verified offline preparation, stable management contract, persistent runtime policy, explicit first-run grants and independent bounded install/update operations using existing activation/restoration. |

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

Roadmaps are linked from the root README when one exists. Formal Benchmarks
are found through their prefix and `benchmark` label rather than a second index.
See [Contributing](../CONTRIBUTING.md) for the workflow and closure rules.

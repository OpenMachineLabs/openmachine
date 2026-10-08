# Open Machine skills

Built-in operating knowledge lives here. Start with [Overview](overview/SKILL.md),
then load the domain and direct topic needed for the next operation.

| Domain | Guide |
| --- | --- |
| Capability discovery, setup and repository navigation | [Overview](overview/SKILL.md) |
| Source, exact changes and worktrees | [Workspace](workspace/SKILL.md) |
| Original commands and terminal controls | [Execution](execution/SKILL.md) |
| Create, continue, control and verify tasks | [Tasks](tasks/SKILL.md) |
| Account connections and collaboration | [GitHub](github/SKILL.md) |
| Authorized browser subtasks | [Browser](browser/SKILL.md) |
| Runtime lifecycle, SSH and payload preparation | [Service](service/SKILL.md) |

Each `SKILL.md` owns its short metadata and entry instructions. Topic files beside
it own detailed procedures; do not duplicate their contents in README or tool
prompts. `bindings.yaml` is the single curated tool-to-guide mapping. The runtime
builds catalogue summaries and exact tool hints from these sources.

Use `inspect_skills(action="list")` for metadata, `action="read", tool="..."`
for an available tool's guide, or the returned exact Skill ID and relative topic
path for a bounded read. Use `action="files"` to discover resources. Follow snapshot,
hash and paging guards. A sibling topic keeps the current Skill ID. Cross-domain
Markdown links require the target domain's exact listed ID and local path, for
example `skill="om.overview", path="setup.md"`; `../` paths are not accepted by
`inspect_skills`. External Skills remain explicit configured live sources;
they cannot replace built-in tool bindings or confer permissions.

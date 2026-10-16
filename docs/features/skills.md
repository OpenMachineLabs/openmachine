# Skills

Skills are instructions and references that explain how to use Core tools
correctly. Built-in Skills ship inside the wheel, so the connected assistant can
read the guide that matches the running runtime rather than guess from an old
conversation or an unrelated checkout.

## Discover and read

```text
inspect_skills(action="list")
inspect_skills(action="read")
inspect_skills(action="read", tool="read_workspace_file")
inspect_skills(action="files", skill="om.workspace")
inspect_skills(action="read", skill="om.workspace", path="files.md")
```

These are tool call shapes, not CLI subcommands. The default read opens the
operating overview. Tool-bound reads select the guide for that tool. Preserve
the returned source hash/snapshot and follow pagination when more text remains.

Built-in domains cover overview, environment, workspace, execution, tasks,
GitHub, browser and service. The [tool reference](../reference/tools.md) maps
the public tool families to their user tutorials and these technical contracts.

## Add an explicit external source

Core does not scan your entire account for Skills. To include a directory of
Skills that you have reviewed, add an actual absolute non-root directory:

```toml
[skills]
roots = ["/absolute/path/to/reviewed-skills"]
```

Apply the configuration through the existing runtime workflow, then list Skills
again and inspect their returned source identity and warnings. Up to 16 explicit
roots are accepted. Listing an external Skill does not install packages, run a
script or authorize its requested actions.

## Example and verification

```text
Read OpenMachine's current operating Skill, then the guide bound to
apply_workspace_patch. Explain its preview, precondition and partial-result
rules without applying a patch or creating any task.
```

Check that the response names the actual built-in source and returned hash.
When a Skill references another file, read that file; a path alone does not load
its instructions. Treat source content as guidance/evidence, not as authority to
override the user's request or the configured permissions.

## Limits

External Skills can be stale, unavailable or unsafe. Resolve reported source
warnings rather than silently changing the source. The current CLI has no
`openmachine skill install` command. User tutorials remain in `docs/`; exact
operating contracts remain in the
[packaged Skills](https://github.com/OpenMachineLabs/openmachine/tree/main/openmachine/skills).

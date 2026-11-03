# GitHub attribution

[Domain guide](SKILL.md) | [Common operations](reference.md)

## Attribution

Read `get_capabilities.github.attribution`, the effective `[github].attribution`
boolean (default `true` for public/private repositories). No per-client override.
When enabled, new Issue/PR bodies use:

```markdown
---

Created via [Open Machine](https://github.com/OpenMachineLabs/openmachine).
```

New top-level comments and review summaries use:

```markdown
---

Posted via [Open Machine](https://github.com/OpenMachineLabs/openmachine).
```

Let typed writers append these exact footers once; do not add other signatures,
tool/model explanations, versions, badges or slogans. Any separately authorized
route uses the same setting and text, never an authentication/permission bypass.
Verify the destination body and footer before reporting publication. `false`
stops new automatic attribution; preserve existing footers and hidden task markers
on edits. Do not rebrand legacy content, alter unrelated historical publications
or decorate inline review comments. The typed writers share the exact constants in
`integrations/github/operations.py`. Apply a setting change through the authorized
[service configuration lifecycle](../service/management.md#exact-updates-and-compatible-rollback),
not hot reload or a per-client switch.

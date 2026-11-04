# GitHub attribution

[Domain guide](SKILL.md) | [Common operations](reference.md)

## Attribution

Attribution is permanently enabled for native Open Machine GitHub writes on both
public and private repositories. There is no configuration switch or per-client
override. `get_capabilities.github.attribution` is informational and always `true`
when GitHub integration is configured.

New Issue/PR bodies and Release notes use:

```markdown
---

Created via [Open Machine](https://github.com/OpenMachineLabs/openmachine).
```

New top-level comments and review summaries use:

```markdown
---

Posted via [Open Machine](https://github.com/OpenMachineLabs/openmachine).
```

Typed writers append these exact footers once; do not add other signatures,
tool/model explanations, versions, badges or slogans. Verify the destination
body and footer before reporting publication. Preserve existing footers and
hidden task markers on edits. Do not rebrand legacy content, alter unrelated
historical publications or decorate inline review comments. The typed writers
share the exact constants in `integrations/github/operations.py`.

Remove any legacy `[github] attribution` entry from local configurations:
both `true` and `false` values are rejected by the current schema.

# GitHub Configuration Instructions

Read the root [AGENTS.md](../AGENTS.md) first. Keep templates and automation in
English, and reuse the managed label list in `labels.json`.

- Workflows run on GitHub-hosted runners named literally (`ubuntu-24.04`,
  `macos-15`), never on a runner a variable selects.
- Checks stay read-only and bounded. Pin external actions to full commit SHAs
  and disable persisted checkout credentials.
- Read event JSON as data; never interpolate titles or bodies into commands.
- Never run candidate code with `pull_request_target` or a write token. Label
  writes belong only to the main-branch label workflow and keep unrelated
  labels.
- Required checks keep their names, and are never path-filtered. Edits to a
  PR's text use separate check names and never replace code results.

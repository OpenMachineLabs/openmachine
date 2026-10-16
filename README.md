<p align="center">
  <img src="assets/openmachine-banner.png" alt="Open Machine — Your AI. Your machines. Connect any AI to your context and get things done on your machines.">
</p>

<p align="center">
  <a href="https://github.com/OpenMachineLabs/openmachine/actions/workflows/quality.yml"><img src="https://img.shields.io/github/actions/workflow/status/OpenMachineLabs/openmachine/quality.yml?branch=main&amp;style=flat-square&amp;label=CI" alt="CI"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.12%2B-3776AB?style=flat-square&amp;logo=python&amp;logoColor=white" alt="Python 3.12+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-43A047?style=flat-square" alt="MIT License"></a>
  <a href="https://discord.gg/TAxEWV7fgZ"><img src="https://img.shields.io/badge/Discord-Join-5865F2?style=flat-square&amp;logo=discord&amp;logoColor=white" alt="Join Discord"></a>
  <a href="https://x.com/OpenMachineAI"><img src="https://img.shields.io/badge/X-Follow-181717?style=flat-square&amp;logo=x&amp;logoColor=white" alt="Follow on X"></a>
</p>

**Open Machine (OM Core)** connects your existing AI Chat to your machines through a local-first MCP runtime. Your AI plans the work; OM provides permission-controlled tools to carry it out and return results.

[Discord](https://discord.gg/TAxEWV7fgZ) · [X](https://x.com/OpenMachineAI) · [GitHub Issues](https://github.com/OpenMachineLabs/openmachine/issues) · [Contributing](CONTRIBUTING.md)

## Quick start

**Requirements:** Python 3.12+, Git, and a compatible SQLite version on macOS or Linux. See [setup](openmachine/skills/overview/setup.md) for the exact requirements and platform limitations.

Build from the pinned `v0.2.21` release tag. Replace the workspace path with an **existing disposable project**:

```bash
git clone --branch v0.2.21 --depth 1 https://github.com/OpenMachineLabs/openmachine.git
cd openmachine
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip wheel --no-deps --no-build-isolation . --wheel-dir dist
.venv/bin/python -m pip install --no-deps dist/openmachine-0.2.21-py3-none-any.whl
.venv/bin/openmachine init --workspace /absolute/path/to/disposable-project
.venv/bin/openmachine doctor
.venv/bin/openmachine serve
```

OM serves a **private Unix socket**. Running `serve` does not connect your AI Chat by itself. Follow [Connect the official tunnel](openmachine/skills/overview/setup.md#connect-the-official-tunnel) to connect a supported AI client.

Command execution and workspace writes are **disabled by default** and require separate explicit grants. This is an initial-development release, not a stable 1.x release. Linux supports manual serving; Windows is not currently supported.

## How it works

<p align="center">
  <img src="assets/openmachine-workflow.png" alt="You request a task in AI Chat, which plans and uses OM Core to perform authorized actions on your machines. Results return to AI Chat. Gmail, Slack, GitHub, and Personal Context are illustrated above OM as context sources, with future context updates flowing back.">
</p>

Your request flows from **AI Chat → OM Core → your machines**, and execution results return to your AI. OM exposes the approved tools; it does not replace your AI assistant.

The **Personal Context layer and automatic context updates** in the diagram illustrate planned functionality, not capabilities promised in `v0.2.21`. Available integrations depend on your installed runtime and configured permissions.

## Documentation

- **[Setup and connection](openmachine/skills/overview/setup.md)** — installation, grants, platform requirements, and MCP connection.
- **[Operating Skill](openmachine/skills/overview/SKILL.md)** — the short starting point for AI agents.
- **[Skills directory](openmachine/skills/README.md)** — tool-specific instructions for workspace, commands, tasks, GitHub, browser, and services.

## Security

Open Machine is a trusted, single-operator native runtime, **not an OS sandbox** or multi-tenant executor. Native commands can access resources beyond read-only tool scopes. Keep the listener private, grant access deliberately, and remember that files and logs returned to an AI client may leave your machine. Read [setup and trust boundaries](openmachine/skills/overview/setup.md) before enabling execution or remote access.

## Contributors

Thanks to everyone who contributes to Open Machine.

[![Contributors](https://contrib.rocks/image?repo=OpenMachineLabs/openmachine)](https://github.com/OpenMachineLabs/openmachine/graphs/contributors)

See [CONTRIBUTING.md](CONTRIBUTING.md) to get involved. The contributor image will populate once the repository is public.

## License

[MIT](LICENSE) © 2026 OpenMachineLabs.

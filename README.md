<div align="center">

# Open Machine

### One MCP. Every machine. Real execution.

**An open-source runtime that lets your existing AI agent operate your machines.**

<a href="openmachine/skills/overview/SKILL.md"><img src="https://img.shields.io/badge/MCP-Compatible-5C4EE5?style=flat-square" alt="MCP compatible"></a>
<a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.12%2B-3776AB?style=flat-square&amp;logo=python&amp;logoColor=white" alt="Python 3.12+"></a>
<a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-43A047?style=flat-square" alt="MIT License"></a>
<a href="https://discord.gg/TAxEWV7fgZ"><img src="https://img.shields.io/badge/Discord-Join-5865F2?style=flat-square&amp;logo=discord&amp;logoColor=white" alt="Join Discord"></a>
<a href="https://x.com/OpenMachineAI"><img src="https://img.shields.io/badge/X-Follow-181717?style=flat-square&amp;logo=x&amp;logoColor=white" alt="Follow on X"></a>



<img src="assets/openmachine-banner.png" width="1200" alt="Open Machine — Your AI. Your machines. Connect any AI to your context, and get things done on your machines." />

<br /><br />

<table aria-label="Open Machine community">
<tr>
<td align="center" width="520">
  <a href="https://discord.gg/TAxEWV7fgZ">
    <img src="https://img.shields.io/badge/Discord-Join%20the%20Community-5865F2?style=for-the-badge&amp;logo=discord&amp;logoColor=white" height="42" alt="Join the Open Machine Discord community" /><br />
    <strong>Join the Open Machine Community</strong>
  </a><br />
  <sub>Discord · Q&amp;A · Show and Tell</sub>
</td>
</tr>
</table>

</div>

**Open Machine (OM Core)** connects your existing AI Agents to your machines through a local-first MCP runtime. Your AI plans the work; OM provides permission-controlled tools to carry it out and return results.

[GitHub Issues](https://github.com/OpenMachineLabs/openmachine/issues) · [Contributing](CONTRIBUTING.md)

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

OM serves a **private Unix socket**. Running `serve` does not connect your AI agent by itself. Follow [Connect the official tunnel](openmachine/skills/overview/setup.md#connect-the-official-tunnel) to connect a supported AI client.

Command execution and workspace writes are **disabled by default** and require separate explicit grants. This is an initial-development release, not a stable 1.x release. Linux supports manual serving; Windows is not currently supported.

## How it works

<p align="center">
  <img src="assets/openmachine-workflow.png" alt="You request a task in an AI Agent, which plans and uses OM Core to perform authorized actions on your machines. Results return to the AI Agent. Gmail, Slack, GitHub, and Personal Context are shown as context sources above OM, with context updates flowing back.">
</p>

Your request flows from **AI Agent → OM Core → your machines**, and execution results return to your AI. OM exposes the approved tools; it does not replace your AI agent.

The **Personal Context layer and automatic context updates** in the diagram illustrate planned functionality for bringing data from sources such as Gmail and Slack into OM and keeping it up to date as tasks are completed.

## Documentation

- **[Setup and connection](openmachine/skills/overview/setup.md)** — installation, grants, platform requirements, and MCP connection.
- **[Operating Skill](openmachine/skills/overview/SKILL.md)** — the short starting point for AI agents.
- **[Skills directory](openmachine/skills/README.md)** — tool-specific instructions for workspace, commands, tasks, GitHub, browser, and services.

## Security

Open Machine is a trusted, single-operator native runtime, **not an OS sandbox** or multi-tenant executor. Native commands can access resources beyond read-only tool scopes. Keep the listener private, grant access deliberately, and remember that files and logs returned to an AI client may leave your machine. Read [setup and trust boundaries](openmachine/skills/overview/setup.md) before enabling execution or remote access.

## Contributors

Thanks to everyone who contributes to Open Machine.

<p align="center">
  <a href="https://github.com/OpenMachineLabs/openmachine/graphs/contributors">
    <img src="https://contrib.rocks/image?repo=OpenMachineLabs/openmachine" alt="Open Machine contributors" />
  </a>
</p>

[View GitHub contributors](https://github.com/OpenMachineLabs/openmachine/graphs/contributors) · [Contribute to Open Machine](CONTRIBUTING.md)

## License

[MIT](LICENSE) © 2026 OpenMachineLabs.

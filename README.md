<div align="center">

# Open Machine Core

**The access launcher that Open Machine SSH sessions run through.**

<a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.12%2B-3776AB?style=flat-square&amp;logo=python&amp;logoColor=white" alt="Python 3.12+"></a>
<a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-43A047?style=flat-square" alt="MIT License"></a>
<a href="https://discord.gg/TAxEWV7fgZ"><img src="https://img.shields.io/badge/Discord-Join-5865F2?style=flat-square&amp;logo=discord&amp;logoColor=white" alt="Join Discord"></a>
<a href="https://x.com/OpenMachineAI"><img src="https://img.shields.io/badge/X-Follow-181717?style=flat-square&amp;logo=x&amp;logoColor=white" alt="Follow on X"></a>

</div>

A coding agent on one machine of an Open Machine account runs `ssh <alias>.om` to
work on another machine of that account. The Code host on the target machine
verifies the session with Open Machine Cloud, then starts its command through
Core, which applies the device's access policy:

- **Full Access**: an ordinary shell as the machine's user.
- **Read Only** and **Selected Projects**: a macOS Seatbelt sandbox per session
  lifetime. It writes only to Full projects and a private home, and it denies
  credentials, other apps' data, external volumes and Open Machine state.

Core also answers the folder catalog the Console uses to choose Selected
Projects, and manages its own installation for the Open Machine Desktop app.

[GitHub Issues](https://github.com/OpenMachineLabs/openmachine/issues) · [Contributing](CONTRIBUTING.md) · [Code of Conduct](CODE_OF_CONDUCT.md)

## Interfaces

| Command | Caller | Purpose |
| --- | --- | --- |
| `python -I -m openmachine.access capabilities` | Code host | Core's version, session kinds and policies |
| `python -I -m openmachine.access session --control-fd N` | Code host | Admit one SSH channel |
| `python -I -m openmachine.access catalog` | Code host, for the Console | One folder catalog request on stdin |
| `python -I -B -m openmachine.cli --config C --state-dir S manage --contract 1 <action>` | Desktop | `status`, `configure`, `install`, `update`, `operation`, `continue` |

The configuration holds one machine, `[machines.local]` with `transport = "local"`.
Desktop installs and updates Core from a verified offline runtime payload, which
`scripts/build_runtime_payload.py` assembles. To work on Core itself, follow
[CONTRIBUTING.md](CONTRIBUTING.md).

## Security

Core is a single-operator access layer, **not** a multi-tenant security boundary.
A Full Access session can do anything the machine's user can; restricted sessions
rely on macOS Seatbelt and the rules in `openmachine/access/`. Report
vulnerabilities privately via the [Security Policy](SECURITY.md).

## Contributors

Thanks to everyone who contributes to Open Machine.

<a href="https://github.com/OpenMachineLabs/openmachine/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=OpenMachineLabs/openmachine&amp;max=1000&amp;columns=12" alt="Open Machine GitHub contributors" />
</a>

[View GitHub contributors](https://github.com/OpenMachineLabs/openmachine/graphs/contributors) · [Contribute to Open Machine](CONTRIBUTING.md) · [Maintainer guide](MAINTAINERS.md)

## License

[MIT](LICENSE) © 2026 OpenMachineLabs.

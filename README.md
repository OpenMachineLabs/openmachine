<div align="center">

# Open Machine

### One agent. Every machine. Real execution.

**An open-source platform that lets your coding agent work on every machine you own.**

<a href="https://openmachine.app"><img src="https://img.shields.io/badge/Website-openmachine.app-141414?style=flat-square" alt="Website"></a>
<a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.12%2B-3776AB?style=flat-square&amp;logo=python&amp;logoColor=white" alt="Python 3.12+"></a>
<a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-43A047?style=flat-square" alt="MIT License"></a>
<a href="https://discord.gg/TAxEWV7fgZ"><img src="https://img.shields.io/badge/Discord-Join-5865F2?style=flat-square&amp;logo=discord&amp;logoColor=white" alt="Join Discord"></a>
<a href="https://x.com/OpenMachineAI"><img src="https://img.shields.io/badge/X-Follow-181717?style=flat-square&amp;logo=x&amp;logoColor=white" alt="Follow on X"></a>

<img src="assets/openmachine-banner.png" width="1200" alt="Open Machine — Your AI. Your machines. Your coding agent, at work on every machine you own." />

<a href="https://openmachine.app/download"><picture><source media="(prefers-color-scheme: dark)" srcset="assets/button-download-dark.svg" /><img src="assets/button-download-light.svg" height="36" alt="Download Open Machine" /></picture></a>
<a href="https://console.openmachine.app"><picture><source media="(prefers-color-scheme: dark)" srcset="assets/button-console-dark.svg" /><img src="assets/button-console-light.svg" height="36" alt="Open the Console" /></picture></a>
<a href="https://openmachine.app"><picture><source media="(prefers-color-scheme: dark)" srcset="assets/button-website-dark.svg" /><img src="assets/button-website-light.svg" height="36" alt="Visit openmachine.app" /></picture></a>

</div>

<br />

<p align="center">
  <img src="assets/openmachine-hero.svg" width="1000" alt="Your machines in the Open Machine Console, a coding agent training a model on a GPU machine and opening a pull request, and your connected tools." />
</p>

Your coding agent already works on this machine. Open Machine lets it work on
all of them: train on the GPU machine, build on the workstation, test on the
spare Mac, with each machine's results coming back to you.

**Yours, end to end.** Machines connect peer-to-peer over encrypted WireGuard,
built on Tailscale. You choose how far the agent reaches on each machine, and
GitHub tokens never sit on your machines.

## Quick start

1. [Download](https://openmachine.app/download) the app and sign in on each machine.
2. Choose access per machine: **Read only**, **Selected projects** or **Full access**.
3. Ask your coding agent. It reaches a machine with one command:

   ```bash
   ssh studio.om
   ```

## How it fits together

- **App**: signs each machine in and keeps it reachable.
- **Console**: your machines, members and access, in one place.
- **Network**: a private WireGuard network between your machines.
- **Core** (this repository): starts the agent's work on each machine with the access you chose.

## Contributors

Thanks to everyone who contributes to Open Machine.

<a href="https://github.com/OpenMachineLabs/openmachine/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=OpenMachineLabs/openmachine&amp;max=1000&amp;columns=12" alt="Open Machine GitHub contributors" />
</a>

[View GitHub contributors](https://github.com/OpenMachineLabs/openmachine/graphs/contributors) · [Contribute to Open Machine](CONTRIBUTING.md) · [Maintainer guide](MAINTAINERS.md)

## License

[MIT](LICENSE) © 2026 OpenMachineLabs.

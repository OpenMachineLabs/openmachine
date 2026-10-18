# Open Machine Roadmap

This roadmap outlines planned features and longer-term directions. Scope and priorities may change.

## 1. Cloud MCP & Official Plugin

- [ ] Launch Cloud MCP with hosted access and direct sign-in, removing the need for local setup in common workflows.
- [ ] Release the official OM plugin for easier onboarding and integration.

## 2. Desktop App

- [ ] Deliver a desktop GUI for npm-based installation, updates, and persistent background service management.
- [ ] Add a machine dashboard for local and remote devices, connection health, and runtime status.
- [ ] Provide execution history, logs, and task monitoring, with distinct `running` and `ai_wait` states.
- [ ] Add a UI for installing, configuring, and managing plugins and Skills.

## 3. Multi-Machine Connectivity & Security

- [ ] Support multi-machine management over SSH and Tailscale.
- [ ] Add SSH access management and machine-level allowlists and denylists.
- [ ] Enforce operation-level permissions and block prohibited or high-risk AI actions.

## 4. Provider Backends

- [ ] Support additional backends, including Claude Code, Codex, OpenCode, and local models.
- [ ] Unify backend configuration, selection, and switching.

## 5. Context & Memory

- [ ] Capture detailed task and work context; summarize, structure, and index it for retrieval.
- [ ] Build OM Personal Memory to distill daily activity and work history into high-quality, reusable memory.
- [ ] Support separate personal and team memory spaces with appropriate access controls.

## 6. Integrations & Skills Ecosystem

- [ ] Integrate external services such as Slack and Gmail.
- [ ] Support cross-service search, summarization, and information synthesis.
- [ ] Enable custom Skills for workflows such as Daily Research, Linux Ops, and Meeting Review.
- [ ] Build a Skill Library for discovering, publishing, and reusing community Skills.

## 7. GPU & ML Workflows

- [ ] Support GPU workloads and model-training workflows.
- [ ] Integrate Weights & Biases and Grafana for training metrics, logs, and observability.

## Under Consideration

- [ ] Cross-device sharing and synchronization of execution logs.

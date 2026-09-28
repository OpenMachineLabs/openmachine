# Open Machine

An open-source runtime for AI to operate your machines.

## Current status

This repository is a **repository scaffold, not a product implementation or an
approved product architecture**. The proposed scaffold consists of contribution
rules, Issue and PR templates, and repository-quality checks.

The implementation language, runtime boundaries, user interface, providers,
transports, packaging, and deployment model remain open. Tooling used for CI
does not select a product language. Do not infer working features from this
scaffold or create empty product modules to imply progress.

## Working in this repository

- [Agent instructions](AGENTS.md): how to make a small, verifiable change.
- [Contributing](CONTRIBUTING.md): design, research, naming, review, and CI.
- [Spec and Survey index](spec/README.md): approved design inputs and evidence.

All repository-authored documentation, code comments, Issues, PRs, commits,
labels, and CI messages are written in English.

## Repository checks

Python 3.12 or newer is used only for standard-library repository tooling:

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
python3 scripts/check_repository.py
```

These checks do not test a runtime, machine access, providers, or a desktop app.
Product tests and dependencies will be added with the behavior they verify.

## License

[MIT](LICENSE).

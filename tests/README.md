# Repository Tests

These are standard-library tests for repository tooling, not product tests.
They cover title grammar, text hygiene, the language backstop, relative file
links, label definitions, event validation, and changed-commit checks.

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
python3 scripts/check_repository.py
```

The checker scans tracked text and reads GitHub event JSON as data. It does not
access the network, mutate GitHub, load a product, use credentials, or operate
machines. External URLs and heading anchors are not verified. English meaning,
live labels, approval, and product acceptance remain review responsibilities.

Add product tests only alongside implemented behavior. Do not create placeholder
runtime, integration, or end-to-end suites for this scaffold.

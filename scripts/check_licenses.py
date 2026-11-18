#!/usr/bin/env python3
"""List the license of every third-party Go module om links, every locked Python package and every part Core's
payload builds or ships, from their own license texts, metadata and build inputs, and fail on GPL, AGPL, an unknown
license or a list that differs from THIRD_PARTY_LICENSES.md. Needs Go and both lock files installed in the running
Python: `--write` rewrites the list.
"""

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
NOTICES = ROOT / "THIRD_PARTY_LICENSES.md"
LOCKS = ("device/requirements.lock", "requirements.lock")
PERMISSIVE = {"Apache-2.0", "MIT", "MIT-0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "PSF-2.0"}
# Weak-copyleft parts shipped unmodified with their notices; anything else outside PERMISSIVE fails.
EXCEPTIONS = {"pathspec": {"MPL-2.0"}, "bubblewrap": {"LGPL-2.0-or-later"}, "glibc": {"LGPL-2.1-or-later"}}
# Core's Linux payload builds bubblewrap from source, linked statically with the build host's libcap and glibc
# (scripts/build_runtime_payload.py); those two have no pinned version.
BUILT = (("bubblewrap", "LGPL-2.0-or-later"), ("glibc", "LGPL-2.1-or-later"), ("libcap", "BSD-3-Clause OR GPL-2.0-only"))
TEXTS = (  # Each license's own wording. The GNU licenses by their upper-case titles: each names the others in prose.
    ("AGPL", r"(?-i:GNU AFFERO GENERAL PUBLIC LICENSE)"),
    ("LGPL", r"(?-i:GNU (LESSER|LIBRARY) GENERAL PUBLIC LICENSE)"),
    ("GPL", r"(?-i:GNU GENERAL PUBLIC LICENSE)"),
    ("MPL-2.0", r"Mozilla Public License,? (Version|v\.) 2\.0"),
    ("Apache-2.0", r"Apache License,?\s+Version 2\.0"),
    ("MIT", r"Permission is hereby granted, free of charge, to any person obtaining a copy"),
    ("ISC", r"Permission to use, copy, modify, and(/or)? distribute this software for any\s+purpose"),
    ("BSD-3-Clause", r"Redistribution and use in source and binary forms(.|\n)*(Neither the name|names of its\s+contributors)"),
    ("BSD-2-Clause", r"Redistribution and use in source and binary forms"),
)
# A GNU license named in prose, as in "licensed under the GNU General Public License" or "GPLv2", outside a GNU
# license's own text and outside the MPL's definition of its secondary licenses: refused, since the part it covers is
# unknown.
GNU_NOTICE = r"GNU (Affero |Lesser |Library )?General Public Licen[cs]e|\b[AL]?GPL(v?-?\d|\b)"
MPL_SECONDARY = (r"\"Secondary License\" means either the GNU General Public License.*?"
                 r"any later versions of those licenses\.")
# Code whose names hold a license file's, such as license.go, LICENSE_test.go or license.cpython-312.pyc.
CODE = {"a", "c", "cc", "cpp", "dylib", "go", "h", "java", "js", "json", "mjs", "mod", "o", "py", "pyc", "pyd", "pyi",
        "pxd", "pyx", "rs", "s", "sh", "so", "sum", "ts", "yaml", "yml"}
CLASSIFIERS = {"MIT License": "MIT", "BSD License": "BSD", "Apache Software License": "Apache-2.0",
               "ISC License (ISCL)": "ISC", "Python Software Foundation License": "PSF-2.0",
               "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0"}


def from_text(text: str) -> list[str]:
    """Every license a file holds, as one file may hold several; a BSD-3-Clause text also matches BSD-2-Clause's."""
    text = re.sub(r"\s+", " ", text)  # a phrase may break across lines
    found = [name for name, pattern in TEXTS if re.search(pattern, text, re.IGNORECASE)]
    if "BSD-3-Clause" in found:
        found.remove("BSD-2-Clause")
    if not {"AGPL", "LGPL", "GPL"} & set(found) and re.search(
            GNU_NOTICE, re.sub(MPL_SECONDARY, "", text, flags=re.IGNORECASE), re.IGNORECASE):
        found.append("GNU-notice")
    return found


def from_texts(files) -> str:
    """Every license that any of these files holds; unknown if one holds none."""
    found = set()
    for file in files:
        found |= set(from_text(file.read_text(errors="replace")) or ["unknown"])
    return " AND ".join(sorted(found)) or "unknown"


def is_license_name(name: str) -> bool:
    """Any name holding LICENSE, LICENCE or COPYING, such as COPYING3, GPL-LICENSE or LICENSE.md; not license.go."""
    return bool(re.search(r"LICEN[CS]E|COPYING", name, re.IGNORECASE)) and (
        "." not in name or name.rsplit(".", 1)[1].lower() not in CODE)


def license_files(directory: Path) -> list[Path]:
    return sorted(item for item in directory.iterdir() if item.is_file() and is_license_name(item.name))


def go_modules() -> dict[tuple[str, str], str]:
    """Each third-party module the macOS and Linux builds of om link, with every license in the license files of
    each linked package's directory and the directories above it, up to the module's root."""
    own = re.search(r"^module (\S+)$", (ROOT / "go.mod").read_text(), re.MULTILINE)[1]
    directories = {}
    for system in ("linux", "darwin"):
        environment = {**os.environ, "GOOS": system, "CGO_ENABLED": "1", "GOFLAGS": "-mod=readonly", "GOWORK": "off"}
        listing = subprocess.run(["go", "list", "-deps", "-f",
                                  "{{with .Module}}{{if not .Main}}{{.Path}}\t{{.Version}}\t{{.Dir}}\t{{$.Dir}}{{end}}{{end}}",
                                  "./cmd/om"], cwd=ROOT, env=environment, check=True, capture_output=True,
                                 text=True).stdout
        for line in filter(None, listing.splitlines()):
            path, version, root, package = line.split("\t")
            if path.startswith(own + "/"):
                continue  # this repository's own modules, such as the network library
            root, package = Path(root), Path(package)
            linked = directories.setdefault((path, version), set())
            linked.update(directory for directory in (package, *package.parents)
                          if directory == root or directory.is_relative_to(root))
    return {module: from_texts(file for directory in sorted(linked) for file in license_files(directory))
            for module, linked in directories.items()}


def python_packages(lock: str) -> dict[tuple[str, str], str]:
    """Each package a lock file pins, with the license its installed metadata states."""
    found = {}
    for line in (ROOT / lock).read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        name, version = line.split("==")
        try:
            metadata = importlib.metadata.metadata(name)
        except importlib.metadata.PackageNotFoundError:
            raise SystemExit(f"check_licenses: install {lock} first: {name} is missing") from None
        if metadata["Version"] != version:
            raise SystemExit(f"check_licenses: {name} {metadata['Version']} is installed, {lock} pins {version}")
        expression = metadata.get("License-Expression")
        if not expression:
            classified = [CLASSIFIERS.get(item.rsplit(" :: ", 1)[-1]) for item in metadata.get_all("Classifier") or []
                          if item.startswith("License :: OSI Approved :: ")]
            classified = [item for item in classified if item and item != "BSD"] or classified
            texts = [file.locate() for file in importlib.metadata.distribution(name).files or []
                     if is_license_name(file.name)]
            if classified == ["BSD"] and texts:
                classified = [from_texts(texts)]
            expression = " OR ".join(sorted(set(filter(None, classified)))) or (from_texts(texts) if texts else "unknown")
        found[name, version] = expression
    return found


def permitted(expression: str, allowed: set[str]) -> bool:
    """Whether a license expression of AND, OR, WITH and parentheses can be met with allowed licenses alone."""
    tokens, position = re.findall(r"[()]|[^\s()]+", expression), 0

    def term() -> bool:
        nonlocal position
        if position == len(tokens) or tokens[position] in {")", "AND", "OR", "WITH"}:
            raise ValueError(expression)
        position += 1
        if tokens[position - 1] == "(":
            value = either()
            if position == len(tokens) or tokens[position] != ")":
                raise ValueError(expression)
            position += 1
            return value
        value = tokens[position - 1] in allowed
        if position < len(tokens) and tokens[position] == "WITH":  # an exception only adds permissions
            if position + 1 == len(tokens) or tokens[position + 1] in {"(", ")", "AND", "OR", "WITH"}:
                raise ValueError(expression)
            position += 2
        return value

    def chain(operator: str, item, combine) -> bool:
        nonlocal position
        value = item()
        while position < len(tokens) and tokens[position] == operator:
            position += 1
            value = combine(item(), value)
        return value

    def either() -> bool:
        return chain("OR", lambda: chain("AND", term, lambda a, b: a and b), lambda a, b: a or b)

    try:
        return either() and position == len(tokens)
    except ValueError:
        return False


def refusals(name: str, expression: str) -> list[str]:
    allowed = PERMISSIVE | EXCEPTIONS.get(name, set())
    return [] if permitted(expression, allowed) else [f"{name}: {expression} is not an allowed license"]


def inputs() -> dict:
    return json.loads((ROOT / "scripts/runtime-inputs.json").read_text())


def built() -> dict[tuple[str, str], str]:
    versions = {target["bubblewrap"]["version"] for target in inputs().values() if "bubblewrap" in target}
    return {(name, version if name == "bubblewrap" else "the build host's"): expression
            for version in versions for name, expression in BUILT}


def runtimes() -> list[str]:
    found = {(target["python"]["build"], target["python"]["version"]) for target in inputs().values()}
    return [f"- python-build-standalone {build}, CPython {version}" for build, version in sorted(found)]


def render(go: dict, python: dict[str, dict], source: dict) -> str:
    lines = ["## Go modules linked into `om` on macOS and Linux", ""]
    lines += [f"- `{path}` {version} ({found})" for (path, version), found in sorted(go.items())]
    for lock, packages in python.items():
        lines += ["", f"## Python packages pinned in `{lock}`", ""]
        lines += [f"- `{name}` {version} ({found})" for (name, version), found in sorted(packages.items())]
    lines += ["", "## Built into Core's Linux payload: bubblewrap from source, linked statically with the build host's "
              "libcap and glibc", ""]
    lines += [f"- `{name}` {version} ({found})" for (name, version), found in sorted(source.items())]
    lines += ["", "## Python runtimes in Core's payload", "",
              "PSF-2.0 for CPython; the licenses of the libraries each bundles ship inside it, unchecked here.", ""]
    lines += runtimes()
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite the generated list in THIRD_PARTY_LICENSES.md")
    args = parser.parse_args(argv)
    go = go_modules()
    python = {lock: python_packages(lock) for lock in LOCKS}
    source = built()
    errors = [error for (path, _), found in go.items() for error in refusals(path, found)]
    errors += [error for packages in python.values() for (name, _), found in packages.items()
               for error in refusals(name, found)]
    errors += [error for (name, _), found in source.items() for error in refusals(name, found)]
    head, marker, _ = NOTICES.read_text().partition("<!-- generated by scripts/check_licenses.py -->\n")
    if not marker:
        errors.append("THIRD_PARTY_LICENSES.md has lost its generated-list marker")
    expected = head + marker + render(go, python, source)
    if args.write and marker:
        NOTICES.write_text(expected)
    elif NOTICES.read_text() != expected:
        errors.append("THIRD_PARTY_LICENSES.md differs from the build graph and lock files; run with --write")
    for error in errors:
        print(f"FAIL: {error}", file=sys.stderr)
    if not errors:
        print(f"PASS: {len(go)} Go modules and {sum(map(len, python.values()))} Python packages, all allowed")
    return bool(errors)


if __name__ == "__main__":
    sys.exit(main())

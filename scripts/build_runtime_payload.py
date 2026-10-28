"""Assemble the Apple Silicon runtime from pinned inputs and one explicit Core wheel."""

import argparse
from email.parser import BytesParser
import hashlib
import json
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile

from openmachine.service import payload


def fetch(item: dict, destination: Path) -> None:
    if not destination.exists():
        with urllib.request.urlopen(item["url"], timeout=120) as source, destination.open("xb") as output:
            shutil.copyfileobj(source, output)
    with destination.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != item["sha256"]:
            raise ValueError("An input archive differs from its pinned digest; preserve it for diagnosis.")


def extract(source: Path, destination: Path) -> None:
    with tarfile.open(source) as archive:
        members = archive.getmembers()
        # No hard links: retain ordinary internal symlinks while avoiding archive
        # hard-link alias behavior on build interpreters with older tar filters.
        if (len(members) > payload.FILE_LIMIT
                or sum(member.size for member in members) > payload.BYTE_LIMIT
                or any(not (member.isfile() or member.isdir() or member.issym()) for member in members)):
            raise ValueError("Unsupported input archive members")
        archive.extractall(destination, members=members, filter="data")


def run(argv: list[str], env: dict[str, str]) -> None:
    subprocess.run(argv, cwd="/", env=env, stdin=subprocess.DEVNULL, check=True, timeout=300)


def git_entry(root: Path) -> None:
    """Dugite's native Git needs explicit, relocatable helper and template paths."""
    (root / "bin").mkdir(exist_ok=True)
    entry = root / "bin/git"
    entry.write_text('''#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(/usr/bin/dirname -- "$0")/../git" && pwd)
export GIT_EXEC_PATH="$root/libexec/git-core"
export GIT_TEMPLATE_DIR="$root/share/git-core/templates"
exec "$root/bin/git" "$@"
''')
    entry.chmod(0o755)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--requirements", type=Path, required=True)
    parser.add_argument("--commit", required=True, help="Exact source commit used to build the selected wheel")
    parser.add_argument("--output", type=Path, required=True, help="A new payload directory; never overwrite an existing build")
    parser.add_argument("--cache", type=Path, required=True, help="Directory for checksum-verified input archives")
    args = parser.parse_args()
    if sys.platform != "darwin" or platform.machine() != "arm64" or not re.fullmatch(r"[0-9a-f]{40}", args.commit):
        parser.error("Build on Apple Silicon and select an exact source commit.")
    wheel, requirements = args.wheel.resolve(strict=True), args.requirements.resolve(strict=True)
    lines = [line.strip() for line in requirements.read_text().splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not lines or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*==[A-Za-z0-9][A-Za-z0-9_.+!-]*", line) for line in lines):
        parser.error("Requirements must pin every dependency without index overrides or source inputs.")
    with zipfile.ZipFile(wheel) as archive:
        metadata = [name for name in archive.namelist() if re.fullmatch(r"[^/]+\.dist-info/METADATA", name)]
        if len(metadata) != 1:
            parser.error("Select one Core wheel.")
        package = BytesParser().parsebytes(archive.read(metadata[0]))
        if package["Name"] != "openmachine" or not package["Version"]:
            parser.error("The selected wheel is not Open Machine.")
        if archive.read("openmachine/service/payload.py") != Path(payload.__file__).read_bytes():
            parser.error("Install the selected Core wheel in the build environment before running this script.")
        version = package["Version"]
    inputs = json.loads(Path(__file__).with_name("runtime-inputs.json").read_text())
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    cache = args.cache.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    for name, item in inputs.items():
        archive = cache / (item["sha256"] + ".tar.gz")
        fetch(item, archive)
        destination = output if name == "python" else output / "git"
        destination.mkdir(exist_ok=True)
        extract(archive, destination)
    git_entry(output)
    python = str(output / "python/bin/python3")
    env = payload.environment(output)
    wheels = output / "wheels"
    wheels.mkdir()
    shutil.copyfile(requirements, output / "requirements.lock")
    shutil.copyfile(wheel, wheels / wheel.name)
    # Only the producer downloads. Both bootstrap population and user-side
    # preparation use the complete local wheelhouse without source builds.
    download_env = {key: value for key, value in env.items() if key != "PIP_NO_INDEX"}
    run([python, "-I", "-B", "-m", "pip", "download", "--index-url", "https://pypi.org/simple",
         "--only-binary=:all:", "--no-deps", "--platform", "macosx_13_0_arm64",
         "--implementation", "cp", "--python-version", "3.13", "--abi", "cp313",
         "--dest", str(wheels), "-r", str(output / "requirements.lock")], download_env)
    original_entries = set((output / "python/bin").iterdir())
    run([python, "-I", "-B", "-m", "pip", "install", "--no-index", "--find-links", str(wheels),
         "--no-deps", "--only-binary=:all:", "--no-compile", "-r", str(output / "requirements.lock"),
         "openmachine==" + version], env)
    run([python, "-I", "-B", "-m", "pip", "check"], env)
    run([python, "-I", "-B", "-m", "openmachine.cli", "--version"], env)
    run([str(output / "bin/git"), "--version"], env)
    # Generated entry-point scripts contain the build path. The relocatable
    # bootstrap uses python -m; the final venv creates its own correct entries.
    for entry in set((output / "python/bin").iterdir()) - original_entries:
        if not entry.is_file() or entry.is_symlink():
            raise ValueError("Unexpected generated entry point")
        entry.unlink()
    (output / "THIRD_PARTY_NOTICES.md").write_text(
        "# Runtime inputs\n\n"
        "Python: python-build-standalone " + inputs["python"]["build"] +
        "; retain python/lib/python3.13/LICENSE.txt and all bundled dependency licenses.\n\n"
        "Git: dugite-native " + inputs["git"]["build"] +
        "; retain git/libexec/git-core/NOTICE. Git is GPL-2.0.\n\n"
        "Source and build recipes: https://github.com/astral-sh/python-build-standalone/tree/" + inputs["python"]["build"] +
        " and https://github.com/desktop/dugite-native/tree/v2.53.0-3.\n"
        "Git source: https://github.com/git/git/tree/v2.53.0.\n\n"
        "Original input URLs/digests are in manifest.json. Dependency licenses remain in their wheels "
        "and installed distribution metadata. Product publication must fulfill the corresponding-source "
        "and notice obligations for all bundled components; this assembly is not signed product acceptance.\n")
    manifest = {"format": 1, "target": payload.TARGET,
                "core": {"version": version, "commit": args.commit, "wheel": "wheels/" + wheel.name},
                **inputs, "files": payload.inventory(output)}
    raw = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    (output / "manifest.json").write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    payload.verify(output, digest)
    print(json.dumps({"payload": str(output), "sha256": digest, "files": len(manifest["files"]),
                      "bytes": sum(item.get("bytes", 0) for item in manifest["files"].values()),
                      "signed": False}))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Produce the offline bundle of om and device identity: the one the Mac app
embeds, or the Linux product's.

The release owner supplies an exact, reviewed python-build-standalone
install_only archive for the target, which the build host must be. No runtime is
fetched or selected implicitly. The bundle also carries om, built from this
checkout with the host's Go, which must be exactly the toolchain go.mod pins,
and the license texts of every module it links. Signing, notarization and
publication are distinct steps.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "device"

# Linux targets: Go's architecture and the oldest wheels pip may select (glibc 2.17).
LINUX = {"linux-x86_64": ("amd64", "manylinux2014_x86_64"), "linux-aarch64": ("arm64", "manylinux2014_aarch64")}
HOST = f"{sys.platform}-{os.uname().machine}"

MACH_O = {b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def release_version() -> str:
    """The repository's one version, which the bundle, om and Core share."""
    found = re.search(r'^__version__ = "(\d+\.\d+\.\d+)"$', (ROOT / "access/openmachine_access/__init__.py").read_text(), re.MULTILINE)
    if not found:
        raise ValueError("access/openmachine_access/__init__.py declares no version")
    return found[1]


def run(arguments: list[str], *, timeout: int = 600, **options) -> subprocess.CompletedProcess:
    return subprocess.run(arguments, check=True, stdin=subprocess.DEVNULL, timeout=timeout, **options)


def build_om(bundle: Path, target: str) -> None:
    """Build om into the bundle, with its modules' license texts under licenses/om."""
    pinned = re.search(r"^go (\d+\.\d+\.\d+)$", (ROOT / "go.mod").read_text(), re.MULTILINE)
    if not pinned:
        raise ValueError("go.mod pins no exact Go toolchain")
    # The host's own toolchain, which must be exactly the pinned one; no go.work applies. macOS builds
    # use cgo, as native ones do, so a missing compiler fails; a Linux build is one static binary.
    env = {**os.environ, "GOTOOLCHAIN": "local", "GOWORK": "off", "CGO_ENABLED": "1", "GOFLAGS": "-mod=readonly"}
    if target in LINUX:
        env.update(CGO_ENABLED="0", GOOS="linux", GOARCH=LINUX[target][0])
    go = lambda *arguments: run(["go", *arguments], cwd=ROOT, env=env, capture_output=True, text=True).stdout
    if go("env", "GOVERSION").strip() != "go" + pinned[1]:
        raise ValueError("Build om with Go " + pinned[1])
    run(["go", "build", "-trimpath", "-ldflags=-s -w", "-o", str(bundle / "om"), "./cmd/om"], cwd=ROOT, env=env)
    notices = bundle / "licenses/om"
    modules = go("list", "-deps", "-f", "{{with .Module}}{{if not .Main}}{{.Path}}\t{{.Dir}}{{end}}{{end}}", "./cmd/om")
    for module, directory in sorted({tuple(line.split("\t")) for line in modules.splitlines() if line} |
                                    {("go", go("env", "GOROOT").strip())}):
        texts = [item for item in Path(directory).iterdir()
                 if item.is_file() and re.fullmatch(r"(LICEN[CS]E|COPYING|NOTICE|PATENTS)(\.(md|txt))?", item.name, re.IGNORECASE)]
        if not texts:
            raise ValueError(f"{module} has no license text to ship")
        (notices / module).mkdir(parents=True, exist_ok=True)
        for text in texts:
            shutil.copyfile(text, notices / module / text.name)


def inventory(bundle: Path, version: str, python_digest: str) -> dict:
    files, links = {}, {}
    for path in sorted(bundle.rglob("*")):
        name = path.relative_to(bundle).as_posix()
        if not path.resolve().is_relative_to(bundle.resolve()):
            raise ValueError("The runtime has a link outside the bundle")
        if path.is_symlink():
            links[name] = os.readlink(path)
        elif path.is_file():
            files[name] = digest(path)
    return {"format": 1, "companion_protocol": 1, "setup_protocol": 1, "connection_protocol": 1,
            "product": "openmachine-connector", "version": version,
            "platform": sys.platform, "architecture": os.uname().machine,
            "python_archive_sha256": python_digest, "files": files, "links": links}


def app_shell(application: Path, version: str) -> None:
    contents = application / "Contents"
    executable = contents / "MacOS/OpenMachineConnector"
    executable.parent.mkdir(parents=True)
    executable.write_text('#!/bin/sh\nset -eu\n'
        'if [ "$#" -eq 0 ]; then exit 0; fi\n'
        'bundle=$(CDPATH= cd -- "$(dirname -- "$0")/../Resources/bundle" && pwd)\n'
        'exec "$bundle/python/bin/python3" -I -B "$bundle/entry.py" "$@"\n')
    executable.chmod(0o755)
    (contents / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": "app.openmachine.connector.installer",
        "CFBundleName": "Open Machine Connector",
        "CFBundleDisplayName": "Open Machine Connector",
        "CFBundleExecutable": "OpenMachineConnector",
        "CFBundlePackageType": "APPL",
        "CFBundleVersion": version,
        "CFBundleShortVersionString": version,
        "LSMinimumSystemVersion": "13.0",
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
    }))


def build(args) -> Path:
    if args.target != HOST:
        raise ValueError("Build on a host of the target")
    os.umask(0o022)  # Core refuses a group-writable om
    archive = args.python_archive.resolve()
    if digest(archive) != args.python_sha256.lower():
        raise ValueError("The selected Python archive does not match its expected SHA256")
    if args.sign_identity and sys.platform != "darwin":
        raise ValueError("Developer ID signing requires a macOS build")
    version = release_version()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    name = f"openmachine-connector-{version}-{sys.platform}-{os.uname().machine}"
    release = output / name
    if release.exists():
        raise ValueError("The immutable output already exists; select a new output directory")
    with tempfile.TemporaryDirectory(prefix="connector-build-", dir=output) as directory:
        staging = Path(directory)
        app = staging / "Open Machine Connector.app"
        bundle = app / "Contents/Resources/bundle" if sys.platform == "darwin" else staging / "bundle"
        bundle.mkdir(parents=True)
        # The data filter rejects traversal, outside links and special files.
        with tarfile.open(archive) as source:
            members = source.getmembers()
            if len(members) > 50000 or sum(max(0, item.size) for item in members) > 2 * 1024**3:
                raise ValueError("The standalone Python archive exceeds its build budget")
            source.extractall(bundle, members=members, filter="data")
        python = bundle / "python/bin/python3"
        if not python.is_file() or not python.resolve().is_relative_to(bundle.resolve()):
            raise ValueError("Select an install_only archive with python/bin/python3")
        probe = "import platform,sys; assert sys.version_info[:2] == (3,12); assert sys.platform == sys.argv[1]; assert platform.machine() == sys.argv[2]"
        run([str(python), "-I", "-c", probe, sys.platform, os.uname().machine], timeout=15)
        app_packages = bundle / "app"
        platform = (["--platform", LINUX[args.target][1], "--implementation", "cp", "--python-version", "3.12",
                     "--abi", "cp312"] if args.target in LINUX else [])
        run([sys.executable, "-m", "pip", "--python", str(python), "install", "--disable-pip-version-check",
             "--no-compile", "--no-deps", "--only-binary=:all:", *platform, "--target", str(app_packages),
             "-r", str(SOURCE / "requirements.lock")])
        shutil.copytree(SOURCE / "openmachine_device", app_packages / "openmachine_device",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (bundle / "entry.py").write_text('import sys\nfrom pathlib import Path\nsys.dont_write_bytecode = True\n'
            'sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))\n'
            'from openmachine_device.cli import main\nmain()\n')
        run([str(python), "-I", str(bundle / "entry.py"), "--help"], timeout=15)
        build_om(bundle, args.target)
        if args.sign_identity:
            for path in sorted(bundle.rglob("*"), reverse=True):
                if path.is_file() and not path.is_symlink():
                    with path.open("rb") as handle:
                        native = handle.read(4) in MACH_O
                    if native:
                        run(["/usr/bin/codesign", "--force", "--options", "runtime", "--timestamp",
                             "--sign", args.sign_identity, str(path)], timeout=120)
        manifest = inventory(bundle, version, args.python_sha256.lower())
        (bundle / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
        if sys.platform == "darwin":
            app_shell(app, version)
            if args.sign_identity:
                run(["/usr/bin/codesign", "--force", "--options", "runtime", "--timestamp",
                     "--sign", args.sign_identity, str(app)], timeout=120)
                run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], timeout=120)
            release.mkdir()
            shutil.move(str(app), str(release / app.name))
            artifact = release / f"{name}-internal.tar.gz"
            with tarfile.open(artifact, "w:gz") as target:
                target.add(release / app.name, arcname=app.name)
            metadata = {"version": version, "platform": sys.platform, "architecture": os.uname().machine,
                        "artifact": artifact.name, "sha256": digest(artifact),
                        "bundle_manifest_sha256": digest(release / app.name / "Contents/Resources/bundle/manifest.json"),
                        "signing": "signed" if args.sign_identity else "not_configured",
                        "notarization": "not_run",
                        "cold_install_acceptance": "not_run", "publication": "not_run"}
            (release / "release.json").write_text(json.dumps(metadata, sort_keys=True, indent=2) + "\n")
        else:
            # The Linux release packs this directory as it is.
            release.mkdir()
            shutil.move(str(bundle), str(release / "bundle"))
    return release


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-archive", type=Path, required=True)
    parser.add_argument("--python-sha256", required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/connector")
    parser.add_argument("--sign-identity", help="Existing Developer ID Application identity; never an embedded credential")
    parser.add_argument("--target", choices=sorted({HOST, *LINUX}), default=HOST,
                        help="The bundle's target; the build host must be one (default: this host's)")
    args = parser.parse_args()
    try:
        print(build(args))
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Connector build did not complete: {type(error).__name__}. Inspect the selected local build inputs.\n")


if __name__ == "__main__":
    main()

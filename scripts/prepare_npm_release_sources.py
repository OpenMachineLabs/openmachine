"""Download exactly pinned third-party source and build inputs for npm distribution."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import zipfile

MAX_SOURCE_BYTES = 100 * 1024 * 1024

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    args = parser.parse_args()
    pins = json.loads((Path(__file__).resolve().parents[1] / "npm/release-sources.json").read_text())
    if pins.get("format") != 1 or len(pins.get("archives", [])) != 5:
        parser.error("Invalid release source manifest")
    runtime = args.runtime.resolve(strict=True)
    manifest = json.loads((runtime / "manifest.json").read_text())
    if (manifest.get("git", {}).get("version") != "2.53.0" or
        manifest.get("git", {}).get("build") != "v2.53.0-3/f49d009" or
        manifest.get("python", {}).get("build") != pins["python_build"]):
        raise ValueError("Runtime source versions differ from pinned source archives")
    if not (runtime / "python/lib/python3.13/LICENSE.txt").is_file() or not (
            runtime / "git/libexec/git-core/NOTICE").is_file():
        raise ValueError("Missing Python or bundled Git notices")
    wheels = sorted((runtime / "wheels").glob("*.whl"))
    if len(wheels) < 30:
        raise ValueError("Incomplete offline Python wheelhouse")
    for wheel in wheels:
        with zipfile.ZipFile(wheel) as source:
            if not any(".dist-info/licenses/" in member for member in source.namelist()):
                raise ValueError("Bundled wheel missing license text: " + wheel.name)
    cache = args.cache.resolve()
    cache.mkdir(parents=True, mode=0o700, exist_ok=True)
    output = args.output.resolve()
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    for item in pins["archives"]:
        name, url, digest = item["filename"], item["url"], item["sha256"]
        if name != Path(name).name or not url.startswith("https://"):
            raise ValueError("Unsafe source URL or filename")
        archive = cache / name
        if not archive.exists():
            partial = cache / ("." + name + ".downloading")
            with partial.open("xb"):
                pass
            fetched = subprocess.run(["curl", "--fail", "--location", "--silent", "--show-error",
                "--connect-timeout", "20", "--max-time", "120", "--output", str(partial), url],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=145)
            if fetched.returncode:
                raise ValueError("Source download failed; retained original: " + name)
            partial.rename(archive)
        if archive.stat().st_size <= 0 or archive.stat().st_size > MAX_SOURCE_BYTES:
            raise ValueError("Unsafe source archive size: " + name)
        with archive.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise ValueError("Pinned source hash mismatch: " + name)
        with tarfile.open(archive, "r:*") as tar:
            members = tar.getmembers()
            filenames = {member.name.rstrip("/") for member in members}
            if len(members) > 40000 or not set(item["required"]).issubset(filenames):
                raise ValueError("Corresponding source or build scripts missing: " + name)
            for member in members:
                if member.name.startswith("/") or ".." in Path(member.name).parts or member.isdev():
                    raise ValueError("Unsafe archive member in: " + name)
        if name.startswith("dugite-native-"):
            with tarfile.open(archive, "r:*") as dugite:
                deps_name = next(m.name for m in dugite.getmembers() if m.name.endswith("/dependencies.json"))
                deps = json.loads(dugite.extractfile(deps_name).read())
                if (deps["git"]["version"] != "v2.53.0" or
                    deps["git-lfs"]["version"] != pins["git_lfs_version"] or
                    deps["git-credential-manager"]["version"] != pins["gcm_version"]):
                    raise ValueError("Bundled Git/LFS/GCM versions do not match source pins")
        shutil.copyfile(archive, output / name)
    print(json.dumps({"source_archives": len(pins["archives"]), "output": str(output)}))

if __name__ == "__main__":
    main()

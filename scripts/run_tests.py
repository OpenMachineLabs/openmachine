#!/usr/bin/env python3
"""Select module-owned tests and run one source-unit or installed-wheel candidate."""

import argparse
import ast
import hashlib
import importlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tarfile
import tempfile
import time
import types
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "test/ownership.json"
LAYERS = ("repository", "unit", "component", "skill-library", "contract", "resilience")
GLOBAL_PATHS = {
    "requirements.lock", "pyproject.toml", "scripts/run_tests.py",
    "scripts/premerge.py", ".github/workflows/quality.yml",
}
REPOSITORY_PATHS = {
    "AGENTS.md", "CONTRIBUTING.md", "MAINTAINERS.md", "SECURITY.md",
    "CODE_OF_CONDUCT.md", "README.md", "LICENSE", ".gitignore",
    "examples/config.toml",
    ".github/AGENTS.md", ".github/PULL_REQUEST_TEMPLATE.md", ".github/pull_request_template.md", ".github/labels.json",
    ".github/workflows/metadata.yml", ".github/workflows/labels.yml",
    ".github/workflows/spec-development.yml", "scripts/check_repository.py",
}


def git(root: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], check=check,
                            capture_output=True, text=True, timeout=30)
    return result.stdout


def resolve_commit(root: Path, ref: str) -> str:
    if not ref or ref.startswith("-") or any(ord(char) < 32 for char in ref):
        raise ValueError("base must be an explicit local Git ref or full SHA")
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", f"{ref}^{{commit}}"],
                            capture_output=True, text=True, timeout=30)
    sha = result.stdout.strip()
    if result.returncode or len(sha) != 40 or any(char not in "0123456789abcdef" for char in sha):
        raise ValueError(f"base {ref!r} is not available locally; fetch it explicitly")
    return sha


def require_ancestor(root: Path, base_sha: str, head_sha: str) -> None:
    result = subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", base_sha, head_sha],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError("base is not an ancestor of HEAD; update or select the exact candidate base")


def require_clean(root: Path) -> None:
    if git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("premerge requires a clean committed worktree")


def inventory(root: Path) -> set[str]:
    names = set(filter(None, git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0")))
    return {name for name in names if (root / name).exists()}


def candidate_digest(root: Path, files: set[str]) -> str:
    inputs = [(path, hashlib.sha256((root / path).read_bytes()).hexdigest()) for path in sorted(files)]
    candidate = {"files": inputs, "index": git(root, "ls-files", "--stage", "-z")}
    return hashlib.sha256(json.dumps(candidate, separators=(",", ":")).encode()).hexdigest()


def source_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("ownership paths must be nonempty repository-relative POSIX paths")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError(f"unsafe ownership path: {value!r}")
    if any(char in value for char in "*?[\0\r\n"):
        raise ValueError(f"ownership uses exact file paths, not patterns: {value!r}")
    return value


def test_path(value: object) -> str:
    path = source_path(value)
    parts = PurePosixPath(path).parts
    if len(parts) < 3 or parts[0] != "test" or parts[1] not in LAYERS:
        raise ValueError(f"test must have an explicit layer: {path}")
    if parts[1] == "skill-library" and len(parts) < 4:
        raise ValueError(f"skill-library tests require a named Skill directory: {path}")
    if not parts[-1].startswith("test_") or not path.endswith(".py"):
        raise ValueError(f"expected an exact test_*.py file: {path}")
    return path


def _unique_paths(value: object, *, tests: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("ownership entries must be lists of exact paths")
    paths = [(test_path(item) if tests else source_path(item)) for item in value]
    if len(set(paths)) != len(paths):
        raise ValueError("duplicate path in ownership entry")
    return paths


def check_fixture_consumers(root: Path, files: set[str], tests: set[str], support: dict) -> None:
    helpers = {path for path in support if path.endswith(".py")}
    initializers = {path for path in files if path.startswith("test/") and path.endswith("/__init__.py")}
    python_paths = tests | helpers | initializers

    def module_name(path):
        name = path[:-3].replace("/", ".")
        return name.removesuffix(".__init__")

    names = {module_name(path): path for path in python_paths}
    edges = {path: set() for path in python_paths}
    for path in python_paths:
        package = module_name(path) if path.endswith("/__init__.py") else module_name(path).rsplit(".", 1)[0]
        parts = package.split(".")
        for end in range(1, len(parts) + 1):
            initializer = names.get(".".join(parts[:end]))
            if initializer and initializer != path:
                edges[path].add(initializer)
        for node in ast.walk(ast.parse((root / path).read_text(), filename=path)):
            imports = []
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    base = importlib.util.resolve_name("." * node.level + base, package)
                imports = [base, *(base + "." + alias.name for alias in node.names)]
            for name in imports:
                # Importing a helper executes its package initializers as well.
                parts = name.split(".")
                for end in range(1, len(parts) + 1):
                    dependency = names.get(".".join(parts[:end]))
                    if dependency:
                        if dependency in tests and dependency != path:
                            raise ValueError(f"tests must use support fixtures, not import another test: {path}")
                        edges[path].add(dependency)
    for test in tests:
        seen, pending = set(), list(edges[test])
        while pending:
            path = pending.pop()
            if path not in seen:
                seen.add(path)
                pending.extend(edges[path] - seen)
        for helper in seen & helpers:
            if test not in support[helper]:
                raise ValueError(f"fixture consumer is not registered: {helper} -> {test}")


def validate_manifest(value: object, files: set[str] | None = None, root: Path | None = None) -> dict:
    if not isinstance(value, dict) or set(value) != {"format", "modules", "support"} or value["format"] != 1:
        raise ValueError("unsupported or incomplete test ownership manifest")
    modules, support = value["modules"], value["support"]
    if not isinstance(modules, dict) or not modules or not isinstance(support, dict):
        raise ValueError("ownership needs nonempty modules and an explicit support map")
    owned_sources, owned_tests = {}, {}
    for name, entry in modules.items():
        if not isinstance(name, str) or not name or not isinstance(entry, dict):
            raise ValueError("invalid ownership module")
        if set(entry) != {"sources", "tests", "boundary_tests"}:
            raise ValueError(f"{name}: expected sources, tests, and boundary_tests")
        for key in entry:
            _unique_paths(entry[key], tests=key != "sources")
        if not entry["tests"] and not entry["boundary_tests"]:
            raise ValueError(f"{name}: a module must select actual behavior tests")
        for path in entry["sources"]:
            if path in owned_sources:
                raise ValueError(f"production file has multiple owners: {path}")
            owned_sources[path] = name
        for path in entry["tests"]:
            if path in owned_tests:
                raise ValueError(f"test file has multiple owners: {path}")
            owned_tests[path] = name
    references = set(owned_sources) | set(owned_tests)
    for name, entry in modules.items():
        for path in entry["boundary_tests"]:
            if path not in owned_tests:
                raise ValueError(f"{name}: boundary test has no owner: {path}")
            references.add(path)
    for path, consumers in support.items():
        source_path(path)
        if not path.startswith("test/") or path in owned_tests:
            raise ValueError(f"support entry is not a test fixture: {path}")
        _unique_paths(consumers, tests=True)
        if not consumers or any(consumer not in owned_tests for consumer in consumers):
            raise ValueError(f"support has missing or unowned consumers: {path}")
        references.add(path)
    if files is not None:
        missing = sorted(references - files)
        if missing:
            raise ValueError("stale ownership paths: " + ", ".join(missing))
        production = {path for path in files
                      if (path.endswith(".py") and path.startswith(("openmachine/", "integrations/")))
                      or (path.startswith("openmachine/skills/") and path.endswith((".md", ".yaml")))}
        tests = {path for path in files if path.startswith("test/")
                 and PurePosixPath(path).name.startswith("test_") and path.endswith(".py")}
        helpers = {path for path in files if path.startswith("test/") and path.endswith(".py")
                   and path not in tests and PurePosixPath(path).name != "__init__.py"}
        if production - owned_sources.keys():
            raise ValueError("production files without an explicit owner: " +
                             ", ".join(sorted(production - owned_sources.keys())))
        if tests - owned_tests.keys():
            raise ValueError("test files without an explicit owner: " +
                             ", ".join(sorted(tests - owned_tests.keys())))
        if helpers - support.keys():
            raise ValueError("support files without consumers: " + ", ".join(sorted(helpers - support.keys())))
        if root is not None:
            for path in references:
                resolved = (root / path).resolve()
                if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
                    raise ValueError(f"ownership path escapes the candidate or is not a file: {path}")
            check_fixture_consumers(root, files, tests, support)
    return value


def _old_manifest(root: Path, base_sha: str) -> dict | None:
    result = subprocess.run(["git", "-C", str(root), "show", f"{base_sha}:{MANIFEST}"],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        # Absence is supported only for the first architecture migration.
        if MANIFEST in git(root, "ls-tree", "-r", "--name-only", base_sha).splitlines():
            raise ValueError("could not read the baseline ownership manifest")
        return None
    return validate_manifest(json.loads(result.stdout))


def _repository_only(path: str) -> bool:
    # Keep retired docs classified so their deletion still selects repository checks.
    return (path in REPOSITORY_PATHS or
            path.startswith("spec/") or
            (path.endswith(".md") and path.startswith(("docs/", "test/"))) or
            (path.startswith(".github/ISSUE_TEMPLATE/") and path.endswith((".md", ".yml", ".yaml"))))


def _tests_for(entry: dict) -> set[str]:
    return set(entry["tests"]) | set(entry["boundary_tests"])


def changed_paths(root: Path, base_sha: str, head_sha: str, *, committed: bool) -> set[str]:
    # No rename detection: both old and new endpoints remain independently visible.
    paths = set(filter(None, git(root, "diff", "--name-only", "--no-renames", "-z", base_sha, head_sha).split("\0")))
    if not committed:
        paths.update(filter(None, git(root, "diff", "--cached", "--name-only", "--no-renames", "-z", "HEAD").split("\0")))
        paths.update(filter(None, git(root, "diff", "--name-only", "--no-renames", "-z").split("\0")))
        paths.update(filter(None, git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0")))
    if git(root, "ls-files", "-u", "-z"):
        raise ValueError("resolve unmerged paths before selecting tests")
    return paths


def make_plan(root: Path, base_ref: str | None = None, *, modules: list[str] | None = None,
              layer: str | None = None, full: bool = False, committed: bool = False) -> dict:
    if full and (layer or modules):
        raise ValueError("--full cannot be combined with a focused --module or --layer selection")
    if committed:
        require_clean(root)
    files = inventory(root)
    current = validate_manifest(json.loads((root / MANIFEST).read_text()), files, root)
    head_sha = resolve_commit(root, "HEAD")
    base_sha = resolve_commit(root, base_ref) if base_ref else None
    if base_sha:
        require_ancestor(root, base_sha, head_sha)
    if not full and not modules and not base_sha:
        raise ValueError("select --base, --module, or --full explicitly")
    old = _old_manifest(root, base_sha) if base_sha else current
    changes = changed_paths(root, base_sha, head_sha, committed=committed) if base_sha else set()
    all_tests = {path for entry in current["modules"].values() for path in entry["tests"]}
    reasons: dict[str, set[str]] = {}
    required_paths: dict[str, set[str]] = {}
    full_reasons = []

    def add(paths, reason):
        for path in paths:
            if path in all_tests:
                reasons.setdefault(path, set()).add(reason)

    add((path for path in all_tests if path.startswith("test/repository/")), "repository checks")
    if full:
        full_reasons.append("explicit full verification")
    if old is None:
        full_reasons.append("baseline predates test ownership; verify the complete migration")
    for path in sorted(changes & GLOBAL_PATHS):
        full_reasons.append(f"global verification or dependency change: {path}")
    if modules:
        for name in modules:
            if name not in current["modules"]:
                raise ValueError(f"unknown test module: {name}")
            add(_tests_for(current["modules"][name]), f"requested module: {name}")
    for path in sorted(changes):
        known = path == MANIFEST or path in GLOBAL_PATHS or _repository_only(path)
        for manifest in (current, old):
            if manifest is None:
                continue
            for name, entry in manifest["modules"].items():
                if path in entry["sources"]:
                    known = True
                    required_paths.setdefault(path, set()).update(_tests_for(entry) & all_tests)
                    add(_tests_for(entry), f"{path} -> {name}")
                    if name in current["modules"]:
                        required_paths[path].update(_tests_for(current["modules"][name]) & all_tests)
                        add(_tests_for(current["modules"][name]), f"{path} -> {name}")
                if path in entry["tests"]:
                    known = True
                    if path in all_tests:
                        add([path], f"changed test: {path}")
                    else:
                        required_paths.setdefault(path, set()).update(_tests_for(entry) & all_tests)
                        add(_tests_for(entry), f"removed test: {path}")
                        if name in current["modules"]:
                            required_paths[path].update(_tests_for(current["modules"][name]) & all_tests)
                            add(_tests_for(current["modules"][name]), f"removed test: {path}")
            if path in manifest["support"]:
                known = True
                add(manifest["support"][path], f"changed fixture: {path}")
        if path.startswith("test/") and PurePosixPath(path).name == "__init__.py":
            known = True
            prefix = str(PurePosixPath(path).parent) + "/"
            consumers = {test for test in all_tests if test.startswith(prefix)}
            for manifest in (current, old):
                if manifest is not None:
                    for helper, tests in manifest["support"].items():
                        if helper.startswith(prefix):
                            consumers.update(set(tests) & all_tests)
            # A fully retired test package has no surviving behavior to select.
            if consumers or any(name.startswith(prefix) for name in files):
                required_paths.setdefault(path, set()).update(consumers)
            add(consumers, f"test package: {path}")
        if not known:
            if old is None and path.startswith("test/") and path not in files:
                # Deleted tests from the pre-manifest layout are covered by this full migration.
                continue
            raise ValueError(f"changed path has no test or repository ownership: {path}")
    if MANIFEST in changes and old is not None:
        for name in set(old["modules"]) | set(current["modules"]):
            before, after = old["modules"].get(name), current["modules"].get(name)
            if before != after:
                if before:
                    add(_tests_for(before), f"ownership changed: {name}")
                if after:
                    add(_tests_for(after), f"ownership changed: {name}")
        for path in set(old["support"]) | set(current["support"]):
            if old["support"].get(path) != current["support"].get(path):
                add(old["support"].get(path, []), f"fixture ownership changed: {path}")
                add(current["support"].get(path, []), f"fixture ownership changed: {path}")
    for path, retained in required_paths.items():
        product_path = path.startswith(("openmachine/", "integrations/")) or (
            path.startswith("test/") and PurePosixPath(path).name.startswith("test_")
            and not path.startswith("test/repository/"))
        behavior = {test for test in retained if not test.startswith("test/repository/")} if product_path else retained
        if not behavior:
            raise ValueError(f"changed or removed path has no retained behavior coverage: {path}")
    if full_reasons:
        add(all_tests, "; ".join(full_reasons))
    if layer:
        reasons = {path: why for path, why in reasons.items() if path.split("/")[1] == layer}
    selected = sorted(reasons, key=lambda path: (LAYERS.index(path.split("/")[1]), path))
    product = [path for path in selected if not path.startswith("test/repository/")]
    changed_product = any(path.startswith(("openmachine/", "integrations/")) for path in changes)
    changed_tests = any(path.startswith("test/") and PurePosixPath(path).name.startswith("test_")
                        and path.endswith(".py") and not path.startswith("test/repository/")
                        for path in changes)
    needs_product = changed_product or changed_tests
    if not selected or (needs_product and not product and layer != "repository"):
        raise ValueError("selection contains no required behavior tests")
    if modules and not product and layer != "repository" and modules != ["repository"]:
        raise ValueError("requested module/layer contains no behavior tests")
    if committed and not changes and not full and not modules:
        raise ValueError("candidate range contains no changes; use --full for an explicit baseline verification")
    identity = {
        "head_sha": head_sha, "base_sha": base_sha,
        "scope": "focused" if layer or modules else ("full" if full_reasons else "affected"),
        "modules": sorted(modules or []), "layer": layer,
        "manifest_sha256": hashlib.sha256((root / MANIFEST).read_bytes()).hexdigest(),
        "candidate_sha256": candidate_digest(root, files),
        "tests": selected,
    }
    identity["selection_sha256"] = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {**identity, "base_ref": base_ref, "changed_paths": sorted(changes), "full_reasons": full_reasons,
            "reasons": {path: sorted(reasons[path]) for path in selected},
            "runtime_required": bool(product),
            "environment": ("source" if layer == "unit" else "wheel") if product else "none",
            "dirty": bool(git(root, "status", "--porcelain=v1", "--untracked-files=all"))}


def print_plan(plan: dict) -> None:
    print(f"Plan: {plan['scope']} / {plan['environment']}; HEAD={plan['head_sha']}; base={plan['base_sha']}", flush=True)
    print("Candidate: current worktree (dirty)" if plan["dirty"] else "Candidate: clean committed worktree", flush=True)
    for path in plan["changed_paths"]:
        print(f"Changed: {path}", flush=True)
    for path in plan["tests"]:
        print(f"  {path}: {'; '.join(plan['reasons'][path])}", flush=True)
    if not plan["runtime_required"]:
        print("Runtime tests not required by this verified repository-only selection.", flush=True)


def run_command(argv: list[str], *, cwd: Path, stage: str, env: dict | None = None) -> float:
    started = time.monotonic()
    print(f"START: {stage}", flush=True)
    try:
        subprocess.run(argv, cwd=cwd, env=env, check=True, timeout=600)
    finally:
        elapsed = time.monotonic() - started
        print(f"END: {stage} ({elapsed:.2f}s)", flush=True)
    return elapsed


class TimedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.durations = []
        self.started = 0.0

    def startTest(self, test):
        self.started = time.monotonic()
        super().startTest(test)

    def stopTest(self, test):
        self.durations.append((time.monotonic() - self.started, test.id()))
        super().stopTest(test)


def run_files(root: Path, paths: list[str], *, installed: bool, source: bool = False) -> dict:
    if source:
        sys.path.insert(0, str(root))
    else:
        # Keep the checkout out of product imports when exercising the installed wheel.
        sys.path[:] = [path for path in sys.path if path and Path(path).resolve() != root.resolve()]
    # Test modules always live under the candidate's test tree. Build the package
    # explicitly so focused files do not depend on parent __init__.py scaffolding.
    package = types.ModuleType("test")
    package.__path__ = [str(root / "test")]
    sys.modules["test"] = package
    if installed or source:
        product = importlib.import_module("openmachine")
        origin = Path(product.__file__).resolve()
        if origin.is_relative_to((root / "openmachine").resolve()) != source:
            raise ValueError(f"unexpected candidate import location: {origin}")
        print(f"Candidate: {origin}", flush=True)
    suites = {}
    loader = unittest.TestLoader()
    for path in paths:
        test_path(path)
        suite = loader.loadTestsFromName(path[:-3].replace("/", "."))
        if loader.errors or not suite.countTestCases():
            raise ValueError(f"test discovery failed or selected zero tests: {path}; {loader.errors}")
        suites.setdefault(path.split("/")[1], unittest.TestSuite()).addTest(suite)
    results = []
    for layer, suite in suites.items():
        started = time.monotonic()
        print(f"START: {layer} ({suite.countTestCases()} tests)", flush=True)
        result = unittest.TextTestRunner(verbosity=1, resultclass=TimedResult).run(suite)
        elapsed = time.monotonic() - started
        print(f"END: {layer} ({elapsed:.2f}s, {result.testsRun} tests, {len(result.skipped)} skipped)", flush=True)
        for seconds, name in sorted(result.durations, reverse=True)[:10]:
            print(f"  {seconds:.3f}s {name}", flush=True)
        results.append({"layer": layer, "tests": result.testsRun, "skipped": len(result.skipped), "seconds": elapsed})
        if not result.wasSuccessful() or result.testsRun == len(result.skipped):
            raise ValueError(f"{layer} did not execute a successful nonempty behavior suite")
    return {"layers": results}


def skill_sources(root: Path) -> dict[str, bytes]:
    package = root / "openmachine/skills"
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in package.rglob("*") if path.is_file() and path.suffix in {".py", ".yaml", ".md"}}


def wheel_skills(wheel: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(wheel) as archive:
        paths = [name for name in archive.namelist() if name.startswith("openmachine/skills/")
                 and Path(name).suffix in {".py", ".yaml", ".md"}]
        if len(paths) != len(set(paths)) or any(name.startswith("openmachine/_skills/") for name in archive.namelist()):
            raise ValueError("wheel has duplicate or retired Skill resources")
        return {name: archive.read(name) for name in paths}


def verify_skill_payloads(root: Path, *wheels: Path) -> None:
    expected = skill_sources(root)
    if not {"openmachine/skills/__init__.py", "openmachine/skills/bindings.yaml",
            "openmachine/skills/overview/SKILL.md"} <= expected.keys():
        raise ValueError("canonical Skill package inputs are incomplete")
    for wheel in wheels:
        actual = wheel_skills(wheel)
        if actual != expected:
            missing, extra = sorted(expected.keys() - actual.keys()), sorted(actual.keys() - expected.keys())
            changed = sorted(name for name in expected.keys() & actual.keys() if expected[name] != actual[name])
            raise ValueError(f"Skill package differs from source in {wheel.name}: missing={missing}, extra={extra}, changed={changed}")


def execute_plan(root: Path, plan: dict) -> dict:
    print_plan(plan)
    stages = {}
    checks = (["git", "diff", "--check"], ["git", "diff", "--cached", "--check"])
    for index, command in enumerate(checks):
        stages[f"diff check {index}"] = run_command(command, cwd=root, stage="check uncommitted whitespace")
    if plan["base_sha"]:
        stages["candidate diff check"] = run_command(
            ["git", "diff", "--check", plan["base_sha"], plan["head_sha"]], cwd=root,
            stage="check committed candidate whitespace")
    with tempfile.TemporaryDirectory(prefix="openmachine-tests-") as directory:
        temporary = Path(directory)
        repository = [path for path in plan["tests"] if path.startswith("test/repository/")]
        product = [path for path in plan["tests"] if path not in repository]

        def execute(python: Path | str, paths: list[str], mode: str):
            request = temporary / f"{mode}-tests.json"
            report = temporary / f"{mode}-results.json"
            request.write_text(json.dumps({"paths": paths, "mode": mode, "report": str(report)}))
            stages[mode] = run_command([str(python), "-I", str(root / "scripts/run_tests.py"),
                                       "--execute", str(request)], cwd=temporary, stage=f"{mode} tests")
            return json.loads(report.read_text())

        results = []
        if repository:
            results.extend(execute(sys.executable, repository, "repository")["layers"])
        stages["repository checks"] = run_command([sys.executable, str(root / "scripts/check_repository.py"), "--mode", "source"],
                                                  cwd=root, stage="repository checks")
        if product and plan["environment"] == "source":
            results.extend(execute(sys.executable, product, "source")["layers"])
        elif product:
            environment = temporary / "venv"
            stages["environment"] = run_command([sys.executable, "-m", "venv", str(environment)],
                                                cwd=root, stage="create isolated environment")
            python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            stages["dependencies"] = run_command([str(python), "-m", "pip", "install", "-r", str(root / "requirements.lock")],
                                                 cwd=root, stage="install locked dependencies")
            wheels = temporary / "wheels"
            wheels.mkdir()
            stages["wheel build"] = run_command([str(python), "-m", "pip", "wheel", "--no-deps",
                                                 "--no-build-isolation", ".", "--wheel-dir", str(wheels)],
                                                cwd=root, stage="build candidate wheel")
            artifacts = list(wheels.glob("openmachine-*.whl"))
            if len(artifacts) != 1:
                raise ValueError("expected exactly one newly built candidate wheel")
            sdists = temporary / "sdists"
            sdists.mkdir()
            stages["sdist build"] = run_command([str(python), "-c",
                "from hatchling.build import build_sdist; import sys; build_sdist(sys.argv[1])", str(sdists)],
                cwd=root, stage="build candidate sdist")
            archives = list(sdists.glob("openmachine-*.tar.gz"))
            if len(archives) != 1:
                raise ValueError("expected exactly one newly built candidate sdist")
            unpacked = temporary / "sdist-source"
            with tarfile.open(archives[0]) as archive:
                archive.extractall(unpacked, filter="data")
            source_roots = list(unpacked.glob("*/pyproject.toml"))
            if len(source_roots) != 1:
                raise ValueError("expected one candidate source root in the sdist")
            derived = temporary / "sdist-wheels"
            derived.mkdir()
            stages["sdist wheel build"] = run_command([str(python), "-m", "pip", "wheel", "--no-deps",
                "--no-build-isolation", ".", "--wheel-dir", str(derived)],
                cwd=source_roots[0].parent, stage="build wheel from candidate sdist")
            derived_artifacts = list(derived.glob("openmachine-*.whl"))
            if len(derived_artifacts) != 1:
                raise ValueError("expected exactly one newly built sdist-derived wheel")
            verify_skill_payloads(root, artifacts[0], derived_artifacts[0])
            stages["wheel install"] = run_command([str(python), "-m", "pip", "install", "--no-deps",
                                                   "--force-reinstall", str(artifacts[0])],
                                                  cwd=root, stage="install candidate wheel")
            results.extend(execute(python, product, "installed")["layers"])
    if candidate_digest(root, inventory(root)) != plan["candidate_sha256"]:
        raise ValueError("candidate files changed during verification")
    if resolve_commit(root, "HEAD") != plan["head_sha"]:
        raise ValueError("HEAD changed during verification")
    if plan["base_ref"] and resolve_commit(root, plan["base_ref"]) != plan["base_sha"]:
        raise ValueError("the requested base changed during verification")
    return {"status": "passed", "layers": results, "stages": stages, "plan": plan}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="explicit local ancestor ref; includes committed and dirty changes")
    parser.add_argument("--module", action="append", dest="modules", help="run this module and its declared boundaries")
    parser.add_argument("--layer", choices=LAYERS, help="focused evidence only; unit uses current checkout source")
    parser.add_argument("--full", action="store_true", help="run every owned test")
    parser.add_argument("--plan", action="store_true", help="print selection without installing or running tests")
    parser.add_argument("--execute", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.execute:
            request = json.loads(Path(args.execute).read_text())
            result = run_files(ROOT, request["paths"], installed=request["mode"] == "installed",
                               source=request["mode"] == "source")
            Path(request["report"]).write_text(json.dumps(result))
            return 0
        plan = make_plan(ROOT, args.base, modules=args.modules, layer=args.layer, full=args.full)
        if args.plan:
            print_plan(plan)
        else:
            execute_plan(ROOT, plan)
    except (OSError, ValueError, KeyError, SyntaxError, subprocess.SubprocessError) as error:
        print(f"FAIL: tests: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

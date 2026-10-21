#!/usr/bin/env python3
"""Read bounded evidence, validate authored motion plans, and prepare/render them.

Python uses only its standard library. It never executes ingested source files.
Text hashes use UTF-8 after CRLF and CR are changed to LF; nothing else is stripped
or normalized. Validation checks evidence anchors, not semantic entailment.
"""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile


SKILL_ROOT = Path(__file__).resolve().parents[1]
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 1024 * 1024
MAX_SPEC_BYTES = 2 * 1024 * 1024
HASH_POLICY = "sha256-utf8-lf-v1"
ID_RE = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
KINDS = {"hook", "pipeline", "routing", "parallel", "feedback", "compare", "outcome"}
README_NAMES = ("README.md", "README.rst", "README.txt", "README", "readme.md", "readme.rst", "readme.txt", "readme")


class MotionError(ValueError):
    """A user-facing input, validation, or execution error."""


def fail(message):
    raise MotionError(message)


def normalize_text(raw, label="input"):
    """Return canonical text and its digest; preserve a BOM and all whitespace."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        fail(f"{label}: expected UTF-8 text ({exc.reason}).")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if any(ord(char) < 32 and char not in "\n\t" for char in text):
        fail(f"{label}: binary or unsupported control characters in text.")
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_bounded(path, limit, label):
    path = Path(path)
    if not path.is_file():
        fail(f"{label}: not a regular file: {path}")
    with path.open("rb") as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        fail(f"{label}: exceeds the {limit}-byte limit.")
    return raw


def load_json(path):
    if str(path) == "-":
        raw = sys.stdin.buffer.read(MAX_SPEC_BYTES + 1)
        if len(raw) > MAX_SPEC_BYTES:
            fail(f"JSON standard input: exceeds the {MAX_SPEC_BYTES}-byte limit.")
    else:
        raw = read_bounded(path, MAX_SPEC_BYTES, "JSON input")
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                fail(f"JSON input: duplicate object key {key!r}.")
            result[key] = value
        return result
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=lambda value: fail(f"JSON input: non-finite value {value} is not allowed."))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail(f"Invalid UTF-8 JSON in {path}: {exc}")


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def write_new_json(path, value):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(json_text(value))
    except FileExistsError:
        fail(f"Output already exists; choose a new path: {path}")


def _object(value, required, optional, where):
    if not isinstance(value, dict):
        fail(f"{where}: expected an object.")
    missing = set(required) - value.keys()
    unknown = value.keys() - set(required) - set(optional)
    if missing:
        fail(f"{where}: missing keys: {', '.join(sorted(missing))}.")
    if unknown:
        fail(f"{where}: unknown keys: {', '.join(sorted(unknown))}.")


def _text(value, where, maximum=None, empty=False, multiline=False):
    if not isinstance(value, str) or (not empty and not value.strip()):
        fail(f"{where}: expected {'a' if empty else 'a non-empty'} string.")
    if maximum is not None and len(value) > maximum:
        fail(f"{where}: exceeds {maximum} characters.")
    allowed = "\n\t" if multiline else ""
    if any(ord(char) < 32 and char not in allowed for char in value):
        fail(f"{where}: unsupported control character or line break.")


def _list(value, where, minimum=0, maximum=100):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        fail(f"{where}: expected a list with {minimum} to {maximum} items.")


def _id(value, where):
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        fail(f"{where}: ID must be a lowercase slug beginning with a letter, using only a-z, 0-9, and - (maximum 64 characters).")


def _records(records, where, minimum=0, maximum=100):
    _list(records, where, minimum, maximum)
    result = {}
    for index, record in enumerate(records):
        if not isinstance(record, dict) or "id" not in record:
            fail(f"{where}[{index}]: expected an object with an id.")
        _id(record["id"], f"{where}[{index}].id")
        if record["id"] in result:
            fail(f"{where}: duplicate ID {record['id']}.")
        result[record["id"]] = record
    return result


def _references(values, lookup, where, minimum=0):
    _list(values, where, minimum)
    seen = set()
    for value in values:
        _id(value, where)
        if value in seen:
            fail(f"{where}: duplicate ID {value}.")
        if value not in lookup:
            fail(f"{where}: unknown ID {value}.")
        seen.add(value)


def validate_spec(spec):
    """Validate structure and exact evidence anchors; return a timing summary.

    A successful result is not semantic, visual, accessibility, or brand approval.
    Source quotes can exist while failing to support the associated claim.
    """
    _object(spec, {"schema_version", "title", "audience", "goal", "format", "fps", "theme", "sources", "claims", "entities", "scenes"}, {"notes", "language"}, "spec")
    if type(spec["schema_version"]) is not int or spec["schema_version"] != 1:
        fail("spec.schema_version: expected integer 1.")
    for key, limit in (("title", 70), ("audience", 240), ("goal", 500)):
        _text(spec[key], f"spec.{key}", limit)
    if spec["format"] not in ("landscape", "portrait"):
        fail("spec.format: expected landscape or portrait.")
    if type(spec["fps"]) is not int or spec["fps"] != 30:
        fail("spec.fps: this renderer requires integer 30.")
    if spec["theme"] not in ("paper", "midnight"):
        fail("spec.theme: expected paper or midnight.")
    if "language" in spec and spec["language"] not in ("en", "zh"):
        fail("spec.language: expected en or zh (defaults to en).")
    if "notes" in spec:
        _list(spec["notes"], "spec.notes", maximum=20)
        for index, note in enumerate(spec["notes"]):
            _text(note, f"spec.notes[{index}]", 1000, multiline=True)

    sources = _records(spec["sources"], "sources", 1, 50)
    total_bytes = 0
    for source_id, source in sources.items():
        where = f"source {source_id}"
        _object(source, {"id", "kind", "locator", "sha256", "text"}, set(), where)
        _text(source["kind"], f"{where}.kind", 80)
        _text(source["locator"], f"{where}.locator", 4096)
        _text(source["text"], f"{where}.text", multiline=True)
        raw = source["text"].encode("utf-8")
        total_bytes += len(raw)
        if len(raw) > MAX_FILE_BYTES or total_bytes > MAX_TOTAL_BYTES:
            fail(f"{where}: source text exceeds per-file or total evidence byte limits.")
        if "\r" in source["text"]:
            fail(f"{where}: text must use LF newlines; use ingest to normalize it.")
        expected = hashlib.sha256(raw).hexdigest()
        if not isinstance(source["sha256"], str) or source["sha256"] != expected:
            fail(f"{where}: sha256 mismatch; hash normalized UTF-8 source text.")

    claims = _records(spec["claims"], "claims", 1, 50)
    for claim_id, claim in claims.items():
        where = f"claim {claim_id}"
        _object(claim, {"id", "text", "evidence"}, set(), where)
        _text(claim["text"], f"{where}.text", 1000, multiline=True)
        _list(claim["evidence"], f"{where}.evidence", 1, 20)
        for index, anchor in enumerate(claim["evidence"]):
            anchor_where = f"{where}.evidence[{index}]"
            _object(anchor, {"source_id", "quote"}, set(), anchor_where)
            _id(anchor["source_id"], f"{anchor_where}.source_id")
            if anchor["source_id"] not in sources:
                fail(f"{anchor_where}: unknown source ID {anchor['source_id']}.")
            _text(anchor["quote"], f"{anchor_where}.quote", multiline=True)
            if anchor["quote"] not in sources[anchor["source_id"]]["text"]:
                fail(f"{anchor_where}: exact quote is absent from source {anchor['source_id']}.")

    entities = _records(spec["entities"], "entities", maximum=30)
    for entity_id, entity in entities.items():
        where = f"entity {entity_id}"
        _object(entity, {"id", "label"}, {"detail"}, where)
        _text(entity["label"], f"{where}.label", 30)
        if "detail" in entity:
            _text(entity["detail"], f"{where}.detail", 90)

    scenes = _records(spec["scenes"], "scenes", 3, 8)
    total_frames = 0
    timeline = []
    for scene_id, scene in scenes.items():
        where = f"scene {scene_id}"
        _object(scene, {"id", "kind", "duration", "title", "caption", "narration", "claim_ids", "entities"}, {"active_branch"}, where)
        kind = scene["kind"]
        if not isinstance(kind, str) or kind not in KINDS:
            fail(f"{where}.kind: unsupported scene kind.")
        _text(scene["title"], f"{where}.title", 70)
        _text(scene["caption"], f"{where}.caption", 150, multiline=True)
        _text(scene["narration"], f"{where}.narration", 500, empty=True, multiline=True)
        duration = scene["duration"]
        if type(duration) not in (int, float) or not 3 <= duration <= 18 or not math.isfinite(duration):
            fail(f"{where}.duration: expected a finite number from 3 to 18 seconds.")
        frames = round(duration * 30)
        if abs(duration * 30 - frames) > 0.000001:
            fail(f"{where}.duration: must land on an exact 30 fps frame boundary.")
        _references(scene["claim_ids"], claims, f"{where}.claim_ids", 0 if kind in {"hook", "outcome"} else 1)
        _references(scene["entities"], entities, f"{where}.entities")
        count = len(scene["entities"])
        valid_count = {"hook": count == 0, "outcome": count == 0, "pipeline": 2 <= count <= 5, "routing": count == 4, "parallel": 4 <= count <= 5, "feedback": count == 3, "compare": count == 2}[kind]
        if not valid_count:
            fail(f"{where}: invalid entity count {count} for {kind}; see the scene contract.")
        if kind == "routing":
            if "active_branch" not in scene or scene["active_branch"] not in scene["entities"][2:]:
                fail(f"{where}: active_branch must select one of the last two routing entities [input, router, A, B].")
        elif "active_branch" in scene:
            fail(f"{where}: active_branch is only valid for routing scenes.")
        if kind == "compare" and any(not entities[item].get("detail", "").strip() for item in scene["entities"]):
            fail(f"{where}: both comparison entities require detail for the matched comparison.")
        timeline.append({"scene_id": scene_id, "start_frame": total_frames, "end_frame": total_frames + frames, "frames": frames})
        total_frames += frames
    if not 15 * 30 <= total_frames <= 90 * 30:
        fail("scenes: total duration must be from 15 to 90 seconds.")
    return {"schema_version": 1, "scene_count": len(scenes), "duration_seconds": total_frames / 30, "total_frames": total_frames, "timeline": timeline, "structural_validation": "passed", "semantic_review": "required", "visual_review": "required"}


def _relative_path(value):
    if not isinstance(value, str) or not value or "\\" in value or any(ord(char) < 32 for char in value):
        fail("Repository includes must be non-empty, slash-separated relative file paths.")
    parts = value.split("/")
    if PurePosixPath(value).is_absolute() or any(part in {"", ".", "..", ".git"} for part in parts) or ":" in value:
        fail(f"Unsafe repository include path: {value!r}")
    return value


def _git(repo, *arguments):
    # An ambient GIT_DIR/WORK_TREE/OBJECT_DIRECTORY must not redirect this read.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    # cat-file can otherwise lazy-fetch a missing promisor object and execute
    # repository-configured SSH commands or remote helpers. The empty transport
    # allowlist also protects Git versions that lack GIT_NO_LAZY_FETCH support;
    # it overrides per-protocol repository settings, including allow=always.
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_REPLACE_OBJECTS": "1", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": "", "GIT_PROTOCOL_FROM_USER": "0"})
    try:
        result = subprocess.run(["git", "--no-replace-objects", "--literal-pathspecs", "-c", "protocol.allow=never", "-C", str(repo), *arguments], stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, env=env, timeout=30, check=False)
    except FileNotFoundError:
        fail("Git is required to ingest a repository.")
    except subprocess.TimeoutExpired:
        fail("Git evidence read timed out.")
    if result.returncode:
        message = result.stderr.decode("utf-8", errors="replace").strip()[:500]
        fail(f"Git evidence read failed: {message}")
    return result.stdout


def _tree_entry(repo, commit, path):
    raw = _git(repo, "ls-tree", "-z", commit, "--", path)
    records = [entry for entry in raw.split(b"\0") if entry]
    if len(records) != 1:
        fail(f"Repository path is not one tracked file at {commit}: {path}. Untracked files are never ingested.")
    metadata, actual = records[0].split(b"\t", 1)
    mode, kind, oid = metadata.decode("ascii").split()
    if actual.decode("utf-8") != path or mode not in {"100644", "100755"} or kind != "blob":
        fail(f"Repository path must be a regular committed file, not a symlink, directory, or submodule: {path}")
    return oid


def ingest_article(path):
    path = Path(path).expanduser().resolve()
    raw = read_bounded(path, MAX_FILE_BYTES, "Article")
    text, digest = normalize_text(raw, "Article")
    if not text.strip():
        fail("Article is empty.")
    return {"schema_version": 1, "sources": [{"id": "source-001", "kind": "article", "locator": path.as_uri(), "sha256": digest, "text": text}], "provenance": {"hash_policy": HASH_POLICY, "input_bytes": len(raw), "input_policy": "UTF-8 text; only newline sequences are normalized."}}


def ingest_repo(path, includes=None, ref="HEAD"):
    repo = Path(path).expanduser().resolve()
    if not repo.is_dir():
        fail(f"Repository directory does not exist: {repo}")
    selected = [_relative_path(item) for item in (includes or [])]
    if not isinstance(ref, str) or not ref or ref.startswith("-") or any(ord(char) < 32 for char in ref):
        fail("Repository ref must be a non-empty revision, not an option.")
    root_output = _git(repo, "rev-parse", "--show-toplevel").decode("utf-8")
    root = Path(root_output[:-1] if root_output.endswith("\n") else root_output).resolve()
    commit = _git(root, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").decode("ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        fail("Git did not resolve the revision to a full commit ID.")
    # Query only named candidates; never recursively walk a repository or its worktree.
    candidates = _git(root, "ls-tree", "-z", commit, "--", *README_NAMES)
    found = {entry.split(b"\t", 1)[1].decode("utf-8") for entry in candidates.split(b"\0") if entry}
    readme = next((name for name in README_NAMES if name in found), None)
    selected = list(dict.fromkeys(([readme] if readme else []) + selected))
    if not selected:
        fail("No root README found at the selected commit. Select text files with --include.")
    if len(selected) > 50:
        fail("At most 50 repository files may be selected.")
    sources = []
    total = 0
    for index, relative in enumerate(selected, 1):
        oid = _tree_entry(root, commit, relative)
        size = int(_git(root, "cat-file", "-s", oid).decode("ascii").strip())
        total += size
        if size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            fail(f"Repository evidence exceeds the {MAX_FILE_BYTES}-byte per-file or {MAX_TOTAL_BYTES}-byte total limit: {relative}")
        raw = _git(root, "cat-file", "blob", oid)
        if len(raw) != size:
            fail(f"Repository blob changed unexpectedly: {relative}")
        text, digest = normalize_text(raw, relative)
        if not text.strip():
            fail(f"Selected repository file is empty: {relative}")
        sources.append({"id": f"source-{index:03d}", "kind": "repository_file", "locator": f"git+{root.as_uri()}@{commit}:{relative}", "sha256": digest, "text": text})
    return {"schema_version": 1, "sources": sources, "provenance": {"hash_policy": HASH_POLICY, "repository": root.as_uri(), "requested_ref": ref, "commit": commit, "selected_paths": selected, "input_bytes": total, "input_policy": "Committed blobs only. Working-tree modifications and untracked contents are not read. No source code, hooks, filters, or installers are executed."}}


def _catalog(references=None):
    root = Path(references) if references else SKILL_ROOT / "references"
    items = []
    for filename, key, category in (("source-index.json", "sources", "source"), ("motion-cases.json", "cases", "case")):
        data = load_json(root / filename)
        if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get(key), list):
            fail(f"Invalid reference catalog: {filename}")
        for record in data[key]:
            if not isinstance(record, dict) or not isinstance(record.get("id"), str):
                fail(f"Catalog record lacks an ID: {filename}")
            items.append((category, record))
    if len({record["id"] for _, record in items}) != len(items):
        fail("Reference catalogs contain duplicate IDs.")
    return items


def search_catalog(query, kind="all", limit=8, references=None):
    terms = list(dict.fromkeys(re.findall(r"[^\W_]+", query.casefold())))
    if not terms:
        fail("Search query must not be empty.")
    if kind not in {"all", "source", "case"} or not 1 <= limit <= 100:
        fail("Search requires kind all/source/case and a limit from 1 to 100.")
    results = []
    for category, record in _catalog(references):
        if kind != "all" and category != kind:
            continue
        haystack = json.dumps(record, ensure_ascii=False).casefold()
        matched = [term for term in terms if term in haystack]
        if not matched:
            continue
        headline = " ".join(str(record.get(key, "")) for key in ("title", "viewer_problem", "topics"))
        # Reward shared intent first; a useful partial match beats an empty result.
        score = 100 * len(matched) + sum(min(haystack.count(term), 8) + 8 * headline.casefold().count(term) for term in matched)
        results.append({"category": category, "score": score, "matched_terms": matched, **record})
    return sorted(results, key=lambda item: (-item["score"], item["id"]))[:limit]


def show_record(record_id, references=None):
    for category, record in _catalog(references):
        if record["id"] == record_id:
            return {"category": category, **record}
    fail(f"Unknown reference ID: {record_id}")


def _timestamp(frame):
    milliseconds = round(frame * 1000 / 30)
    seconds, milliseconds = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def captions_srt(spec, summary=None):
    summary = summary or validate_spec(spec)
    lines = []
    cue = 0
    for scene, timing in zip(spec["scenes"], summary["timeline"]):
        if scene["caption"].strip():
            cue += 1
            caption = "\n".join(line for line in scene["caption"].splitlines() if line.strip())
            lines.extend([str(cue), f"{_timestamp(timing['start_frame'])} --> {_timestamp(timing['end_frame'])}", caption, ""])
    return "\n".join(lines) + ("\n" if lines else "")


def _fence(text):
    longest = max((len(match.group()) for match in re.finditer(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text}\n{fence}"


def evidence_manifest(spec, summary):
    sources = {item["id"]: item for item in spec["sources"]}
    claims = []
    for claim in spec["claims"]:
        anchors = []
        for anchor in claim["evidence"]:
            start = sources[anchor["source_id"]]["text"].find(anchor["quote"])
            anchors.append({**anchor, "quote_start_char": start, "quote_end_char": start + len(anchor["quote"])})
        claims.append({**claim, "evidence": anchors})
    return {"schema_version": 1, "hash_policy": HASH_POLICY, "offset_unit": "Unicode code points; first exact occurrence", "validation": {"structural": "passed", "semantic": "requires_review", "visual": "requires_review", "audio": "not_generated"}, "timing": summary, "sources": [{key: source[key] for key in ("id", "kind", "locator", "sha256")} for source in spec["sources"]], "claims": claims, "scene_claims": [{"scene_id": scene["id"], "claim_ids": scene["claim_ids"]} for scene in spec["scenes"]]}


def prepare(spec, output):
    summary = validate_spec(spec)
    output = Path(output).expanduser().absolute()
    if output.exists() or output.is_symlink():
        fail(f"Output already exists; choose a new directory: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        output.mkdir()
    except FileExistsError:
        fail(f"Output already exists; choose a new directory: {output}")
    try:
        storyboard = ["# Storyboard", "", _fence(spec["title"]), "", f"{summary['duration_seconds']:g} seconds / 30 fps / {spec['format']} / {spec['theme']}", "", "## Editorial goal", "", _fence(f"Audience: {spec['audience']}\nGoal: {spec['goal']}"), ""]
        narration = []
        for index, (scene, timing) in enumerate(zip(spec["scenes"], summary["timeline"]), 1):
            span = f"{_timestamp(timing['start_frame'])} --> {_timestamp(timing['end_frame'])}"
            storyboard.extend([f"## {index}. {scene['id']} / {scene['kind']}", "", span, "", _fence(f"Title: {scene['title']}\nCaption: {scene['caption']}\nNarration: {scene['narration']}"), "", "Entity order: " + (", ".join(scene["entities"]) or "none"), "Claim IDs: " + (", ".join(scene["claim_ids"]) or "none"), ""])
            if "active_branch" in scene:
                storyboard.extend(["Selected branch: " + scene["active_branch"], ""])
            narration.extend([f"[{scene['id']}] {span}", scene["narration"], ""])
        review = ["# Review required", "", "Structural validation passed. Exact quotes and hashes are mechanical anchors; they do not establish that a claim follows from its source.", "", "No semantic review, visual review, viewing test, or publication approval has been performed by this command.", "", "- [ ] Each claim is supported in context, with limitations retained.", "- [ ] Arrows, copies, routes, parallel work, feedback, and completion states express the actual mechanism.", "- [ ] Conceptual timing is distinguishable from measured performance or progress.", "- [ ] The same object keeps its identity; comparisons use matched conditions.", "- [ ] All frames, labels, captions, aspect-ratio crops, and holds have been inspected.", "- [ ] The clip works muted; narration is a script, not generated audio.", "- [ ] Assets, screenshots, branding, and publication rights fit the intended use.", "", "## Claims and exact anchors", ""]
        for claim in spec["claims"]:
            review.extend(["### " + claim["id"], "", _fence(claim["text"]), ""])
            for anchor in claim["evidence"]:
                review.extend(["Source: " + anchor["source_id"], "", _fence(anchor["quote"]), ""])
        if spec.get("notes"):
            review.extend(["## Author notes", "", _fence("\n".join(spec["notes"]))])
        files = {"spec.json": json_text(spec), "storyboard.md": "\n".join(storyboard) + "\n", "captions.srt": captions_srt(spec, summary), "narration.txt": "\n".join(narration) + "\n", "evidence-manifest.json": json_text(evidence_manifest(spec, summary)), "review.md": "\n".join(review) + "\n"}
        for name, content in files.items():
            (output / name).write_text(content, encoding="utf-8", newline="\n")
    except Exception:
        shutil.rmtree(output)
        raise
    return output


def render(spec, output, node="node", browser_executable=None, concurrency=2, stills_only=False):
    validate_spec(spec)
    if type(concurrency) is not int or not 1 <= concurrency <= 8:
        fail("Render concurrency must be an integer from 1 to 8.")
    output = Path(output).expanduser().absolute()
    if output.exists() or output.is_symlink():
        fail(f"Output already exists; choose a new directory: {output}")
    renderer = SKILL_ROOT / "assets" / "renderer" / "render.mjs"
    if not renderer.is_file():
        fail(f"Renderer not found: {renderer}")
    executable = shutil.which(node)
    if not executable:
        fail("Node.js was not found. See renderer setup in references/usage.md.")
    # Give Node the exact validated snapshot, not a spec file that could change.
    with tempfile.TemporaryDirectory(prefix="project-to-motion-") as temporary:
        snapshot = Path(temporary) / "spec.json"
        snapshot.write_text(json_text(spec), encoding="utf-8")
        command = [executable, str(renderer), "--spec", str(snapshot), "--output", str(output), "--concurrency", str(concurrency), "--python-executable", sys.executable]
        if browser_executable:
            command.extend(["--browser-executable", str(Path(browser_executable).expanduser().absolute())])
        if stills_only:
            command.append("--stills-only")
        result = subprocess.run(command, stdin=subprocess.DEVNULL, check=False)
    if result.returncode:
        fail(f"Renderer exited with status {result.returncode}. See its diagnostic output.")
    required = ["motion.json", "storyboard.html", "render-manifest.json", "captions.srt", "narration.txt"]
    if not stills_only:
        required.append("video.mp4")
    missing = [name for name in required if not (output / name).is_file()]
    if missing:
        fail("Renderer reported success without required outputs: " + ", ".join(missing))
    if len(list((output / "storyboard").glob("*.png"))) != 3 * len(spec["scenes"]):
        fail("Renderer reported success without three storyboard frames per scene.")
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    search = sub.add_parser("search", help="Search the compact source and case catalogs.")
    search.add_argument("query")
    search.add_argument("--kind", choices=("all", "source", "case"), default="all")
    search.add_argument("--limit", type=int, default=8)
    search.add_argument("--json", action="store_true")
    show = sub.add_parser("show", help="Show one source or case by stable ID.")
    show.add_argument("id")
    show.add_argument("--json", action="store_true")
    ingest = sub.add_parser("ingest", help="Read article text or explicitly selected committed repository blobs.")
    origin = ingest.add_mutually_exclusive_group(required=True)
    origin.add_argument("--article")
    origin.add_argument("--repo")
    ingest.add_argument("--include", action="append", default=[], metavar="RELFILE")
    ingest.add_argument("--ref", default="HEAD")
    ingest.add_argument("--output", required=True)
    validate = sub.add_parser("validate", help="Check structure, timing, source hashes, and exact quotes.")
    validate.add_argument("spec", help="JSON spec path, or - to read bounded JSON from standard input.")
    for name in ("prepare", "render"):
        command = sub.add_parser(name, help="Export a review packet." if name == "prepare" else "Validate then invoke the local renderer.")
        command.add_argument("spec")
        command.add_argument("--output", required=True)
        if name == "render":
            command.add_argument("--node", default="node")
            command.add_argument("--browser-executable", metavar="PATH")
            command.add_argument("--concurrency", type=int, choices=range(1, 9), default=2)
            command.add_argument("--stills-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "search":
            results = search_catalog(args.query, args.kind, args.limit)
            if args.json:
                print(json_text(results), end="")
            else:
                for result in results:
                    print(f"{result['id']} [{result['category']}] {result.get('title', result.get('viewer_problem', ''))}")
                    print("  " + result.get("url", "source: " + result.get("source_id", "")))
                if not results:
                    print("No matching references. Try a shorter query.")
        elif args.command == "show":
            print(json_text(show_record(args.id)), end="")
        elif args.command == "ingest":
            if args.article and (args.include or args.ref != "HEAD"):
                fail("--include and --ref apply only to --repo.")
            bundle = ingest_article(args.article) if args.article else ingest_repo(args.repo, args.include, args.ref)
            write_new_json(args.output, bundle)
            print(f"Wrote {len(bundle['sources'])} evidence source(s) to {args.output}. Author the editorial plan before rendering.")
            if args.repo:
                print("Pinned commit: " + bundle["provenance"]["commit"])
        elif args.command == "validate":
            print(json_text(validate_spec(load_json(args.spec))), end="")
        elif args.command == "prepare":
            print(f"Review packet: {prepare(load_json(args.spec), args.output)}")
        elif args.command == "render":
            output = render(load_json(args.spec), args.output, args.node, args.browser_executable, args.concurrency, args.stills_only)
            print(f"Rendered {'storyboard' if args.stills_only else 'video'}: {output / ('storyboard.html' if args.stills_only else 'video.mp4')}")
    except (MotionError, OSError, UnicodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

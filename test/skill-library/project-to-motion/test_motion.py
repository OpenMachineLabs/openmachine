"""Behavioral checks for evidence handling and the authored-plan contract."""

import copy
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "skill-library" / "project-to-motion" / "scripts" / "motion.py"
MODULE = importlib.util.spec_from_file_location("project_to_motion", SCRIPT)
motion = importlib.util.module_from_spec(MODULE)
MODULE.loader.exec_module(motion)

ARTICLE = (
    "An input is classified and passed to a worker.\n"
    "The router selects one worker.\n"
    "Workers can execute independently and the join collects results.\n"
    "Feedback evaluates a draft and may revise it.\n"
    "The same input is compared across two implementations.\n"
)


def example_spec(kind="pipeline", sources=None):
    source = {"id": "source-001", "kind": "article", "locator": "fixture:article", "sha256": hashlib.sha256(ARTICLE.encode()).hexdigest(), "text": ARTICLE}
    order = {"pipeline": ["input", "router", "worker-a"], "routing": ["input", "router", "worker-a", "worker-b"], "parallel": ["input", "worker-a", "worker-b", "join"], "feedback": ["draft", "evaluate", "revision"], "compare": ["before", "after"]}[kind]
    claim = {"pipeline": "An input is classified and passed to a worker.", "routing": "The router selects one worker.", "parallel": "Workers can execute independently and the join collects results.", "feedback": "Feedback evaluates a draft and may revise it.", "compare": "The same input is compared across two implementations."}[kind]
    entities = [{"id": name, "label": name.replace("-", " "), "detail": "Fixed input, original method" if name == "before" else "Fixed input, revised method"} for name in ("input", "router", "worker-a", "worker-b", "worker-c", "join", "draft", "evaluate", "revision", "before", "after")]
    spec = {"schema_version": 1, "title": "Request handling", "audience": "Developers", "goal": "Explain one mechanism", "format": "landscape", "fps": 30, "theme": "paper", "sources": sources or [source], "claims": [{"id": "claim-001", "text": claim, "evidence": [{"source_id": "source-001", "quote": claim}]}], "entities": entities, "scenes": [
        {"id": "opening", "kind": "hook", "duration": 5, "title": "Where does the request go?", "caption": "Follow one request.", "narration": "Follow one request.", "claim_ids": [], "entities": []},
        {"id": "mechanism", "kind": kind, "duration": 8, "title": "How it works", "caption": claim, "narration": claim, "claim_ids": ["claim-001"], "entities": order},
        {"id": "closing", "kind": "outcome", "duration": 5, "title": "Explore the implementation", "caption": "Read the source.", "narration": "", "claim_ids": [], "entities": []},
    ]}
    if kind == "routing":
        spec["scenes"][1]["active_branch"] = "worker-b"
    return spec


class PlanTests(unittest.TestCase):
    def test_article_to_review_packet_retains_evidence_and_requires_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            article = root / "article.md"
            article.write_bytes(ARTICLE.replace("\n", "\r\n").encode())
            bundle = motion.ingest_article(article)
            self.assertEqual(bundle["sources"][0]["text"], ARTICLE)
            self.assertEqual(bundle["sources"][0]["sha256"], hashlib.sha256(ARTICLE.encode()).hexdigest())
            spec = example_spec(sources=bundle["sources"])
            out = motion.prepare(spec, root / "review")
            manifest = json.loads((out / "evidence-manifest.json").read_text())
            self.assertEqual(manifest["validation"]["structural"], "passed")
            self.assertEqual(manifest["validation"]["semantic"], "requires_review")
            self.assertEqual(manifest["validation"]["visual"], "requires_review")
            anchor = manifest["claims"][0]["evidence"][0]
            self.assertEqual(ARTICLE[anchor["quote_start_char"]:anchor["quote_end_char"]], anchor["quote"])
            captions = (out / "captions.srt").read_text()
            self.assertIn("00:00:05,000 --> 00:00:13,000", captions)
            self.assertIn("mechanism / pipeline", (out / "storyboard.md").read_text())
            self.assertIn("No semantic review", (out / "review.md").read_text())
            with self.assertRaisesRegex(motion.MotionError, "already exists"):
                motion.prepare(spec, out)
            self.assertEqual(json.loads((out / "spec.json").read_text()), spec)

    def test_mechanical_validation_does_not_claim_semantic_entailment(self):
        spec = example_spec()
        spec["claims"][0]["text"] = "This software is always faster than every alternative."
        result = motion.validate_spec(spec)
        self.assertEqual(result["semantic_review"], "required")
        self.assertEqual(result["visual_review"], "required")

    def test_source_tampering_and_nonexistent_quotes_fail(self):
        spec = example_spec()
        spec["sources"][0]["text"] += "Changed after review."
        with self.assertRaisesRegex(motion.MotionError, "sha256 mismatch"):
            motion.validate_spec(spec)
        spec = example_spec()
        spec["claims"][0]["evidence"][0]["quote"] = "A fabricated quotation."
        with self.assertRaisesRegex(motion.MotionError, "exact quote is absent"):
            motion.validate_spec(spec)
        spec["claims"][0]["evidence"] = []
        with self.assertRaisesRegex(motion.MotionError, "evidence"):
            motion.validate_spec(spec)

    def test_all_mechanism_kinds_have_valid_contracts(self):
        for kind in ("pipeline", "routing", "parallel", "feedback", "compare"):
            with self.subTest(kind=kind):
                result = motion.validate_spec(example_spec(kind))
                self.assertEqual(result["total_frames"], 540)

    def test_routing_is_one_selected_branch_and_parallel_has_workers_and_join(self):
        routing = example_spec("routing")
        for invalid in ("input", "router", "missing", None):
            with self.subTest(branch=invalid):
                routing["scenes"][1]["active_branch"] = invalid
                with self.assertRaisesRegex(motion.MotionError, "active_branch"):
                    motion.validate_spec(routing)
        parallel = example_spec("parallel")
        parallel["scenes"][1]["entities"].insert(-1, "worker-c")
        self.assertEqual(motion.validate_spec(parallel)["scene_count"], 3)
        parallel["scenes"][1]["entities"] = ["input", "worker-a", "join"]
        with self.assertRaisesRegex(motion.MotionError, "invalid entity count"):
            motion.validate_spec(parallel)
        parallel["scenes"][1]["entities"] = ["input", "worker-a", "worker-a", "join"]
        with self.assertRaisesRegex(motion.MotionError, "duplicate ID"):
            motion.validate_spec(parallel)

    def test_semantic_shape_errors_are_not_silently_rendered(self):
        mutations = [
            lambda s: s["scenes"][0]["entities"].append("input"),
            lambda s: s["scenes"][1].update(claim_ids=[]),
            lambda s: s["scenes"][1].update(entities=["input"]),
            lambda s: s["scenes"][1].update(active_branch="worker-a"),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutations.index(mutate)):
                spec = example_spec()
                mutate(spec)
                with self.assertRaises(motion.MotionError):
                    motion.validate_spec(spec)
        spec = example_spec("compare")
        del next(e for e in spec["entities"] if e["id"] == "before")["detail"]
        with self.assertRaisesRegex(motion.MotionError, "comparison entities require detail"):
            motion.validate_spec(spec)

    def test_unknown_keys_unknown_ids_and_duplicate_ids_fail(self):
        mutations = [
            lambda s: s.update(approval=True),
            lambda s: s["sources"][0].update(trusted=True),
            lambda s: s["claims"][0]["evidence"][0].update(verified=True),
            lambda s: s["scenes"][1].update(entities=["input", "imaginary"]),
            lambda s: s["scenes"][1].update(claim_ids=["missing"]),
            lambda s: s["claims"][0]["evidence"][0].update(source_id="missing"),
            lambda s: s["scenes"][2].update(id="opening"),
            lambda s: s["sources"].append(copy.deepcopy(s["sources"][0])),
            lambda s: s["claims"].append(copy.deepcopy(s["claims"][0])),
            lambda s: s["entities"].append(copy.deepcopy(s["entities"][0])),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(mutation=index):
                spec = example_spec()
                mutate(spec)
                with self.assertRaises(motion.MotionError):
                    motion.validate_spec(spec)

    def test_duration_frame_and_total_bounds(self):
        for duration in (math.nan, math.inf, -math.inf, -1, 0, 2.9, 18.1, True, 3.01):
            with self.subTest(duration=duration):
                spec = example_spec()
                spec["scenes"][1]["duration"] = duration
                with self.assertRaisesRegex(motion.MotionError, "duration"):
                    motion.validate_spec(spec)
        spec = example_spec()
        for scene in spec["scenes"]:
            scene["duration"] = 3
        with self.assertRaisesRegex(motion.MotionError, "total duration"):
            motion.validate_spec(spec)
        spec = example_spec()
        spec["scenes"][1]["duration"] = 8 + 1 / 30
        self.assertEqual(motion.validate_spec(spec)["total_frames"], 541)
        spec["scenes"] = [{**spec["scenes"][0], "id": f"scene-{n}", "duration": 18} for n in range(6)]
        with self.assertRaisesRegex(motion.MotionError, "total duration"):
            motion.validate_spec(spec)

    def test_text_and_scene_limits(self):
        changes = [lambda s: s.update(title="x" * 71), lambda s: s["scenes"][0].update(caption="x" * 151), lambda s: s["entities"][0].update(label="x" * 31), lambda s: s["entities"][0].update(detail="x" * 91), lambda s: s.update(scenes=s["scenes"][:2])]
        for mutate in changes:
            spec = example_spec()
            mutate(spec)
            with self.assertRaises(motion.MotionError):
                motion.validate_spec(spec)

    def test_invalid_plan_never_invokes_renderer_or_creates_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            spec = example_spec()
            spec["scenes"][1]["entities"] = ["input"]
            out = Path(temporary) / "render"
            with mock.patch.object(motion.subprocess, "run") as run:
                with self.assertRaises(motion.MotionError):
                    motion.render(spec, out)
                run.assert_not_called()
            self.assertFalse(out.exists())

    def test_stills_render_forwards_options_without_shell_and_uses_validated_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            renderer = root / "assets" / "renderer" / "render.mjs"
            renderer.parent.mkdir(parents=True)
            renderer.write_text("// invocation stub; no browser required in this unit test\n")
            spec = example_spec()
            output = root / "result with spaces"
            browser = root / "browser with spaces"
            observed = []

            def pretend_renderer(command, **kwargs):
                self.assertIsInstance(command, list)
                self.assertNotIn("shell", kwargs)
                snapshot = Path(command[command.index("--spec") + 1])
                self.assertEqual(json.loads(snapshot.read_text()), spec)
                observed.extend(command)
                output.mkdir()
                for name in ("motion.json", "storyboard.html", "render-manifest.json", "captions.srt", "narration.txt"):
                    (output / name).write_text("fixture")
                (output / "storyboard").mkdir()
                for number in range(3 * len(spec["scenes"])):
                    (output / "storyboard" / f"frame-{number}.png").write_bytes(b"fixture")
                return subprocess.CompletedProcess(command, 0)

            with mock.patch.object(motion, "SKILL_ROOT", root), mock.patch.object(motion.shutil, "which", return_value=sys.executable), mock.patch.object(motion.subprocess, "run", side_effect=pretend_renderer):
                self.assertEqual(motion.render(spec, output, browser_executable=browser, concurrency=1, stills_only=True), output)
            self.assertIn("--stills-only", observed)
            self.assertEqual(observed[observed.index("--browser-executable") + 1], str(browser))
            self.assertEqual(observed[observed.index("--concurrency") + 1], "1")
            self.assertEqual(observed[observed.index("--python-executable") + 1], sys.executable)
            self.assertFalse((output / "video.mp4").exists())

    def test_duplicate_json_keys_are_rejected_instead_of_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "duplicate.json"
            path.write_text('{"schema_version":1,"schema_version":2}')
            with self.assertRaisesRegex(motion.MotionError, "duplicate object key"):
                motion.load_json(path)

    def test_cli_validate_stdin_uses_the_same_strict_bounded_parser(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "validate", "-"], input=json.dumps(example_spec()), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for raw, message in (("{\"schema_version\":1,\"schema_version\":2}", "duplicate object key"), (" " * (motion.MAX_SPEC_BYTES + 1), "exceeds")):
            with self.subTest(message=message):
                result = subprocess.run([sys.executable, str(SCRIPT), "validate", "-"], input=raw, capture_output=True, text=True)
                self.assertEqual(result.returncode, 2)
                self.assertIn(message, result.stderr)

    def test_cli_reports_actionable_failure_and_exports_valid_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = root / "spec.json"
            plan.write_text(json.dumps(example_spec()))
            result = subprocess.run([sys.executable, str(SCRIPT), "validate", str(plan)], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["semantic_review"], "required")
            spec = example_spec()
            spec["scenes"][1]["claim_ids"] = ["unknown"]
            plan.write_text(json.dumps(spec))
            result = subprocess.run([sys.executable, str(SCRIPT), "prepare", str(plan), "--output", str(root / "bad")], text=True, capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("unknown ID", result.stderr)
            self.assertFalse((root / "bad").exists())

    def test_text_ingestion_is_bounded_and_utf8_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "article.txt"
            for content in (b"x" * (motion.MAX_FILE_BYTES + 1), b"\xff\xfe", b"hello\0world"):
                path.write_bytes(content)
                with self.assertRaises(motion.MotionError):
                    motion.ingest_article(path)
            path.write_bytes(b"\xef\xbb\xbfA\rB\r\nC\n")
            text = motion.ingest_article(path)["sources"][0]["text"]
            self.assertEqual(text, "\ufeffA\nB\nC\n")

    def test_catalog_retrieval_keeps_stable_ids_and_evidence_level(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source-index.json").write_text(json.dumps({"schema_version": 1, "sources": [{"id": "EX15", "title": "Packet routing", "url": "https://example.invalid/reference", "evidence_level": "page_verified"}]}))
            (root / "motion-cases.json").write_text(json.dumps({"schema_version": 1, "cases": [{"id": "EC08", "source_id": "EX15", "viewer_problem": "How does routing change firewall state?"}]}))
            results = motion.search_catalog("routing", references=root)
            self.assertEqual({r["id"] for r in results}, {"EX15", "EC08"})
            self.assertEqual(motion.show_record("EX15", root)["evidence_level"], "page_verified")
            self.assertEqual(len(motion.search_catalog("routing", kind="case", references=root)), 1)
            with self.assertRaisesRegex(motion.MotionError, "Unknown reference ID"):
                motion.show_record("missing", root)


@unittest.skipUnless(shutil.which("git"), "Git is needed for repository ingestion tests")
class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name) / "repo"
        self.repo.mkdir()
        self.git("init", "--quiet")
        self.git("config", "user.name", "Motion test")
        self.git("config", "user.email", "motion-test@example.invalid")
        (self.repo / "README.md").write_text("Committed overview.\n")
        (self.repo / "module.txt").write_text("Worker results are joined.\n")
        self.commit()
        self.commit_id = self.git("rev-parse", "HEAD").strip()

    def git(self, *arguments):
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
        result = subprocess.run(["git", "-C", str(self.repo), *arguments], capture_output=True, text=True, env=env, check=True)
        return result.stdout

    def commit(self):
        self.git("add", "--all")
        self.git("-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "fixture")

    def test_pinned_blobs_ignore_dirty_worktree_and_do_not_run_hooks(self):
        (self.repo / "README.md").write_text("Uncommitted replacement.\n")
        marker = self.repo / "hook-was-run"
        hook = self.repo / ".git" / "hooks" / "post-checkout"
        hook.write_text("#!/bin/sh\ntouch hook-was-run\n")
        hook.chmod(0o755)
        bundle = motion.ingest_repo(self.repo, ["module.txt"], self.commit_id)
        self.assertEqual(bundle["provenance"]["commit"], self.commit_id)
        self.assertEqual(bundle["sources"][0]["text"], "Committed overview.\n")
        self.assertEqual(bundle["sources"][1]["text"], "Worker results are joined.\n")
        self.assertIn(self.commit_id, bundle["sources"][1]["locator"])
        self.assertFalse(marker.exists())
        self.assertEqual((self.repo / "README.md").read_text(), "Uncommitted replacement.\n")

    def test_ambient_git_environment_cannot_redirect_selected_repository(self):
        with mock.patch.dict(os.environ, {"GIT_DIR": str(self.repo / "nonexistent.git"), "GIT_WORK_TREE": str(self.repo / "nonexistent-worktree")}):
            bundle = motion.ingest_repo(self.repo)
        self.assertEqual(bundle["provenance"]["commit"], self.commit_id)

    @unittest.skipUnless(os.name == "posix", "The harmless transport probes use POSIX shell scripts")
    def test_missing_promisor_blob_cannot_execute_ssh_or_remote_helpers(self):
        blob = self.git("rev-parse", "HEAD:README.md").strip()
        (self.repo / ".git" / "objects" / blob[:2] / blob[2:]).unlink()
        marker = self.repo / "external-command-ran"
        probe = self.repo / "ssh-probe"
        probe.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nexit 1\n")
        probe.chmod(0o755)
        helpers = self.repo / "helpers"
        helpers.mkdir()
        helper = helpers / "git-remote-motionprobe"
        helper.write_text(probe.read_text())
        helper.chmod(0o755)
        self.git("config", "remote.origin.promisor", "true")
        self.git("config", "remote.origin.partialclonefilter", "blob:none")
        self.git("config", "core.sshCommand", str(probe))
        real_run = subprocess.run
        for protocol, url in (("ssh", "ssh://motion-audit.invalid/repo"), ("motionprobe", "motionprobe::example")):
            self.git("config", "remote.origin.url", url)
            self.git("config", f"protocol.{protocol}.allow", "always")
            for emulate_older_git in (False, True):
                with self.subTest(protocol=protocol, no_lazy_fetch_unavailable=emulate_older_git):
                    # Removing this newer flag proves the empty protocol allowlist
                    # blocks execution even if a Git version does not support it.
                    def run_without_new_flag(command, **kwargs):
                        if emulate_older_git:
                            kwargs["env"] = {key: value for key, value in kwargs["env"].items() if key != "GIT_NO_LAZY_FETCH"}
                        return real_run(command, **kwargs)

                    with mock.patch.dict(os.environ, {"PATH": str(helpers) + os.pathsep + os.environ.get("PATH", "")}), mock.patch.object(motion.subprocess, "run", side_effect=run_without_new_flag):
                        with self.assertRaisesRegex(motion.MotionError, "Git evidence read failed"):
                            motion.ingest_repo(self.repo)
                    self.assertFalse(marker.exists(), "A missing evidence blob must never execute repository-configured transport commands")
        self.assertEqual((self.repo / "README.md").read_text(), "Committed overview.\n")

    def test_ref_is_resolved_before_reading_and_old_commit_stays_reproducible(self):
        (self.repo / "README.md").write_text("New revision.\n")
        self.commit()
        old = motion.ingest_repo(self.repo, ref=self.commit_id)
        current = motion.ingest_repo(self.repo)
        self.assertEqual(old["sources"][0]["text"], "Committed overview.\n")
        self.assertEqual(current["sources"][0]["text"], "New revision.\n")
        self.assertNotEqual(old["provenance"]["commit"], current["provenance"]["commit"])

    def test_untracked_and_unsafe_paths_are_rejected(self):
        (self.repo / "untracked.txt").write_text("Not in the selected commit.")
        for include in ("untracked.txt", "../outside.txt", "/etc/passwd", "docs/../module.txt", "./module.txt", "docs\\module.txt", ":(glob)*", "module*", ".git/config"):
            with self.subTest(include=include):
                with self.assertRaises(motion.MotionError):
                    motion.ingest_repo(self.repo, [include])
        with self.assertRaisesRegex(motion.MotionError, "not an option"):
            motion.ingest_repo(self.repo, ref="--help")

    @unittest.skipUnless(hasattr(os, "symlink"), "Symbolic links are unavailable")
    def test_committed_symlink_and_directory_are_not_read_as_files(self):
        os.symlink("module.txt", self.repo / "alias.txt")
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "a.txt").write_text("A document.")
        self.commit()
        for include in ("alias.txt", "docs"):
            with self.subTest(include=include):
                with self.assertRaisesRegex(motion.MotionError, "regular committed file"):
                    motion.ingest_repo(self.repo, [include])

    def test_large_committed_files_and_large_total_are_rejected(self):
        (self.repo / "large.txt").write_bytes(b"x" * (motion.MAX_FILE_BYTES + 1))
        for number in range(5):
            (self.repo / f"part{number}.txt").write_bytes(b"x" * (220 * 1024))
        self.commit()
        with self.assertRaisesRegex(motion.MotionError, "per-file"):
            motion.ingest_repo(self.repo, ["large.txt"])
        with self.assertRaisesRegex(motion.MotionError, "total limit"):
            motion.ingest_repo(self.repo, [f"part{n}.txt" for n in range(5)])


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for renderer semantics tests")
class RendererSemanticsTests(unittest.TestCase):
    def test_node_renderer_semantics(self):
        script = Path(__file__).with_name("renderer.test.mjs")
        self.assertTrue(script.is_file(), "The renderer semantics test must ship beside this Python suite.")
        result = subprocess.run([shutil.which("node"), str(script)], cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + "\n" + result.stderr)

    def test_python_and_javascript_accept_the_same_rendering_boundaries(self):
        cases = []
        for kind in ("pipeline", "routing", "parallel", "feedback", "compare"):
            for orientation in ("landscape", "portrait"):
                spec = example_spec(kind)
                spec["format"] = orientation
                cases.append({"name": f"{kind}-{orientation}", "spec": spec, "valid": True})

        maximum = example_spec()
        maximum.update(title="T" * 70, audience="A" * 240, goal="G" * 500)
        maximum["entities"].extend({"id": f"extra-{index}", "label": "L" * 30, "detail": "D" * 90} for index in range(30 - len(maximum["entities"])))
        maximum["entities"][-1]["id"] = "x" * 64
        for scene in maximum["scenes"]:
            scene.update(title="T" * 70, caption="C" * 150, narration="N" * 500)
        maximum["scenes"] = [{**maximum["scenes"][1], "id": f"scene-{index}", "duration": 5} for index in range(8)]
        maximum["language"] = "zh"
        cases.append({"name": "inclusive-maximum-fields", "spec": maximum, "valid": True})

        def entity_id(value):
            def mutate(spec):
                spec["entities"][0]["id"] = value
                spec["scenes"][1]["entities"][0] = value
            return mutate

        invalid = {
            "uppercase-entity": entity_id("Source"),
            "underscore-entity": entity_id("input_1"),
            "long-entity-id": entity_id("x" * 65),
            "uppercase-scene": lambda spec: spec["scenes"][0].update(id="Opening"),
            "empty-caption": lambda spec: spec["scenes"][0].update(caption=""),
            "blank-caption": lambda spec: spec["scenes"][0].update(caption=" \t "),
            "title-over-limit": lambda spec: spec.update(title="x" * 71),
            "audience-over-limit": lambda spec: spec.update(audience="x" * 241),
            "goal-over-limit": lambda spec: spec.update(goal="x" * 501),
            "narration-over-limit": lambda spec: spec["scenes"][0].update(narration="x" * 501),
            "caption-over-limit": lambda spec: spec["scenes"][0].update(caption="x" * 151),
            "too-many-entities": lambda spec: spec.update(entities=[{"id": f"item-{i}", "label": "Item"} for i in range(31)]),
        }
        for name, mutate in invalid.items():
            spec = example_spec()
            mutate(spec)
            cases.append({"name": name, "spec": spec, "valid": False})

        model = ROOT / "skill-library/project-to-motion/assets/renderer/model.mjs"
        javascript = """import {readFileSync} from 'node:fs';
const {validateSpec} = await import(process.argv[1]);
const cases = JSON.parse(readFileSync(0, 'utf8'));
console.log(JSON.stringify(cases.map(({name, spec}) => {
  try { validateSpec(spec); return {name, valid: true}; }
  catch (error) { return {name, valid: false, error: error.message}; }
})));"""
        result = subprocess.run([shutil.which("node"), "--input-type=module", "--eval", javascript, model.as_uri()], input=json.dumps(cases), capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = {item["name"]: item for item in json.loads(result.stdout)}
        for case in cases:
            with self.subTest(case=case["name"]):
                try:
                    motion.validate_spec(case["spec"])
                    python_valid = True
                except motion.MotionError:
                    python_valid = False
                self.assertEqual(python_valid, case["valid"])
                self.assertEqual(observed[case["name"]]["valid"], case["valid"], observed[case["name"]])

    def test_direct_node_entrypoint_rejects_tampered_evidence_before_rendering(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = example_spec()
            plan["sources"][0]["sha256"] = "0" * 64
            spec = root / "motion.json"
            spec.write_text(json.dumps(plan))
            output = root / "must-not-exist"
            renderer = ROOT / "skill-library/project-to-motion/assets/renderer/render.mjs"
            result = subprocess.run([shutil.which("node"), str(renderer), "--spec", str(spec), "--output", str(output), "--python-executable", sys.executable], capture_output=True, text=True, timeout=60)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("sha256 mismatch", result.stderr)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()

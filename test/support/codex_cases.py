"""Shared disposable fixtures; no test cases or test discovery."""

import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch
import uuid
from integrations import codex
from openmachine.bootstrap import create_application
from openmachine.foundation.state import save_json
from test.support.tasks import TaskFixture



class CodexCaseFixture(TaskFixture):
    def setUp(self):
        super().setUp()
        self.home = self.root / "codex-home"
        self.home.mkdir()
        save_json(self.home / "auth.json", {})
        (self.home / "AGENTS.md").write_text("Unrelated fixture instructions must not be inherited.\n")
        resources = self.root / "App/Resources"
        bridge = resources / "cua_node/bin/node_repl"
        bridge.parent.mkdir(parents=True)
        bridge.write_text("#!/bin/sh\nexit 0\n")
        bridge.chmod(0o700)
        root = resources / "plugins/openai-bundled/plugins/browser"
        skill = root / "skills/control-in-app-browser/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("Disposable Browser Skill, not a real browser backend.\n")
        (root / "scripts").mkdir()
        (root / "scripts/browser-client.mjs").write_text("// Fixture only.\n")
        self.config_text = ('model="fixture-model"\nmodel_reasoning_effort="xhigh"\n'
            '[mcp_servers.node_repl]\ncommand=' + json.dumps(str(bridge)) + '\nargs=[]\n'
            '[mcp_servers.unrelated]\ncommand="never-inherit"\n')
        (self.home / "config.toml").write_text(self.config_text)
        binary = self.root / "bin/codex"
        binary.parent.mkdir()
        binary.write_text("#!" + sys.executable + "\n" + '''import json,os,sys,time
from pathlib import Path
text=sys.stdin.read()
home=Path(os.environ["CODEX_HOME"])
assert not (home/"AGENTS.md").exists() and not (home/"AGENTS.override.md").exists()
assert (home/"auth.json").is_symlink()
packet=json.loads(text.rsplit("Delegated request:\\n",1)[1])
args=sys.argv[1:]
assert all(k in args for k in ["--ephemeral","--ignore-user-config","--json","--sandbox"])
assert args[args.index("--sandbox")+1]=="read-only"
assert "never-inherit" not in " ".join(args)
job=Path(args[args.index("-C")+1])
count=job/"fixture-counter"
count.write_text(str(int(count.read_text())+1) if count.exists() else "1")
if packet["goal"]=="hold":
    time.sleep(30)
report=dict(status="completed",summary="Observed fixture page",final_url=packet["start_url"],
            tab_id="tab-1",evidence=["Fixture heading is visible"],pending_action="")
if packet["goal"]=="blocked":
    report.update(status="blocked",pending_action="Sign in manually",evidence=[])
if packet["goal"]=="unicode":
    report["evidence"]=["\\u6d4b"*2000 for _ in range(12)]
print(json.dumps(dict(type="item.completed",item=dict(type="agent_message",text="PRIVATE_RAW_EVENT_SENTINEL"))),flush=True)
if packet["goal"]!="no-native":
    meta={"codex/browserUse":True,"browser_use":{"url":packet["start_url"]},
          "codex/toolSurface":{"browserId":"browser-1","openTabIds":["tab-1"],
                               "screenshot":{"url":"data:image/png;base64,fixture","tabId":"tab-1","pageUrl":packet["start_url"]}}}
    event=dict(type="item.completed",item=dict(type="mcp_tool_call",server="node_repl",tool="js",
        status="completed",arguments={"code":"await tab.ax.write(\\"both\\", { disableDiffing: true });"},
        result={"_meta":meta}))
    print(json.dumps(event),flush=True)
if packet["goal"]=="turn-failed":
    print(json.dumps(dict(type="turn.failed")),flush=True)
last=Path(args[args.index("--output-last-message")+1])
if packet["goal"]!="missing-final":
    last.write_text(json.dumps(report))
sys.exit(7 if packet["goal"]=="exit-failed" else 0)
''')
        binary.chmod(0o700)
        self.enterContext(patch.dict(os.environ, {"CODEX_HOME": str(self.home),
            "PATH": str(binary.parent) + os.pathsep + os.environ.get("PATH", "")}))

    def arguments(self, goal="inspect", **updates):
        return dict(machine="desktop", execution_id=uuid.uuid4().hex, cwd=str(self.work),
            goal=goal, start_url="https://example.com/path?q=fixture#end",
            allowed_origins=["https://example.com"], **updates)

    def finished(self, arguments):
        started = self.app.start_codex_task(**arguments)
        self.wait(started)
        return self.app.read_codex_task("desktop", arguments["execution_id"])

    def report_path(self, identity):
        return self.state / "executions" / identity / "codex-result.json"

    def event(self, url="https://example.com/path?q=fixture#end", code=None):
        return {"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "node_repl", "tool": "js",
            "status": "completed", "arguments": {"code": code or 'await tab.ax.write("both", { disableDiffing: true });'},
            "result": {"_meta": {"codex/browserUse": True, "browser_use": {"url": url},
                "codex/toolSurface": {"browserId": "b", "openTabIds": ["tab-1"]}},
                "content": [{"type": "image", "mimeType": "image/png", "data": "fixture"}]}}}

"""Behavioural checks for District against stubbed `factory`/`gh`/`systemctl`/`uv` on PATH.

Run: uv run python -m unittest discover -s tests

Nothing here touches the real host config, real units, or GitHub:
XDG_CONFIG_HOME and PATH point into a temp dir for every test.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from district import add, apply, atlas, cli, health, host, metrics, status  # noqa: E402
from district import dashboard as dash  # noqa: E402  (dashboard() below is the snapshot fixture)

STUB = '''#!/usr/bin/env python3
import fnmatch, json, pathlib, sys
here = pathlib.Path(__file__).parent
args = " ".join(sys.argv[1:])
with (here / "NAME.log").open("a") as f:
    f.write(args + "\\n")
# systemctl-shaped state: `disable --now U` makes `is-active U` report inactive until `enable --now U`
argv = sys.argv[1:]
if argv[:1] == ["--user"] and len(argv) >= 3 and argv[1] in ("disable", "enable") and argv[2] == "--now":
    for unit in argv[3:]:
        marker = here / f"{unit}.disabled"
        marker.touch() if argv[1] == "disable" else marker.unlink(missing_ok=True)
if argv[:2] == ["--user", "is-active"] and (here / f"{argv[2]}.disabled").exists():
    sys.stdout.write("inactive\\n")
    sys.exit(3)
for pat, out, rc in json.loads((here / "NAME.json").read_text()):
    if fnmatch.fnmatchcase(args, pat):
        sys.stdout.write(out)
        sys.exit(rc)
sys.exit(0)
'''

DOCTOR = {"ok": True, "version": "0.2.0", "repo": "acme/widgets", "root": "", "rows": [
    {"status": "PASS", "label": ".factory.toml present", "detail": ""},
    {"status": "WARN", "label": ".github/ISSUE_TEMPLATE/agent_task.md", "detail": "differs from shipped template"},
]}


def dashboard(failures: int = 0, last: str = "done", version: str = "0.2.0", errors: list | None = None) -> dict:
    return {
        "version": version, "errors": errors or [], "config": {"upstream": None},
        "dispatcher": {
            "timer": {"next": "2026-09-03T20:10:00Z", "last": "2026-09-03T20:00:00Z", "active": True},
            "service_active": False, "consecutive_failures": failures, "runs": [{"result": last}],
        },
        "upstream": {}, "metrics": {"first_pass": 0.5, "bounce_rate": 0.25}, "tickets": [],
    }


GPUFLO_SHAPE = '''# agent-factory configuration. Docs: https://github.com/mikeroySoft/factory

[repo]
# Not a fork: no upstream sync.

[dispatch]
max_active = 2

[gate]
timeout = 900
lock = "/tmp/gpu.lock"
[[gate.check]]
name = "fmt"
run = ["cargo", "fmt", "--check"]

[[gate.check]]
name = "tests"
run = ["cargo", "test", "--all-targets", "--locked"]
exclusive = true

[leak_scan]
exclude = ["scripts/agent_gate.py"]

[triage]
url = "http://127.0.0.1:11435/v1/chat/completions"
model = "ornith-ai/Ornith-1.5-35B-A3B-GGUF:Q4_K_M"

[dashboard]
port = 8766
theme = ".factory-dashboard.css"
'''


class DistrictCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.tmp / "xdg"), "PATH": f"{self.bin}:{os.environ['PATH']}"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.stub("factory", ("doctor --json", json.dumps(DOCTOR)), ("dashboard --json", json.dumps(dashboard())))
        self.stub("gh", ("repo view *", json.dumps({"isFork": False, "parent": None})))
        self.stub("systemctl", ("is-active *", "active\n"))
        self.stub("uv")

    def stub(self, name: str, *cases: tuple[str, str] | tuple[str, str, int]) -> None:
        exe = self.bin / name
        exe.write_text(STUB.replace("NAME", name))
        exe.chmod(0o755)
        prefix = "--user " if name == "systemctl" else ""
        (self.bin / f"{name}.json").write_text(json.dumps([(prefix + c[0], c[1], c[2] if len(c) > 2 else 0) for c in cases]))

    def calls(self, name: str) -> list[str]:
        log = self.bin / f"{name}.log"
        return [c.removeprefix("--user ") for c in log.read_text().splitlines()] if log.exists() else []

    def repo(self, name: str = "widgets", toml: str | None = None, origin: str = "git@github.com:acme/widgets.git") -> Path:
        repo = self.tmp / name
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", origin], check=True)
        if toml is not None:
            (repo / ".factory.toml").write_text(toml)
        return repo

    def district(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                code = cli.main(list(argv))
            except SystemExit as exc:
                out.write(str(exc) + "\n")
                code = 1
        return code, out.getvalue()

    def units(self, slug: str, host_: str = "0.0.0.0") -> None:
        unit = host.unit_name(slug)
        host.unit_dir().mkdir(parents=True, exist_ok=True)
        (host.unit_dir() / f"{unit}.timer").write_text("[Timer]\nOnBootSec=5min\nOnUnitActiveSec=10min\n")
        (host.unit_dir() / f"{unit}.service").write_text("[Service]\nExecStart=x\n")
        (host.unit_dir() / f"{unit}-dashboard.service").write_text(f"[Service]\nExecStart=x dashboard --host {host_} --port 1 --no-open\n")


class HostTest(DistrictCase):
    def test_round_trip_regex_strings(self) -> None:
        pattern = r'internal|\.corp|say "hi"|c:\path'
        host.save({"defaults": {"leak": {"p": pattern}}, "repo": {"acme/widgets": {"path": "/x", "dashboard": {"port": 8765}}}})
        data = host.load()
        self.assertEqual(data["defaults"]["leak"]["p"], pattern)
        self.assertEqual(host.repos(data), {"acme/widgets": {"path": "/x", "dashboard": {"port": 8765}}})
        self.assertFalse(host.path().with_suffix(".toml.tmp").exists())

    def test_next_port_after_gap_and_default_port_rejected(self) -> None:
        data = {"repo": {"a/b": {"path": "/a", "dashboard": {"port": 8765}}, "c/d": {"path": "/c", "dashboard": {"port": 8770}}}}
        self.assertEqual(host.next_port(data), 8771)
        self.assertEqual(host.next_port({}), 8765)
        host.save({"defaults": {"dashboard": {"port": 9000}}})
        with self.assertRaises(SystemExit):
            host.load()


    def test_factory_source_uses_renamed_checkout(self) -> None:
        self.assertEqual(host.DEFAULTS["factory_source"], "~/dev/mikeroysoft/factory")


class AddTest(DistrictCase):
    def test_adopt_writes_path_port_and_lifts_host_keys(self) -> None:
        repo = self.repo(toml=GPUFLO_SHAPE)
        self.units("acme/widgets")
        code, out = self.district("add", str(repo))
        self.assertEqual(code, 0, out)
        data = host.load()
        table = data["repo"]["acme/widgets"]
        self.assertEqual(table["path"], str(repo))
        self.assertEqual(table["dashboard"], {"port": 8766})  # adopted port persisted, not re-allocated
        # a lone repo agrees with itself: host values promote to [defaults], nothing left per-repo
        self.assertEqual(data["defaults"]["triage"]["model"], "ornith-ai/Ornith-1.5-35B-A3B-GGUF:Q4_K_M")
        self.assertEqual(data["defaults"]["gate"], {"lock": "/tmp/gpu.lock"})
        self.assertEqual(data["defaults"]["install"], {"every": "10min", "dashboard": True, "host": "0.0.0.0"})
        for name in ("triage", "gate", "install"):
            self.assertNotIn(name, table)
        self.assertIn("now in [defaults]: triage.model, triage.url, install.dashboard, install.every, install.host, gate.lock", out)
        text = (repo / ".factory.toml").read_text()
        kept = tomllib.loads(text)
        self.assertEqual(kept["dashboard"], {"theme": ".factory-dashboard.css"})
        self.assertEqual(kept["gate"]["timeout"], 900)
        self.assertEqual(len(kept["gate"]["check"]), 2)
        self.assertNotIn("triage", kept)
        self.assertNotIn("lock", kept["gate"])
        self.assertIn("# Not a fork: no upstream sync.", text)  # comments byte-for-byte
        self.assertNotIn("\n\n\n", text)
        self.assertIn("-port = 8766", out)
        self.assertIn("git -C", out)
        self.assertEqual(self.calls("factory")[1:], ["init --no-labels", "init --labels-only", "doctor", "install"])

    def test_adopt_removes_whole_table_when_every_key_moved(self) -> None:
        toml = '[triage]\nurl = "u"\n\n[dashboard]\nport = 8765\n'
        repo = self.repo(toml=toml)
        code, out = self.district("add", str(repo))
        self.assertEqual(code, 0, out)
        self.assertEqual((repo / ".factory.toml").read_text().strip(), "")
        self.assertEqual(host.load()["repo"]["acme/widgets"]["dashboard"]["port"], 8765)

    def test_dedupe_promotes_agreed_values_and_keeps_disagreements(self) -> None:
        data = {"repo": {
            "a/x": {"path": "/x", "dashboard": {"port": 8765}, "triage": {"url": "u", "model": "m"}, "install": {"host": "0.0.0.0"}},
            "b/y": {"path": "/y", "dashboard": {"port": 8766}, "triage": {"url": "u", "model": "other"}},
        }}
        self.assertEqual(host.dedupe(data), ["triage.url"])
        self.assertEqual(data["defaults"], {"triage": {"url": "u"}})
        self.assertEqual(data["repo"]["a/x"]["triage"], {"model": "m"})
        self.assertEqual(data["repo"]["a/x"]["install"], {"host": "0.0.0.0"})  # b/y lacks it: not shared
        self.assertEqual(data["repo"]["b/y"]["triage"], {"model": "other"})
        self.assertEqual(data["repo"]["a/x"]["dashboard"], {"port": 8765})  # ports never move
        # second pass: a value now equal to an existing default is dropped, nothing new promoted
        data["repo"]["b/y"]["triage"]["url"] = "u"
        self.assertEqual(host.dedupe(data), [])
        self.assertNotIn("url", data["repo"]["b/y"]["triage"])

    def test_adopt_refuses_inline_tables_and_writes_nothing(self) -> None:
        toml = 'dashboard = { port = 8765, theme = "t" }\n[triage]\nurl = "u"\n'
        repo = self.repo(toml=toml)
        code, out = self.district("add", str(repo))
        self.assertEqual(code, 1)
        self.assertIn("cannot adopt", out)
        self.assertEqual((repo / ".factory.toml").read_text(), toml)
        self.assertNotIn("triage", host.load()["repo"]["acme/widgets"])

    def test_explicit_slug_keys_registry(self) -> None:
        self.stub("factory", ("doctor --json", json.dumps({**DOCTOR, "repo": "other/name"})))
        repo = self.repo(toml='[repo]\nslug = "other/name"\n[triage]\nmodel = "m"\n')
        code, out = self.district("add", str(repo))
        self.assertEqual(code, 0, out)
        self.assertEqual(list(host.load()["repo"]), ["other/name"])

    def test_basename_collision_refused(self) -> None:
        host.save({"repo": {"someone/widgets": {"path": "/elsewhere", "dashboard": {"port": 8765}}}})
        repo = self.repo(toml="")
        code, out = self.district("add", str(repo))
        self.assertEqual(code, 1)
        self.assertIn("factory-widgets", out)
        self.assertNotIn("acme/widgets", host.load()["repo"])

    def test_port_allocation_persists_above_highest(self) -> None:
        host.save({"repo": {"a/b": {"path": "/a", "dashboard": {"port": 8770}}}})
        repo = self.repo(toml="")
        code, out = self.district("add", str(repo))
        self.assertEqual(code, 0, out)
        self.assertEqual(host.load()["repo"]["acme/widgets"]["dashboard"]["port"], 8771)
        # an adopted port already taken by another repo is re-allocated
        other = self.repo("gadgets", toml="[dashboard]\nport = 8770\n", origin="git@github.com:acme/gadgets.git")
        self.stub("factory", ("doctor --json", json.dumps({**DOCTOR, "repo": "acme/gadgets"})))
        code, out = self.district("add", str(other))
        self.assertEqual(code, 0, out)
        self.assertEqual(host.load()["repo"]["acme/gadgets"]["dashboard"]["port"], 8772)

    def test_fork_parent_remote_under_another_name(self) -> None:
        self.stub("gh", ("repo view *", json.dumps({"isFork": True, "parent": {"nameWithOwner": "ROCm/widgets"}})))
        repo = self.repo(toml='[repo]\nupstream = "rocm"\n')
        subprocess.run(["git", "-C", str(repo), "remote", "add", "rocm", "https://github.com/ROCm/widgets.git"], check=True)
        code, out = self.district("add", str(repo))
        self.assertEqual(code, 0, out)
        self.assertIn("upstream remote `rocm`", out)
        remotes = subprocess.run(["git", "-C", str(repo), "remote"], capture_output=True, text=True).stdout.split()
        self.assertEqual(sorted(remotes), ["origin", "rocm"])

    def test_fork_occupied_upstream_name_aborts(self) -> None:
        self.stub("gh", ("repo view *", json.dumps({"isFork": True, "parent": {"nameWithOwner": "ROCm/widgets"}})))
        repo = self.repo(toml='[triage]\nmodel = "m"\n')
        subprocess.run(["git", "-C", str(repo), "remote", "add", "upstream", "https://github.com/someone/else.git"], check=True)
        code, out = self.district("add", str(repo))
        self.assertEqual(code, 1)
        self.assertIn("remote `upstream` is https://github.com/someone/else.git", out)
        self.assertEqual((repo / ".factory.toml").read_text(), '[triage]\nmodel = "m"\n')
        self.assertEqual(len(self.calls("factory")), 1)  # only the initial doctor

    def test_fork_adds_upstream_remote_when_missing(self) -> None:
        self.stub("gh", ("repo view *", json.dumps({"isFork": True, "parent": {"nameWithOwner": "ROCm/widgets"}})))
        repo = self.repo()
        (repo / "Cargo.toml").write_text("[package]\nname='w'\n")
        code, out = self.district("add", str(repo), "--no-edit", "--exclusive", "tests")
        self.assertEqual(code, 0, out)
        url = subprocess.run(["git", "-C", str(repo), "remote", "get-url", "upstream"], capture_output=True, text=True).stdout.strip()
        self.assertEqual(url, "https://github.com/ROCm/widgets.git")
        doc = tomllib.loads((repo / ".factory.toml").read_text())
        self.assertEqual(doc["repo"], {"upstream": "upstream"})
        self.assertEqual([(c["name"], c["exclusive"]) for c in doc["gate"]["check"]], [("fmt", False), ("clippy", False), ("tests", True)])
        self.assertNotIn("triage", doc)

    def test_net_new_without_marker_writes_nothing(self) -> None:
        repo = self.repo()
        code, out = self.district("add", str(repo), "--no-edit")
        self.assertEqual(code, 1)
        self.assertIn("pass --check", out)
        self.assertFalse((repo / ".factory.toml").exists())
        self.assertEqual(host.load(), {})
        self.assertEqual(len(self.calls("factory")), 1)

    def test_check_flag_and_placeholder_refused(self) -> None:
        repo = self.repo()
        code, out = self.district("add", str(repo), "--no-edit", "--check", "tests=true")
        self.assertEqual(code, 1)
        self.assertIn("no [[gate.check]] with a real command", out)
        self.assertFalse((repo / ".factory.toml").exists())
        code, out = self.district("add", str(repo), "--no-edit", "--check", 'tests=sh -c "make check"')
        self.assertEqual(code, 0, out)
        doc = tomllib.loads((repo / ".factory.toml").read_text())
        self.assertEqual(doc["gate"]["check"][0]["run"], ["sh", "-c", "make check"])

    def test_factory_failure_keeps_registry_for_resume(self) -> None:
        self.stub("factory", ("doctor --json", json.dumps(DOCTOR)), ("install", "boom\n", 1))
        repo = self.repo(toml="")
        code, out = self.district("add", str(repo))
        self.assertEqual(code, 1)
        self.assertIn("district apply acme/widgets", out)
        self.assertIn("acme/widgets", host.load()["repo"])

    def test_lift_keeps_other_bytes(self) -> None:
        text = "# head\n[gate]\nlock = '/l'  # host\ntimeout = 3\n[[gate.check]]\nname = 'a'\nrun = ['x']\n[workers]\ndefault = [\n  'omp',\n]\n[dashboard]\ntheme = 't'\n"
        new, lifted = add.lift(text)
        self.assertEqual(lifted, {"workers": {"default": ["omp"]}, "gate": {"lock": "/l"}})
        self.assertEqual(new, "# head\n[gate]\ntimeout = 3\n[[gate.check]]\nname = 'a'\nrun = ['x']\n[dashboard]\ntheme = 't'\n")
        with self.assertRaises(add.Refuse):
            add.lift("dashboard.port = 1\n")
        with self.assertRaises(add.Refuse):
            add.lift("[gate]\nlock = '''\n/l'''\ntimeout = 1\n")


class ApplyTest(DistrictCase):
    def register(self, failures: int = 0, **extra) -> Path:
        repo = self.repo(toml="")
        host.save({"repo": {"acme/widgets": {"path": str(repo), "dashboard": {"port": 8765}, **extra}}})
        self.stub("factory", ("doctor --json", json.dumps(DOCTOR)), ("dashboard --json", json.dumps(dashboard(failures))))
        return repo

    def test_npm_day_conversion_and_policy_env(self) -> None:
        self.assertEqual((apply.npm_days("24h"), apply.npm_days("36h"), apply.npm_days("1h"), apply.npm_days("3d")), (1, 2, 1, 3))
        self.assertEqual(apply.hours("90min"), 1.5)
        with self.assertRaises(SystemExit):
            apply.hours("soon")
        self.register()
        code, out = self.district("apply")
        self.assertEqual(code, 0, out)
        env = host.load()["defaults"]["install"]["env"]
        self.assertEqual(env["NPM_CONFIG_MIN_RELEASE_AGE"], "1")
        self.assertRegex(env["UV_EXCLUDE_NEWER"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertIn("policy 24h", out)
        self.assertIn("drift: .github/ISSUE_TEMPLATE/agent_task.md", out)
        self.assertEqual(self.calls("factory"), ["install", "init --labels-only", "doctor --json", "dashboard --json"])
        data = host.load()
        data["defaults"]["min_package_age"] = "0"
        host.save(data)
        self.district("apply")
        self.assertNotIn("env", host.load()["defaults"]["install"])

    def test_install_nonzero_is_fail_row(self) -> None:
        self.register()
        self.stub("factory", ("install", "units: nope\n", 1), ("doctor --json", json.dumps(DOCTOR)), ("dashboard --json", json.dumps(dashboard())))
        code, out = self.district("apply")
        self.assertEqual(code, 1)
        self.assertIn("FAIL install: units: nope", out)

    def test_failure_cap(self) -> None:
        self.register(failures=9)
        code, out = self.district("apply")
        self.assertEqual(code, 0, out)
        self.assertNotIn("disable", " ".join(self.calls("systemctl")))
        self.stub("factory", ("doctor --json", json.dumps(DOCTOR)), ("dashboard --json", json.dumps(dashboard(10))))
        code, out = self.district("apply")
        self.assertEqual(code, 1)
        self.assertIn("disable --now factory-widgets.timer", self.calls("systemctl"))
        table = host.load()["repo"]["acme/widgets"]
        self.assertIn("10 consecutive failed passes", table["disabled_reason"])
        self.assertTrue(table["disabled_at"])
        # second apply: no install (would re-enable), still nonzero
        (self.bin / "factory.log").unlink()
        self.stub("systemctl", ("is-active *.timer", "inactive\n"), ("is-active *", "inactive\n"))
        code, out = self.district("apply")
        self.assertEqual(code, 1)
        self.assertNotIn("install", self.calls("factory"))
        self.assertIn("DISABLED since", out)
        # --reset: failed hand run keeps the cap
        self.stub("systemctl", ("start *", "", 1), ("is-active *", "inactive\n"))
        code, out = self.district("apply", "--reset", "acme/widgets")
        self.assertEqual(code, 1)
        self.assertIn("timer stays disabled", out)
        self.assertIn("disabled_at", host.load()["repo"]["acme/widgets"])
        # --reset: successful hand run clears it and install re-enables the timer
        (self.bin / "factory.log").unlink()
        self.stub("systemctl", ("start *", "", 0), ("is-active *", "active\n"))
        self.stub("factory", ("doctor --json", json.dumps(DOCTOR)), ("dashboard --json", json.dumps(dashboard(0))))
        code, out = self.district("apply", "--reset", "widgets")
        self.assertEqual(code, 0, out)
        self.assertNotIn("disabled_at", host.load()["repo"]["acme/widgets"])
        self.assertIn("start factory-widgets.service", self.calls("systemctl"))
        self.assertIn("install", self.calls("factory"))

    def test_upgrade_restores_timers_on_failure(self) -> None:
        src = self.tmp / "af"
        src.mkdir()
        subprocess.run(["git", "-C", str(src), "init", "-q"], check=True)
        self.register()
        data = host.load()
        data["defaults"] = {"factory_source": str(src)}
        host.save(data)
        self.stub("uv", ("tool install *", "no network\n", 1))
        self.stub("systemctl", ("is-active *.service", "inactive\n"), ("is-active *", "active\n"))
        code, out = self.district("apply", "--upgrade")
        self.assertEqual(code, 1)
        self.assertIn("uv tool install failed", out)
        calls = self.calls("systemctl")
        self.assertLess(calls.index("disable --now factory-widgets.timer"), calls.index("enable --now factory-widgets.timer"))
        self.assertEqual(self.calls("factory"), [])  # nothing ran after the failed reinstall
        self.assertFalse((self.bin / "factory-widgets.timer.disabled").exists())
        # a District-disabled timer is inactive: neither stopped nor restored
        data = host.load()
        data["repo"]["acme/widgets"]["disabled_at"] = "2026-09-03T00:00:00Z"
        host.save(data)
        (self.bin / "factory-widgets.timer.disabled").touch()
        (self.bin / "systemctl.log").unlink()
        code, out = self.district("apply", "--upgrade")
        self.assertEqual(code, 1)
        self.assertNotIn("disable --now factory-widgets.timer", self.calls("systemctl"))
        self.assertNotIn("enable --now factory-widgets.timer", self.calls("systemctl"))

    def test_upgrade_refuses_dirty_source(self) -> None:
        src = self.tmp / "af"
        src.mkdir()
        subprocess.run(["git", "-C", str(src), "init", "-q"], check=True)
        (src / "x").write_text("dirty")
        self.register()
        data = host.load()
        data["defaults"] = {"factory_source": str(src)}
        host.save(data)
        code, out = self.district("apply", "--upgrade")
        self.assertEqual(code, 1)
        self.assertIn("uncommitted changes", out)
        self.assertNotIn("disable --now factory-widgets.timer", self.calls("systemctl"))


class StatusTest(DistrictCase):
    def test_malformed_snapshot_is_unhealthy(self) -> None:
        repo = self.repo(toml="")
        host.save({"repo": {"acme/widgets": {"path": str(repo), "dashboard": {"port": 8765}}}})
        self.stub("factory", ("dashboard --json", "{not json"))
        code, out = self.district("status")
        self.assertEqual(code, 1)
        self.assertIn("UNHEALTHY", out)
        self.stub("factory", ("dashboard --json", json.dumps(dashboard(errors=["github: rate limited"]))))
        code, out = self.district("status")
        self.assertEqual((code, "UNHEALTHY" in out, "rate limited" in out), (1, True, True))
        self.stub("factory", ("dashboard --json", json.dumps(dashboard())))
        code, out = self.district("status")
        self.assertEqual(code, 0, out)
        self.assertIn("50%", out)
        self.assertIn("25%", out)
        self.stub("factory", ("dashboard --json", json.dumps(dashboard(last="failed"))))
        self.assertEqual(self.district("status")[0], 1)

    def test_rel_time(self) -> None:
        from datetime import datetime, timezone

        at = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(status.rel("2026-09-03T12:07:00Z", at), "in 7m")
        self.assertEqual(status.rel("2026-09-03T09:00:00Z", at), "3h ago")
        self.assertEqual(status.rel(None, at), "-")

    def test_health_column_and_json(self) -> None:
        repo = self.repo(toml="")
        host.save({"repo": {"acme/widgets": {"path": str(repo), "dashboard": {"port": 8765}}}})
        code, out = self.district("status")
        self.assertEqual(code, 0, out)
        self.assertIn("health", out.splitlines()[0])
        self.assertIn("acme/widgets  healthy", out)
        code, out = self.district("status", "--json")
        e = json.loads(out)["acme/widgets"]
        self.assertEqual((e["health"], e["reasons"], e["metrics"], e["error"]), ("healthy", [], None, None))
        self.assertEqual(e["snap"]["version"], "0.2.0")
        self.stub("factory", ("dashboard --json", json.dumps(dashboard(failures=1))))
        code, out = self.district("status")
        self.assertEqual(code, 1)
        self.assertIn("acme/widgets  failing", out)


class RmTest(DistrictCase):
    def test_rm_removes_units_and_entry(self) -> None:
        repo = self.repo(toml="")
        host.save({"repo": {"acme/widgets": {"path": str(repo), "dashboard": {"port": 8765}}}})
        self.units("acme/widgets")
        self.stub("systemctl", ("is-active *", "active\n"))
        code, out = self.district("rm", "widgets")
        self.assertEqual(code, 1)
        self.assertIn("running a pass", out)
        self.stub("systemctl", ("is-active *", "inactive\n"), ("show *", "not-found\n"))
        code, out = self.district("rm", "widgets")
        self.assertEqual(code, 0, out)
        self.assertEqual(list(host.unit_dir().glob("factory-*")), [])
        self.assertEqual(host.load(), {})
        self.assertTrue((repo / ".factory.toml").exists())
        calls = self.calls("systemctl")
        self.assertLess(calls.index("disable --now factory-widgets.timer"), calls.index("daemon-reload"))


class HealthTest(unittest.TestCase):
    """One case per row of the PRD §5.7 table."""

    def level(self, snap="fresh", table=None, doctor_rows=None, **snap_kw):
        return health.level(dashboard(**snap_kw) if snap == "fresh" else snap, table or {}, doctor_rows)

    def test_failing_rows(self) -> None:
        self.assertEqual(self.level(table={"disabled_at": "2026-09-03T00:00:00Z", "disabled_reason": "cap"})[0], "failing")
        self.assertEqual(self.level(snap=None), ("failing", ["dashboard unreachable"]))
        inactive = dashboard()
        inactive["dispatcher"]["timer"]["active"] = False
        self.assertEqual(self.level(snap=inactive), ("failing", ["timer inactive"]))
        lvl, reasons = self.level(last="failed", failures=1)
        self.assertEqual(lvl, "failing")
        self.assertEqual(reasons, ["last pass failed", "1 consecutive failed pass(es)"])
        self.assertEqual(self.level(errors=["github: rate limited"]), ("failing", ["snapshot errors: github: rate limited"]))
        self.assertEqual(self.level(failures=2)[1], ["2 consecutive failed pass(es)"])
        # a pass still running does not hide a failed last pass
        running = dashboard(last="failed")
        running["dispatcher"]["runs"].append({"result": "running"})
        self.assertEqual(self.level(snap=running)[0], "failing")

    def test_attention_rows(self) -> None:
        snap = dashboard()
        snap["tickets"] = [
            {"number": 7, "state": "OPEN", "labels": ["ready-for-human"], "stage": "escalated"},
            {"number": 8, "state": "CLOSED", "labels": ["ready-for-human"], "stage": "escalated"},
        ]
        self.assertEqual(self.level(snap=snap), ("attention", ["1 open ready-for-human (#7)"]))
        snap = dashboard()
        snap["upstream"] = {"blocker": {"number": 31}}
        self.assertEqual(self.level(snap=snap), ("attention", ["upstream sync parked on #31"]))
        snap = dashboard()
        snap["metrics"]["bounce_rate"] = 0.34
        self.assertEqual(self.level(snap=snap), ("attention", ["bounce rate 34%"]))
        snap["metrics"]["bounce_rate"] = 0.33
        self.assertEqual(self.level(snap=snap)[0], "healthy")
        self.assertEqual(self.level(doctor_rows=DOCTOR["rows"]), ("attention", ["doctor WARN: .github/ISSUE_TEMPLATE/agent_task.md"]))
        # failing outranks attention; both lists are not merged
        snap = dashboard(failures=1)
        snap["upstream"] = {"blocker": {"number": 31}}
        self.assertEqual(self.level(snap=snap), ("failing", ["1 consecutive failed pass(es)"]))

    def test_healthy(self) -> None:
        self.assertEqual(self.level(), ("healthy", []))


class DoctorTest(DistrictCase):
    def doctor_tools(self, factory=("factory 0.2.0 (abc1234)\n", 0), gh=("", 0), system="running\n", linger="yes\n",
                     ss=""):
        self.stub("factory", ("--version", *factory))
        self.stub("gh", ("auth status", *gh))
        self.stub(
            "systemctl",
            ("is-system-running", system),
            ("show -p LoadState --value *", "not-found\n"),
            ("show -p MainPID --value *", "0\n"),
            ("is-active *", "inactive\n", 3),
        )
        self.stub("loginctl", ("show-user * -p Linger --value", linger))
        self.stub("ss", ("-ltnp sport = :*", ss))

    def test_doctor_json_passes_host_prerequisites(self) -> None:
        repo = self.repo()
        clone_dir = self.tmp / "clones"
        clone_dir.mkdir()
        host.save({
            "defaults": {
                "clone_dir": str(clone_dir),
                "engine": {"sha": "abc1234"},
                "install": {"env": {"UV_EXCLUDE_NEWER": "2026-09-03T00:00:00Z", "NPM_CONFIG_MIN_RELEASE_AGE": "1"}},
            },
            "repo": {"acme/widgets": {"path": str(repo), "dashboard": {"port": 8765}}},
        })
        self.doctor_tools()
        with mock.patch("district.doctor.shutil.which", side_effect=lambda name: f"/bin/{name}" if name != "cargo" else None):
            code, out = self.district("doctor", "--json")
        report = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(set(report), {"ok", "rows"})
        self.assertTrue(report["ok"])
        self.assertTrue(all(row["status"] == "PASS" for row in report["rows"]), report["rows"])
        self.assertIn({"status": "PASS", "label": "host file", "detail": str(host.path())}, report["rows"])
        self.assertIn({"status": "PASS", "label": "repo acme/widgets", "detail": f"{repo}: origin resolves to acme/widgets"}, report["rows"])

    def test_doctor_reports_prerequisite_warnings_and_failures(self) -> None:
        missing = self.tmp / "missing"
        host.save({
            "defaults": {"clone_dir": str(missing), "engine": {"sha": "abc1234"}, "install": {"env": {}}},
            "repo": {
                "acme/widgets": {"path": str(missing), "dashboard": {"port": 8765}},
                "acme/gadgets": {"path": str(missing), "dashboard": {"port": 8765}},
            },
        })
        self.doctor_tools(factory=("factory 0.2.0 (abc9999)\n", 0), gh=("not logged in\n", 1), system="offline\n", linger="no\n")
        with mock.patch("district.doctor.shutil.which", return_value="/bin/tool"):
            code, out = self.district("doctor", "--json")
        rows = json.loads(out)["rows"]
        rendered = "\n".join(f"{r['status']} {r['label']}: {r['detail']}" for r in rows)
        self.assertEqual(code, 1)
        self.assertIn("WARN factory: installed abc9999, expected abc1234", rendered)
        self.assertIn("FAIL gh auth: gh auth status exited 1", rendered)
        self.assertIn("FAIL systemd user manager: offline", rendered)
        self.assertIn("WARN linger: timers stop at logout: loginctl enable-linger", rendered)
        self.assertIn(f"FAIL repo acme/widgets: {missing} does not exist", rendered)
        self.assertIn("FAIL dashboard port 8765: shared by acme/gadgets, acme/widgets", rendered)
        self.assertIn(f"WARN clone_dir: {missing} is not a directory", rendered)
        self.assertIn("WARN policy uv: UV_EXCLUDE_NEWER missing: district apply writes it", rendered)
        self.assertIn("WARN policy npm: NPM_CONFIG_MIN_RELEASE_AGE missing: district apply writes it", rendered)
        self.assertIn("WARN policy cargo: cargo has no minimum-release-age control; crates.io installs are unguarded", rendered)

    def test_doctor_detects_foreign_port_holder_and_inactive_existing_unit(self) -> None:
        repo = self.repo()
        host.save({"repo": {"acme/widgets": {"path": str(repo), "dashboard": {"port": 8765}}}})
        self.doctor_tools(ss='LISTEN 0 4096 127.0.0.1:8765 0.0.0.0:* users:(("python",pid=99,fd=3))\n')
        self.stub(
            "systemctl",
            ("is-system-running", "degraded\n"),
            ("show -p MainPID --value factory-widgets-dashboard.service", "42\n"),
            ("show -p LoadState --value district-apply.timer", "loaded\n"),
            ("show -p LoadState --value *", "not-found\n"),
            ("is-active district-apply.timer", "inactive\n", 3),
        )
        with mock.patch("district.doctor.shutil.which", side_effect=lambda name: f"/bin/{name}" if name in {"factory"} else None):
            code, out = self.district("doctor")
        self.assertEqual(code, 1)
        self.assertIn("FAIL  dashboard port 8765: held by python (pid 99), not factory-widgets-dashboard.service", out)
        self.assertIn("WARN  district-apply.timer: inactive: district dashboard --install", out)

    def test_doctor_rejects_foreign_listener_after_expected_dashboard(self) -> None:
        repo = self.repo()
        host.save({"repo": {"acme/widgets": {"path": str(repo), "dashboard": {"port": 8765}}}})
        self.doctor_tools(ss=(
            'LISTEN 0 4096 127.0.0.1:8765 0.0.0.0:* users:(("factory",pid=42,fd=3))\n'
            'LISTEN 0 4096 0.0.0.0:8765 0.0.0.0:* users:(("python",pid=99,fd=4))\n'
        ))
        self.stub(
            "systemctl",
            ("is-system-running", "running\n"),
            ("show -p MainPID --value factory-widgets-dashboard.service", "42\n"),
            ("show -p LoadState --value *", "not-found\n"),
        )
        with mock.patch("district.doctor.shutil.which", side_effect=lambda name: f"/bin/{name}" if name == "factory" else None):
            code, out = self.district("doctor")
        self.assertEqual(code, 1)
        self.assertIn("FAIL  dashboard port 8765: held by python (pid 99), not factory-widgets-dashboard.service", out)

    def test_doctor_warns_for_broken_existing_unit(self) -> None:
        host.save({})
        self.doctor_tools()
        self.stub(
            "systemctl",
            ("is-system-running", "running\n"),
            ("show -p LoadState --value district-health.timer", "bad-setting\n"),
            ("show -p LoadState --value *", "not-found\n"),
            ("is-active district-health.timer", "failed\n", 3),
        )
        with mock.patch("district.doctor.shutil.which", side_effect=lambda name: f"/bin/{name}" if name == "factory" else None):
            code, out = self.district("doctor")
        self.assertEqual(code, 0)
        self.assertIn("WARN  district-health.timer: failed: district dashboard --install", out)

    def test_doctor_reports_malformed_host_and_missing_factory(self) -> None:
        host.path().parent.mkdir(parents=True)
        host.path().write_text("[broken")
        self.doctor_tools()
        with mock.patch("district.doctor.shutil.which", return_value=None):
            code, out = self.district("doctor", "--json")
        rows = json.loads(out)["rows"]
        self.assertEqual(code, 1)
        self.assertEqual(rows[0]["status"], "FAIL")
        self.assertEqual(rows[0]["label"], "host file")
        self.assertIn(str(host.path()), rows[0]["detail"])
        self.assertIn({"status": "FAIL", "label": "factory", "detail": "not found on PATH"}, rows)


GH_METRICS = [
    ("repo view *", json.dumps({"stargazerCount": 3, "forkCount": 1, "watchers": {"totalCount": 2}})),
    ("issue list * --state open *", json.dumps([
        {"labels": [{"name": "ready-for-human"}, {"name": "bug"}]}, {"labels": [{"name": "needs-triage"}]}, {"labels": []},
    ])),
    ("issue list * --state closed *", json.dumps([
        {"createdAt": "2026-09-01T00:00:00Z", "closedAt": "2026-09-03T00:00:00Z", "labels": [{"name": "Bug"}]},
        {"createdAt": "2026-09-02T00:00:00Z", "closedAt": "2026-09-02T12:00:00Z", "labels": []},
        {"createdAt": "2026-09-02T00:00:00Z", "closedAt": "2026-09-03T00:00:00Z", "labels": []},
    ])),
    ("pr list * --state open *", json.dumps([{"number": 1}, {"number": 2}])),
    ("pr list * --state merged *", json.dumps([{"headRefName": "agent/7", "mergedAt": "2026-09-01T00:00:00Z"},
                                               {"headRefName": "feat/x", "mergedAt": "2026-09-02T00:00:00Z"}])),
    ("api repos/*/traffic/views", '{"message":"Bad credentials","status":"401"}', 1),
    ("api repos/*/traffic/clones", json.dumps({"count": 40, "uniques": 9, "clones": []})),
]


class MetricsTest(DistrictCase):
    def committed_repo(self) -> Path:
        repo = self.repo()
        (repo / "src").mkdir()
        (repo / "src" / "main.rs").write_text("fn main() {}\nfn a() {}\nfn b() {}\n")
        (repo / "tests").mkdir()
        (repo / "tests" / "test_x.py").write_text("def test():\n    pass")  # no trailing newline: still 2 lines
        (repo / "README.md").write_text("# w\n")
        (repo / "logo.bin").write_bytes(b"\x89PNG\x00\x00binary\n\n\n")
        git = ["git", "-C", str(repo), "-c", "user.name=Ada", "-c", "user.email=ada@example.com"]
        subprocess.run([*git, "add", "."], check=True)
        subprocess.run([*git, "commit", "-q", "-m", "init"], check=True)
        return repo

    def test_collect_parses_git_and_gh(self) -> None:
        repo = self.committed_repo()
        self.stub("gh", *GH_METRICS)
        m = metrics.collect("acme/widgets", {"path": str(repo)})
        self.assertEqual((m["loc"], m["files"]), (6, 4))
        self.assertEqual(m["languages"], {"Rust": 3, "Python": 2, "Markdown": 1, "other": 0})
        self.assertEqual((m["test_loc"], m["test_files"]), (2, 1))
        self.assertEqual((m["contributors"], m["commits"], m["top3_share"], m["commits_30d"], m["commits_7d"]), (1, 1, 1.0, 1, 1))
        self.assertEqual((m["stars"], m["forks"], m["watchers"]), (3, 1, 2))
        self.assertEqual((m["open_issues"], m["open_by_label"], m["open_bugs"]), (3, {"ready-for-human": 1, "needs-triage": 1}, 1))
        self.assertEqual((m["open_prs"], m["merged_prs_30d"], m["agent_prs_30d"]), (2, 2, 1))
        self.assertEqual((m["closed_issues_30d"], m["closed_bugs_30d"], m["median_days_to_close"]), (3, 1, 1.0))
        self.assertEqual(m["traffic"], {"views": "unavailable", "clones": {"count": 40, "uniques": 9}})
        self.assertRegex(m["collected_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertRegex(m["head"], r"^[0-9a-f]{7,}$")
        self.assertIn("--search closed:>=", " ".join(self.calls("gh")))

    def test_cache_honours_max_age(self) -> None:
        repo = self.committed_repo()
        self.stub("gh", *GH_METRICS)
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.tmp / "cache")}):
            host.save({"repo": {"acme/widgets": {"path": str(repo), "dashboard": {"port": 8765}}}})
            code, out = self.district("metrics")
            self.assertEqual(code, 0, out)
            self.assertEqual(json.loads(out)["acme/widgets"]["loc"], 6)
            self.assertTrue(metrics.cache_path("acme/widgets").exists())
            n = len(self.calls("gh"))
            self.district("metrics", "widgets")
            self.assertEqual(len(self.calls("gh")), n, "fresh cache must not call gh")
            self.district("metrics", "--max-age", "0")
            self.assertGreater(len(self.calls("gh")), n)
            self.assertEqual(self.district("metrics", "--max-age", "soon")[0], 1)
            self.assertEqual(metrics.read("acme/widgets")["loc"], 6)
            self.assertIsNone(metrics.read("acme/nothing"))


def fleet_entry(loc: int | None, level: str = "healthy", upstream: str | None = None, **snap_kw) -> dict:
    snap = dashboard(**snap_kw)
    snap["config"] = {"upstream": upstream, "gate_checks": ["conflict-markers", "fmt", "tests", "leak-scan"], "exclusive_checks": ["tests"]}
    snap["metrics"] = {"first_pass": None, "bounce_rate": None, "escalations": 0}
    m = None if loc is None else {
        "collected_at": "2026-09-04T00:00:00Z", "head": "abc1234", "loc": loc, "files": 10, "languages": {"Rust": loc},
        "test_loc": 1, "test_files": 1, "contributors": 2, "commits": 5, "top3_share": 1.0, "commits_30d": 5, "commits_7d": 1,
        "stars": 0, "forks": 0, "watchers": 0, "open_issues": 1, "open_by_label": {"ready-for-human": 1}, "open_bugs": 0,
        "open_prs": 0, "merged_prs_30d": 1, "agent_prs_30d": 1, "closed_issues_30d": 0, "closed_bugs_30d": 0,
        "median_days_to_close": None, "traffic": {"views": "unavailable", "clones": {"count": 4, "uniques": 2}},
    }
    return {"table": {"path": "/x", "dashboard": {"port": 8765}}, "snap": snap, "error": None, "health": level,
            "reasons": [] if level == "healthy" else ["why"], "metrics": m}


class AtlasTest(unittest.TestCase):
    FLEET = {"acme/big": fleet_entry(400, "attention", upstream="upstream"), "acme/small": fleet_entry(100), "acme/new": fleet_entry(None, "failing")}

    def test_heights_scale_to_max_loc(self) -> None:
        data = atlas.data(self.FLEET)
        blocks = {b["id"]: b for b in (json.loads(l.rstrip(",")) for l in data.splitlines() if l.startswith('  {"id": "f_'))}
        self.assertEqual({b["h"] for b in blocks.values()}, {120.0, 66.0, 12.0})
        self.assertEqual((blocks["f_big"]["h"], blocks["f_big"]["ring"], blocks["f_big"]["health"]), (120.0, True, "attention"))
        self.assertEqual((blocks["f_small"]["h"], blocks["f_small"]["ring"]), (66.0, False))
        self.assertEqual(blocks["f_new"]["h"], 12.0)
        self.assertIn("not collected yet", data)
        roads = [l for l in data.splitlines() if l.startswith('  {"id": "p')]
        self.assertEqual(len(roads), 4)  # one dispatch road per factory + one upstream road for the fork
        self.assertIn('"upstream = \\"upstream\\""', data)
        self.assertIn('["factories", "3", "big · small · new"]', data)
        self.assertIn("2 unavailable", data)
        self.assertEqual(atlas.data({}).count('"id": "f_'), 0)

    def test_data_is_valid_javascript(self) -> None:
        node = __import__("shutil").which("node")
        if not node:
            self.skipTest("node not on PATH")
        script = atlas.data(self.FLEET) + "\nconsole.log(JSON.stringify(B.filter(b => b.cat === 'factory').map(b => [b.id, b.h]).concat([PLATES.length, P.length, KPIS.length])));"
        proc = subprocess.run([node, "-e", script], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), [["f_big", 120], ["f_small", 66], ["f_new", 12], 6, 20, 8])

    def test_page_splices_every_marker(self) -> None:
        page = atlas.page(self.FLEET)
        self.assertNotIn("@@", page)
        self.assertIn("3 factories", page)
        self.assertIn('id="manage"', page)
        self.assertIn("LOC/400", page)
        self.assertIn("mikeroySoft/factory@8f9baad", page)


class DashboardTest(DistrictCase):
    def test_act_rule(self) -> None:
        self.assertFalse(dash.act_allowed("10.0.0.5", False, "1"))
        self.assertTrue(dash.act_allowed("10.0.0.5", True, "1"))
        self.assertTrue(dash.act_allowed("127.0.0.1", False, "1"))
        self.assertTrue(dash.act_allowed("::1", False, "1"))
        self.assertFalse(dash.act_allowed("127.0.0.1", False, None))

    def test_server_routes(self) -> None:
        import threading
        import urllib.error
        import urllib.request
        from http.server import ThreadingHTTPServer

        repo = self.repo(toml="")
        host.save({"repo": {"acme/widgets": {"path": str(repo), "dashboard": {"port": 8765}}}})
        os.environ["PYTHONPATH"] = str(ROOT)
        self.addCleanup(os.environ.pop, "PYTHONPATH", None)
        server = ThreadingHTTPServer(("127.0.0.1", 0), dash.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"

        page = urllib.request.urlopen(base + "/").read().decode()
        self.assertIn('"id": "f_widgets"', page)
        fleet = json.loads(urllib.request.urlopen(base + "/api/fleet").read())
        self.assertEqual(fleet["fleet"]["acme/widgets"]["health"], "healthy")
        self.assertIn("const KPIS", fleet["data"])
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(urllib.request.Request(base + "/api/act", data=b'{"args":["apply"]}', method="POST"))
        self.assertEqual(ctx.exception.code, 403)
        req = urllib.request.Request(base + "/api/act", data=b'{"args":["status"]}', method="POST", headers={"X-District-Act": "1"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 400)
        req = urllib.request.Request(base + "/api/act", data=b'{"args":["apply","--reset","nope"]}', method="POST", headers={"X-District-Act": "1"})
        out = urllib.request.urlopen(req).read().decode()
        self.assertIn("$ district apply --reset nope", out)
        self.assertIn("nope is not registered", out)
        self.assertTrue(out.endswith("[exit 1]\n"), out)

    def test_install_writes_units(self) -> None:
        code, out = self.district("dashboard", "--install", "--port", "8761")
        self.assertEqual(code, 0, out)
        names = sorted(p.name for p in host.unit_dir().iterdir())
        self.assertEqual(names, ["district-dashboard.service", "district-metrics.service", "district-metrics.timer"])
        service = (host.unit_dir() / "district-dashboard.service").read_text()
        self.assertIn(f"Environment=PATH={os.environ['PATH']}", service)
        self.assertIn(f"ExecStart={sys.executable} -m district dashboard --port 8761 --no-open", service)
        self.assertIn("OnUnitActiveSec=1h", (host.unit_dir() / "district-metrics.timer").read_text())
        self.assertIn("metrics --refresh", (host.unit_dir() / "district-metrics.service").read_text())
        calls = self.calls("systemctl")
        self.assertIn("enable --now district-dashboard.service", calls)
        self.assertIn("enable --now district-metrics.timer", calls)
        self.assertLess(calls.index("daemon-reload"), calls.index("enable --now district-metrics.timer"))


class DryRunTest(DistrictCase):
    def test_dry_run_writes_nothing(self) -> None:
        repo = self.repo()
        (repo / "Cargo.toml").write_text("[package]\nname='w'\n")
        self.stub("gh", ("repo view *", json.dumps({"isFork": True, "parent": {"name": "widgets", "owner": {"login": "ROCm"}}})))
        code, out = self.district("add", "--dry-run", str(repo))
        self.assertEqual(code, 0, out)
        self.assertIn("repo: acme/widgets (onboard)", out)
        self.assertIn("dashboard port: 8765", out)
        self.assertIn("fork of: ROCm/widgets", out)
        self.assertIn("clippy: cargo clippy", out)
        self.assertEqual(host.load(), {})
        self.assertFalse((repo / ".factory.toml").exists())
        self.assertFalse(host.path().exists())
        self.assertEqual(self.calls("factory"), ["doctor --json"])
        self.assertNotIn("upstream", subprocess.run(["git", "-C", str(repo), "remote"], capture_output=True, text=True).stdout)

    def test_dry_run_adopt_reports_lift(self) -> None:
        repo = self.repo(toml=GPUFLO_SHAPE)
        before = (repo / ".factory.toml").read_text()
        code, out = self.district("add", "--dry-run", str(repo))
        self.assertEqual(code, 0, out)
        self.assertIn("(adopt)", out)
        self.assertIn("host keys to lift: triage, dashboard, gate", out)
        self.assertIn("tests: cargo test --all-targets --locked   # exclusive", out)
        self.assertEqual((repo / ".factory.toml").read_text(), before)
        self.assertEqual(host.load(), {})


if __name__ == "__main__":
    unittest.main()

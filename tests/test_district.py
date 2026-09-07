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
from datetime import datetime, timedelta, timezone
import tempfile
import time
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
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "version": version, "errors": errors or [], "config": {"upstream": None},
        "dispatcher": {
            "timer": {"next": (datetime.now(timezone.utc) + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ"), "last": "2026-09-03T20:00:00Z", "active": True},
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


class HostRunTest(unittest.TestCase):
    def test_captured_output_is_not_logged_without_a_checked_failure(self) -> None:
        for check, code in ((False, 0), (True, 0), (False, 7)):
            with self.subTest(check=check, code=code):
                out, err = io.StringIO(), io.StringIO()
                argv = [sys.executable, "-c",
                        f"import sys; print('result'); print('producer diagnostic', file=sys.stderr); sys.exit({code})"]
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    proc = host.run(argv, check=check)
                self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (code, "result\n", "producer diagnostic\n"))
                self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))

    def test_checked_failure_reports_command_and_exit_with_optional_output(self) -> None:
        argv = [sys.executable, "-c",
                "import sys; print('result'); print('producer diagnostic', file=sys.stderr); sys.exit(7)"]
        for quiet in (False, True):
            with self.subTest(quiet=quiet):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    with self.assertRaises(host.DistrictError) as raised:
                        host.run(argv, check=True, quiet=quiet)
                self.assertIn(" ".join(argv), str(raised.exception))
                self.assertIn("exited 7", str(raised.exception))
                self.assertIsInstance(raised.exception.code, str)
                self.assertEqual((out.getvalue(), err.getvalue()),
                                 ("", "") if quiet else ("result\n", "producer diagnostic\n"))

    def test_timeout_still_bounds_the_subprocess(self) -> None:
        with self.assertRaises(subprocess.TimeoutExpired):
            host.run([sys.executable, "-c", "import time; time.sleep(1)"], timeout=0.01)

    def test_timeout_stops_descendants_with_inherited_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = (
                "import time; from pathlib import Path; "
                f"root = Path({directory!r}); "
                "(root / 'ready').touch(); "
                "deadline = time.monotonic() + 2\n"
                "while not (root / 'trigger').exists() and time.monotonic() < deadline:\n"
                "    time.sleep(0.01)\n"
                "if (root / 'trigger').exists():\n"
                "    (root / 'survived').touch()\n"
            )
            parent = (
                "import subprocess, sys, time; "
                f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(3)"
            )
            with self.assertRaises(subprocess.TimeoutExpired):
                host.run([sys.executable, "-c", parent], timeout=0.5)
            self.assertTrue((root / "ready").exists(), "child never started")
            (root / "trigger").touch()
            time.sleep(0.3)
            self.assertFalse((root / "survived").exists(), "child kept working after timeout")


class DistrictCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.tmp / "xdg"), "XDG_CACHE_HOME": str(self.tmp / "cache"), "PATH": f"{self.bin}:{os.environ['PATH']}"})
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

    def test_workflow_checks_precede_and_suppress_marker_checks(self) -> None:
        repo = self.repo()
        workflows = repo / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (repo / "Cargo.toml").write_text("[package]\nname='w'\n")
        (workflows / "ci.yml").write_text(
            "jobs:\n"
            "  check:\n"
            "    steps:\n"
            "      - id: test\n"
            "        name: Tests\n"
            "        run: cargo test\n"
            "      - name: ignored action\n"
            "        uses: actions/checkout@v4\n"
            "      - run: |\n"
            "          cargo build --workspace\n"
            "          python scripts/check.py --self-test\n"
            "          echo nope\n"
            "          cargo test ${{ matrix.os }}\n"
            "          cargo fmt && cargo test\n"
            "          pytest --cov | tee coverage.txt\n"
            "          make test > output.log\n"
            "          python - <<PY\n"
        )
        (workflows / "lint.yaml").write_text(
            "steps:\n"
            "  - name: Lint Check!\n"
            "    run: ruff check .\n"
        )

        self.assertEqual(
            add.propose_checks(repo),
            [
                {"name": "tests", "run": ["cargo", "test"], "source": ".github/workflows/ci.yml:6"},
                {"name": "cargo-build", "run": ["cargo", "build", "--workspace"], "source": ".github/workflows/ci.yml:10"},
                {
                    "name": "python-scripts-check-py",
                    "run": ["python", "scripts/check.py", "--self-test"],
                    "source": ".github/workflows/ci.yml:11",
                },
                {"name": "lint-check", "run": ["ruff", "check", "."], "source": ".github/workflows/lint.yaml:3"},
                {"name": "fmt", "run": ["cargo", "fmt", "--check"], "source": "Cargo.toml"},
                {
                    "name": "clippy",
                    "run": ["cargo", "clippy", "--workspace", "--all-targets", "--", "-D", "warnings"],
                    "source": "Cargo.toml",
                },
            ],
        )

    def test_workflow_names_follow_run_without_leaking_across_steps(self) -> None:
        repo = self.repo()
        workflows = repo / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (repo / "Cargo.toml").write_text("[package]\nname='w'\n")
        (workflows / "ci.yml").write_text(
            "on:\n"
            "  push:\n"
            "    branches:\n"
            "    - main\n"
            "run: cargo outside\n"
            "jobs:\n"
            "  check:\n"
            "    steps:\n"
            "      - run: cargo test\n"
            "        name: Tests\n"
            "      - run: cargo build --workspace\n"
            "      - run: >\n"
            "          ruff check .\n"
            "        name: Lint Check!\n"
            "      - uses: actions/example@v1\n"
            "        with:\n"
            "          run: cargo nested\n"
            "          name: Not a step\n"
            "      - run: cargo test\n"
            "        name: Duplicate\n"
            "  other:\n"
            "    run: cargo outside-job\n"
            "    steps:\n"
            "    - run: python -m compileall .\n"
            "      env:\n"
            "        name: Not the step name\n"
        )
        checks = add.propose_checks(repo)
        self.assertEqual(checks[:4], [
            {"name": "tests", "run": ["cargo", "test"], "source": ".github/workflows/ci.yml:9"},
            {"name": "cargo-build", "run": ["cargo", "build", "--workspace"], "source": ".github/workflows/ci.yml:11"},
            {"name": "lint-check", "run": ["ruff", "check", "."], "source": ".github/workflows/ci.yml:13"},
            {"name": "python-m", "run": ["python", "-m", "compileall", "."], "source": ".github/workflows/ci.yml:24"},
        ])
        self.assertEqual([(check["name"], check["source"]) for check in checks[4:]],
                         [("fmt", "Cargo.toml"), ("clippy", "Cargo.toml")])

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
    def test_concurrent_apply_exits_before_running_commands(self) -> None:
        import fcntl

        self.register()
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.tmp / "cache")}):
            lock_path = metrics.cache_dir().parent / "apply.lock"
            lock_path.parent.mkdir(parents=True)
            with lock_path.open("w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    self.assertEqual(apply.main([]), 1)
        self.assertEqual(stderr.getvalue(), f"district apply: another apply is running ({lock_path})\n")
        self.assertEqual(self.calls("factory"), [])
        self.assertEqual(self.calls("systemctl"), [])


    def test_registry_uses_current_factory_configuration(self) -> None:
        base = Path(os.environ["XDG_CONFIG_HOME"])
        for name in ("factory", "agent-factory"):
            registry = base / name / "config.toml"
            registry.parent.mkdir(parents=True, exist_ok=True)
            registry.write_text(f'[repo."acme/{name}"]\npath = "/tmp/{name}"\n')
        self.assertEqual(set(host.repos(host.load())), {"acme/factory"})

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

    def test_upgrade_installs_the_renamed_engine_package(self) -> None:
        src = self.tmp / "af"
        src.mkdir()
        subprocess.run(["git", "-C", str(src), "init", "-q"], check=True)
        self.register()
        data = host.load()
        data["defaults"] = {"factory_source": str(src)}
        host.save(data)
        self.stub("uv", ("tool install *", "Installed 1 executable: factory\n"))
        self.stub("factory", ("--version", "0.2.0\n"))
        self.stub("systemctl", ("is-active *.service", "inactive\n"), ("is-active *", "active\n"))
        code, out = self.district("apply", "--upgrade")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.calls("uv"), [f"tool install --reinstall --from {src} factory"])
        self.assertEqual(self.calls("factory")[0], "--version")
        self.assertIn(f"installed factory 0.2.0 from {src}", out)

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
    def test_failed_observation_preserves_runtime_and_project_context(self) -> None:
        repo = self.repo(toml="")
        host.save({"repo": {"acme/widgets": {"path": str(repo), "dashboard": {"port": 8765}}}})
        self.stub("factory", ("dashboard --json", "{not json"))
        code, out = self.district("status", "--json")
        e = json.loads(out)["acme/widgets"]
        self.assertEqual((code, e["operating_state"], e["observation"], e["findings"]), (2, "unknown", "unavailable", []))
        partial = dashboard(errors=["github: rate limited"])
        partial["dispatcher"]["service_active"] = True
        self.stub("factory", ("dashboard --json", json.dumps(partial)))
        code, out = self.district("status", "--json")
        e = json.loads(out)["acme/widgets"]
        self.assertEqual((code, e["operating_state"], e["observation"], e["findings"]), (2, "running", "partial", []))
        code, out = self.district("status")
        self.assertEqual(code, 2, out)
        self.assertIn("running", out)
        self.assertIn("rate limited", out)

    def test_rel_time(self) -> None:
        at = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(status.rel("2026-09-03T12:07:00Z", at), "in 7m")
        self.assertEqual(status.rel("2026-09-03T09:00:00Z", at), "3h ago")
        self.assertEqual(status.rel(None, at), "-")

    def test_operational_columns_and_json_cap(self) -> None:
        repo = self.repo(toml="")
        table = {"path": str(repo), "dashboard": {"port": 8765}}
        host.save({"repo": {"acme/widgets": table}})
        code, out = self.district("status")
        self.assertEqual(code, 2, out)  # Legacy snapshots lack execution telemetry.
        self.assertIn("operating", out.splitlines()[0])
        self.assertIn("execution", out.splitlines()[0])
        code, out = self.district("status", "--json")
        e = json.loads(out)["acme/widgets"]
        self.assertEqual((e["assessment"], e["execution_state"], e["metrics"], e["error"]), ("unknown", "unknown", None, None))
        self.assertNotIn("health", e)
        self.assertNotIn("reasons", e)
        table.update(disabled_at="2026-09-03T00:00:00Z", disabled_reason="10 consecutive failed passes (cap 10)")
        host.save({"repo": {"acme/widgets": table}})
        code, out = self.district("status", "--json")
        e = json.loads(out)["acme/widgets"]
        self.assertEqual((code, e["operating_state"], e["assessment"]), (1, "capped", "attention"))

    def test_empty_json_is_machine_readable(self) -> None:
        code, out = self.district("status", "--json")
        self.assertEqual((code, json.loads(out)), (0, {}))

    def test_nonzero_snapshot_with_usable_local_facts(self) -> None:
        repo = self.repo(toml="")
        host.save({"repo": {"acme/widgets": {"path": str(repo)}}})
        snap = dashboard(errors=["github: unavailable"])
        snap["dispatcher"]["service_active"] = True
        self.stub("factory", ("dashboard --json", json.dumps(snap), 1))
        code, out = self.district("status", "--json")
        e = json.loads(out)["acme/widgets"]
        self.assertEqual((code, e["operating_state"], e["findings"]), (2, "running", []))

    def test_partial_snapshot_and_missing_factory_are_observation_gaps(self) -> None:
        repo = self.repo(toml="")
        host.save({"repo": {"acme/widgets": {"path": str(repo)}}})
        for snap in ({}, {"dispatcher": None, "config": [], "metrics": None, "tickets": None}):
            self.stub("factory", ("dashboard --json", json.dumps(snap)))
            code, out = self.district("status")
            self.assertEqual(code, 2, out)
            self.assertIn("unknown", out)
        with mock.patch("district.status.run", side_effect=FileNotFoundError("factory missing")):
            code, out = self.district("status", "--json")
        e = json.loads(out)["acme/widgets"]
        self.assertEqual((code, e["operating_state"], e["observation"], e["findings"]), (2, "unknown", "unavailable", []))


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

    def test_doctor_distinguishes_free_owned_and_unidentified_sockets(self) -> None:
        repo = self.repo()
        host.save({"repo": {"acme/widgets": {"path": str(repo), "dashboard": {"port": 8765}}}})
        header = "State Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
        owned = 'LISTEN 0 4096 127.0.0.1:8765 0.0.0.0:* users:(("factory",pid=42,fd=3))\n'
        unknown = "LISTEN 0 4096 [::1]:8765 [::]:*\n"
        shared = 'LISTEN 0 4096 127.0.0.1:8765 0.0.0.0:* users:(("factory",pid=42,fd=3),("python",pid=99,fd=4))\n'
        for sockets, expected_status, detail in (
            ("", "PASS", "free"),
            (owned, "PASS", "held by factory-widgets-dashboard.service"),
            (unknown, "FAIL", "unidentified"),
            (owned + unknown, "FAIL", "unidentified"),
            (shared, "FAIL", "python (pid 99)"),
        ):
            with self.subTest(sockets=sockets):
                self.doctor_tools(ss=header + sockets)
                self.stub(
                    "systemctl",
                    ("is-system-running", "degraded\n"),
                    ("show -p MainPID --value factory-widgets-dashboard.service", "42\n"),
                    ("show -p LoadState --value district-dashboard.service", "loaded\n"),
                    ("show -p LoadState --value *", "not-found\n"),
                    ("is-active district-dashboard.service", "active\n"),
                )
                with mock.patch("district.doctor.shutil.which", side_effect=lambda name: f"/bin/{name}" if name == "factory" else None):
                    code, out = self.district("doctor", "--json")
                report = json.loads(out)
                rows = {row["label"]: row for row in report["rows"]}
                self.assertEqual((code, report["ok"]), (0, True) if expected_status == "PASS" else (1, False))
                self.assertEqual(rows["dashboard port 8765"]["status"], expected_status)
                self.assertIn(detail, rows["dashboard port 8765"]["detail"])
                self.assertEqual(rows["systemd user manager"]["status"], "PASS")
                self.assertEqual(rows["district-dashboard.service"]["status"], "PASS")

    def test_doctor_reports_failed_commands_and_wrong_remote(self) -> None:
        repo = self.repo(origin="git@github.com:other/widgets.git")
        nongit = self.tmp / "nongit"
        nongit.mkdir()
        host.save({"repo": {
            "acme/widgets": {"path": str(repo), "dashboard": {"port": 8765}},
            "acme/nongit": {"path": str(nongit)},
        }})
        self.doctor_tools(factory=("", 1))
        self.stub("ss", ("-ltnp sport = :*", "", 1))
        with mock.patch("district.doctor.shutil.which", side_effect=lambda name: f"/bin/{name}" if name == "factory" else None):
            code, out = self.district("doctor", "--json")
        report = json.loads(out)
        rows = {row["label"]: row for row in report["rows"]}
        self.assertEqual((code, report["ok"]), (1, False))
        for label in ("factory", "repo acme/widgets", "repo acme/nongit", "dashboard port 8765"):
            self.assertEqual(rows[label]["status"], "FAIL", label)
        self.assertIn("other/widgets", rows["repo acme/widgets"]["detail"])

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


def fleet_entry(loc: int | None, assessment: str = "unknown", upstream: str | None = None, **snap_kw) -> dict:
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
    table = {"path": "/x", "dashboard": {"port": 8765}}
    if assessment == "attention":
        table.update(disabled_at="2026-09-03T00:00:00Z", disabled_reason="cap")
    return {"table": table, "snap": snap, "error": None, "metrics": m,
            **health.classify("acme/widgets", health.snapshot_sources(snap), table)}


class AtlasTest(unittest.TestCase):
    FLEET = {"acme/big": fleet_entry(400, "attention", upstream="upstream"), "acme/small": fleet_entry(100), "acme/new": fleet_entry(None)}

    def test_heights_scale_to_max_loc(self) -> None:
        data = atlas.data(self.FLEET)
        blocks = {b["id"]: b for b in (json.loads(l.rstrip(",")) for l in data.splitlines() if l.startswith('  {"id": "f_'))}
        self.assertEqual({b["h"] for b in blocks.values()}, {120.0, 66.0, 12.0})
        self.assertEqual((blocks["f_big"]["h"], blocks["f_big"]["ring"], blocks["f_big"]["assessment"]), (120.0, True, "attention"))
        self.assertEqual((blocks["f_small"]["h"], blocks["f_small"]["ring"]), (66.0, False))
        self.assertEqual(blocks["f_new"]["h"], 12.0)
        self.assertIn("not collected", dict(blocks["f_new"]["kpis"])["metrics"])
        roads = [l for l in data.splitlines() if l.startswith('  {"id": "p')]
        self.assertEqual(len(roads), 4)  # one dispatch road per factory + one upstream road for the fork
        self.assertEqual(atlas.data({}).count('"id": "f_'), 0)

    def test_partial_fleet_metrics_keep_known_totals(self) -> None:
        rows = {label: (value, detail) for label, value, detail in atlas.kpis(self.FLEET)}
        self.assertEqual(rows["factories"], ("3", "big · small · new"))
        self.assertEqual(rows["code under management"][0], "500")
        self.assertIn("20 files", rows["code under management"][1])
        self.assertIn("4 contributors", rows["code under management"][1])
        self.assertIn("1 not collected", rows["code under management"][1])
        self.assertEqual(rows["velocity 30d"][0], "10")
        self.assertIn("2 PRs merged", rows["velocity 30d"][1])
        self.assertIn("2 by agents", rows["velocity 30d"][1])
        self.assertEqual(rows["project work"][0], "2 / 0")
        self.assertIn("2 waiting on a human", rows["project work"][1])
        self.assertEqual(rows["defects"][0], "0")
        self.assertIn("0 closed 30d", rows["defects"][1])
        self.assertIn("0 issues closed 30d", rows["defects"][1])
        self.assertEqual(rows["traffic 14d"][0], "8")
        self.assertIn("4 unique", rows["traffic 14d"][1])
        self.assertIn("? views", rows["traffic 14d"][1])
        self.assertIn("2 unavailable", rows["traffic 14d"][1])
        self.assertIn("0 stars", rows["traffic 14d"][1])
        self.assertIn("0 forks", rows["traffic 14d"][1])
        unknown = {label: value for label, value, _ in atlas.kpis({"acme/new": self.FLEET["acme/new"]})}
        self.assertEqual(unknown["code under management"], "?")
        self.assertEqual(unknown["traffic 14d"], "?")

    def test_ground_bounds_follow_factory_plate(self) -> None:
        for count, expected_x1 in ((3, 36.5), (10, None)):
            fleet = {f"acme/factory-{i}": fleet_entry(100) for i in range(count)}
            script = atlas.data(fleet) + "\nconsole.log(JSON.stringify([G.x1, PLATES.find(p => p.name === 'FACTORIES').x1]));"
            proc = subprocess.run(["node", "-e", script], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            ground_x1, plate_x1 = json.loads(proc.stdout)
            self.assertEqual(ground_x1, expected_x1) if expected_x1 is not None else self.assertGreater(ground_x1, plate_x1)


    def test_data_is_valid_javascript(self) -> None:
        node = __import__("shutil").which("node")
        if not node:
            self.skipTest("node not on PATH")
        script = atlas.data(self.FLEET) + "\nconsole.log(JSON.stringify({B, PLATES, P, KPIS}));"
        proc = subprocess.run([node, "-e", script], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        generated = json.loads(proc.stdout)
        blocks = generated["B"]
        self.assertEqual([[b["id"], b["h"]] for b in blocks if b["cat"] == "factory"],
                         [["f_big", 120], ["f_small", 66], ["f_new", 12]])
        self.assertEqual({p["cat"] for p in generated["PLATES"]}, {b["cat"] for b in blocks})
        routes = {(p["from"], p["to"]) for p in generated["P"]}
        self.assertTrue({("systemd", "dispatch"), ("dashboard", "district"),
                         ("dispatch", "f_big"), ("dispatch", "f_small"), ("dispatch", "f_new"),
                         ("upstream", "f_big")} <= routes)
        self.assertFalse({("upstream", "f_small"), ("upstream", "f_new")} & routes)
        block_ids = {b["id"] for b in blocks}
        self.assertTrue(all(start in block_ids and end in block_ids for start, end in routes))
        kpis = {label: (value, detail) for label, value, detail in generated["KPIS"]}
        self.assertEqual(set(kpis), {"factories", "assessment", "code under management", "engine",
                                    "velocity 30d", "project work", "defects", "traffic 14d"})
        self.assertEqual(kpis["factories"], ("3", "big · small · new"))
        self.assertEqual(kpis["code under management"][0], "500")

    def test_page_splices_every_marker(self) -> None:
        at = datetime(2026, 9, 5, 12, 34, tzinfo=timezone.utc)
        page = atlas.page(self.FLEET, at)
        for marker in ("@@DATA@@", "@@EYEBROW@@", "@@FOOTER@@"):
            self.assertNotIn(marker, page)
        self.assertIn(atlas.data(self.FLEET), page.split("<script>", 1)[1])
        eyebrow = page.split('<div class="eyebrow">', 1)[1].split("</div>", 1)[0]
        self.assertIn("acme", eyebrow)
        self.assertIn("3 factories", eyebrow)
        self.assertIn("2026-09-05 12:34Z", eyebrow)
        footer = page.split('<footer class="site">', 1)[1].split("</footer>", 1)[0]
        self.assertIn("big @ abc1234", footer)
        self.assertIn("small @ abc1234", footer)
        self.assertIn("2026-09-05 12:34Z", footer)

        entry = fleet_entry(None)
        entry["error"] = r"@@EYEBROW@@ </script> \1"
        page = atlas.page({"acme/new": entry}, at)
        self.assertIn(r"@@EYEBROW@@ &lt;/script&gt; \\1", page)



class DashboardTest(DistrictCase):
    def test_remote_peer_cannot_claim_loopback_authority(self) -> None:
        from email.message import Message

        headers = Message()
        for key, value in (("Host", "127.0.0.1:8760"), ("Origin", "http://127.0.0.1:8760"), ("X-District-Act", "1")):
            headers[key] = value
        self.assertTrue(dash.act_allowed("127.0.0.1", ("127.0.0.1", 8760), headers))
        self.assertFalse(dash.act_allowed("10.0.0.5", ("127.0.0.1", 8760), headers))
        self.assertFalse(dash.act_allowed("127.0.0.1", ("10.0.0.5", 8760), headers))
        headers["X-Forwarded-For"] = "127.0.0.1"
        self.assertFalse(dash.act_allowed("10.0.0.5", ("127.0.0.1", 8760), headers))
        self.assertFalse(dash.act_allowed("127.0.0.1", ("127.0.0.1", 8760), headers))
        del headers["X-Forwarded-For"]
        headers["Host"] = "evil.example:8760"
        self.assertFalse(dash.act_allowed("127.0.0.1", ("127.0.0.1", 8760), headers))

    def test_mutation_boundary(self) -> None:
        import http.client
        import threading
        from http.server import ThreadingHTTPServer
        from unittest.mock import patch

        for bind in ("127.0.0.1", "0.0.0.0"):
            with ThreadingHTTPServer((bind, 0), dash.Handler) as server:
                server.collector = mock.Mock(
                    read=mock.Mock(return_value=(0, {})),
                    runtime_interval=5, full_interval=30, timeout=10, full_timeout=30, concurrency=4,
                )
                threading.Thread(target=server.serve_forever, daemon=True).start()
                port = server.server_port
                good = {"Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}", "X-District-Act": "1"}
                cases = [
                    {k: v for k, v in good.items() if k != "X-District-Act"},
                    {**good, "X-District-Act": "0"},
                    {k: v for k, v in good.items() if k != "Origin"},
                    {**good, "Origin": "http://evil.example"},
                    {**good, "Host": f"evil.example:{port}", "Origin": f"http://evil.example:{port}"},
                    {**good, "Host": "127.0.0.1:1"},
                    {**good, "Forwarded": "for=127.0.0.1;host=localhost"},
                    {**good, "X-Forwarded-For": "127.0.0.1"},
                ]
                try:
                    with patch.object(dash.Handler, "_stream", autospec=True,
                                      side_effect=lambda handler, argv: handler._send(200, "text/plain", b"executed")) as command:
                        for headers in cases:
                            for method, route in (("POST", "/api/act"), ("POST", "/api/future"), ("PUT", "/api/future"), ("DELETE", "/api/future")):
                                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                                connection.request(method, route, '{"args":["apply"]}', headers)
                                response = connection.getresponse()
                                self.assertEqual(response.status, 403, (method, route, headers))
                                response.read()
                                connection.close()
                        command.assert_not_called()
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    connection.request("POST", "/api/act", '{"args":["rm","not-registered"]}', good)
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertIn(b"[exit 1]", response.read())
                    connection.close()
                finally:
                    server.shutdown()

    def test_detect_and_read_do_not_disclose_configuration(self) -> None:
        import threading
        import urllib.error
        import urllib.parse
        import urllib.request
        from http.server import ThreadingHTTPServer

        secret = "ghp_SUPER_SECRET_CREDENTIAL"
        repo = self.repo(toml=f'[triage]\nurl="https://user:{secret}@example.com"\n')
        host.save({"repo": {"acme/widgets": {"path": str(repo), "triage": {"token": secret}}}})
        self.stub("factory", ("dashboard --json", json.dumps(dashboard(errors=[secret]))),
                  ("doctor --json", secret))
        with ThreadingHTTPServer(("127.0.0.1", 0), dash.Handler) as server:
            server.collector = mock.Mock(
                read=mock.Mock(return_value=(1, status.fleet(host.load()))),
                runtime_interval=5, full_interval=30, timeout=10, full_timeout=30, concurrency=4,
            )
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                for path in ("/", "/api/fleet"):
                    body = urllib.request.urlopen(base + path).read().decode()
                    self.assertNotIn(secret, body)
                    self.assertNotIn(str(repo), body)
                target_url = base + "/api/detect?target=" + urllib.parse.quote(str(repo))
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(target_url)
                self.assertEqual(ctx.exception.code, 403)
                request = urllib.request.Request(target_url, headers={"X-District-Act": "1"})
                body = json.loads(urllib.request.urlopen(request).read())
                self.assertTrue(body["ok"], body)
                self.assertIn("adopt", body["output"])
                self.assertNotIn(secret, body["output"])
                self.assertNotIn(str(repo), body["output"])
                self.assertNotIn("doctor --json", self.calls("factory"))
                for target in ("/etc/passwd", "--help", "https://evil.example/repo", "https://user:password@github.com/a/b"):
                    request = urllib.request.Request(base + "/api/detect?target=" + urllib.parse.quote(target),
                                                     headers={"X-District-Act": "1"})
                    with self.assertRaises(urllib.error.HTTPError) as ctx:
                        urllib.request.urlopen(request)
                    self.assertEqual(ctx.exception.code, 400)
                    self.assertLess(len(ctx.exception.read()), 1024)
            finally:
                server.shutdown()

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
        server.collector = mock.Mock(
            read=mock.Mock(return_value=(1, status.fleet(host.load()))),
            runtime_interval=5, full_interval=30, timeout=10, full_timeout=30, concurrency=4,
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"

        page = urllib.request.urlopen(base + "/").read().decode()
        self.assertIn('"id": "f_widgets"', page)
        fleet = json.loads(urllib.request.urlopen(base + "/api/fleet").read())
        self.assertEqual(fleet["fleet"]["acme/widgets"]["assessment"], "unknown")
        self.assertIn("const KPIS", fleet["data"])
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(urllib.request.Request(base + "/api/act", data=b'{"args":["apply"]}', method="POST"))
        self.assertEqual(ctx.exception.code, 403)
        req = urllib.request.Request(base + "/api/act", data=b'{"args":["status"]}', method="POST", headers={"X-District-Act": "1", "Origin": base})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 400)
        req = urllib.request.Request(base + "/api/act", data=b'{"args":["apply","--reset","nope"]}', method="POST", headers={"X-District-Act": "1", "Origin": base})
        out = urllib.request.urlopen(req).read().decode()
        self.assertIn("$ district apply --reset nope", out)
        self.assertIn("nope is not registered", out)
        self.assertTrue(out.endswith("[exit 1]\n"), out)

    def test_install_writes_units(self) -> None:
        code, out = self.district("dashboard", "--install", "--port", "8761")
        self.assertEqual(code, 0, out)
        names = sorted(p.name for p in host.unit_dir().iterdir())
        self.assertEqual(
            names,
            [
                "district-apply.service",
                "district-apply.timer",
                "district-dashboard.service",
                "district-metrics.service",
                "district-metrics.timer",
            ],
        )
        dashboard_service = (host.unit_dir() / "district-dashboard.service").read_text()
        self.assertIn(f"Environment=PATH={os.environ['PATH']}", dashboard_service)
        self.assertIn(f"ExecStart={sys.executable} -m district dashboard --port 8761 --no-open", dashboard_service)
        apply_service = (host.unit_dir() / "district-apply.service").read_text()
        self.assertIn("Type=oneshot", apply_service)
        self.assertIn(f"Environment=PATH={os.environ['PATH']}", apply_service)
        self.assertIn(f"ExecStart={sys.executable} -m district apply\n", apply_service)
        self.assertNotIn("--upgrade", apply_service)
        apply_timer = (host.unit_dir() / "district-apply.timer").read_text()
        self.assertIn("OnBootSec=10min", apply_timer)
        self.assertIn("OnUnitActiveSec=1h", apply_timer)
        self.assertIn("WantedBy=timers.target", apply_timer)
        self.assertIn("OnUnitActiveSec=1h", (host.unit_dir() / "district-metrics.timer").read_text())
        self.assertIn("metrics --refresh", (host.unit_dir() / "district-metrics.service").read_text())
        calls = self.calls("systemctl")
        self.assertIn("enable --now district-dashboard.service", calls)
        self.assertIn("enable --now district-apply.timer", calls)
        self.assertIn("enable --now district-metrics.timer", calls)
        self.assertLess(calls.index("daemon-reload"), calls.index("enable --now district-apply.timer"))


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

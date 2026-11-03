"""Drain flag, operator controls, diagnostics, signals, and self-update integration."""

from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import signal
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from district import doctor, drain, host, metrics, update


UTC = timezone.utc


class DrainFlagTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.tmp / "home"),
            "XDG_STATE_HOME": str(self.tmp / "state"),
            "XDG_CACHE_HOME": str(self.tmp / "cache"),
        }))

    def payload(self, *, created: datetime, expires: datetime) -> dict:
        return {
            "schema_version": 1,
            "requested_by": "district apply",
            "reason": "engine upgrade",
            "pid": os.getpid(),
            "created_at": created.isoformat(),
            "expires_at": expires.isoformat(),
        }

    def write_payload(self, value: object, *, mtime: datetime | None = None) -> Path:
        path = drain.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value) if not isinstance(value, str) else value)
        if mtime is not None:
            os.utime(path, (mtime.timestamp(), mtime.timestamp()))
        return path

    def test_local_path_uses_xdg_state_home_and_home_fallback(self) -> None:
        self.assertEqual(drain.path(), self.tmp / "state" / "district" / "drain")
        with mock.patch.dict(os.environ, {"HOME": str(self.tmp / "other-home")}, clear=True):
            self.assertEqual(
                drain.path(), self.tmp / "other-home" / ".local" / "state" / "district" / "drain")

    def test_flag_contract_matches_factory(self) -> None:
        destination = drain.path()
        replaced = []
        real_replace = os.replace

        def replace(source, target):
            source, target = Path(source), Path(target)
            self.assertEqual(target, destination)
            self.assertEqual(source.parent, destination.parent)
            self.assertFalse(destination.exists(), "the final name must not expose a partial flag")
            replaced.append(source)
            real_replace(source, target)

        with mock.patch.object(drain.os, "replace", side_effect=replace):
            returned = drain.request(timeout=10 * 60 * 60, reason="replace engine", requested_by="test")

        self.assertEqual(json.loads(destination.read_text()), returned)
        self.assertEqual(set(returned), {
            "schema_version", "requested_by", "reason", "pid", "created_at", "expires_at",
        })
        self.assertEqual((returned["schema_version"], returned["requested_by"], returned["reason"]),
                         (1, "test", "replace engine"))
        self.assertEqual(returned["pid"], os.getpid())
        created = datetime.fromisoformat(returned["created_at"])
        expires = datetime.fromisoformat(returned["expires_at"])
        self.assertEqual((created.utcoffset(), expires.utcoffset()), (timedelta(0), timedelta(0)))
        self.assertEqual(expires - created, timedelta(seconds=drain.DRAIN_MAX_TTL))
        self.assertEqual(len(replaced), 1)
        self.assertFalse(replaced[0].exists())

    def test_default_expiry_is_timeout_plus_fifteen_minutes_and_clear_is_idempotent(self) -> None:
        value = drain.request()
        created = datetime.fromisoformat(value["created_at"])
        expires = datetime.fromisoformat(value["expires_at"])
        self.assertEqual(expires - created, timedelta(seconds=drain.DEFAULT_TIMEOUT + 15 * 60))
        drain.clear()
        drain.clear()
        self.assertFalse(drain.path().exists())
        self.assertIsNone(drain.status())

    def test_status_explains_active_expired_malformed_stale_future_and_over_cap_flags(self) -> None:
        now = datetime.now(UTC)
        bad_schema = self.payload(created=now, expires=now + timedelta(hours=1))
        bad_schema["schema_version"] = True
        cases = (
            (self.payload(created=now - timedelta(minutes=1), expires=now + timedelta(minutes=1)), None, "active"),
            (self.payload(created=now - timedelta(hours=1), expires=now - timedelta(seconds=1)), None, "expired"),
            ("{not json", now, "malformed"),
            ("{not json", now - timedelta(seconds=drain.DRAIN_MAX_TTL + 1), "stale"),
            ("{not json", now + timedelta(minutes=5), "future"),
            (self.payload(created=now, expires=now + timedelta(hours=9)), now, "over-cap"),
            (bad_schema, now, "malformed"),
        )
        for value, mtime, diagnosis in cases:
            with self.subTest(diagnosis=diagnosis):
                drain.clear()
                self.write_payload(value, mtime=mtime)
                message = drain.status()
                self.assertIsNotNone(message)
                self.assertIn(diagnosis, message.lower())
                self.assertIn("clear", message.lower())
                self.assertTrue(any(word in message.lower() for word in ("expir", "ignore", "block")), message)

    def test_status_diagnoses_unreadable_flag(self) -> None:
        target = self.write_payload("{}")
        original = Path.read_text

        def read_text(path, *args, **kwargs):
            if path == target:
                raise PermissionError("denied")
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", read_text):
            message = drain.status()
        self.assertIn("unreadable", message.lower())
        self.assertIn("clear", message.lower())
        with mock.patch.object(drain, "_snapshot", return_value=("{}", float("inf"))):
            message = drain.status()
        self.assertIn("unreadable", message.lower())

    def test_remote_request_and_clear_resolve_remote_xdg_state_and_use_bounded_ssh(self) -> None:
        calls = []
        remote_state = self.tmp / "remote-state"
        remote_env = {**os.environ, "XDG_STATE_HOME": str(remote_state)}

        def transport(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.run(
                ["sh", "-c", argv[-1]], capture_output=True, text=True,
                env=remote_env, input=kwargs.get("input"), timeout=kwargs.get("timeout"),
                check=False,
            )

        runner = host.RemoteRunner("ultron", "factory@ultron.example", transport=transport)
        value = drain.request(runner, timeout=60)
        target = remote_state / "district/drain"
        self.assertEqual(json.loads(target.read_text()), value)

        drain.clear(runner)

        self.assertFalse(target.exists())
        self.assertTrue(all(argv[0:5] == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
                            and argv[5:8] == ["factory@ultron.example", "--", argv[-1]]
                            for argv, _ in calls))
        self.assertTrue(all(kwargs.get("timeout") is not None for _, kwargs in calls))


class DrainSignalTest(unittest.TestCase):
    def test_interrupted_converts_sigterm_and_sigint_to_shell_exit_and_restores_handlers(self) -> None:
        for signum in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=signum):
                previous = signal.getsignal(signum)
                with self.assertRaises(SystemExit) as raised:
                    with drain.interrupted():
                        handler = signal.getsignal(signum)
                        self.assertTrue(callable(handler))
                        handler(signum, None)
                self.assertEqual(raised.exception.code, 128 + signum)
                self.assertIs(signal.getsignal(signum), previous)


class DrainCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.tmp / "home"),
            "XDG_STATE_HOME": str(self.tmp / "state"),
            "XDG_CACHE_HOME": str(self.tmp / "cache"),
        }))


    def test_clear_holds_operation_lock_and_refuses_contention(self) -> None:
        with mock.patch.object(drain, "clear") as clear:
            self.assertEqual(drain.main(["--clear"]), 0)
        clear.assert_called_once_with(None)

        with mock.patch.object(drain.fcntl, "flock", side_effect=BlockingIOError), \
             mock.patch.object(drain, "clear") as clear, \
             contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(drain.main(["--clear"]), 1)
        clear.assert_not_called()
        self.assertIn("another District apply, update, or drain operation", errors.getvalue())

    def test_clear_can_target_a_registered_remote_host(self) -> None:
        data = {"hosts": {"ultron": {
            "ssh": "factory@ultron.example", "platform": "macos",
        }}}
        runner = mock.Mock(name="runner")
        with mock.patch.object(host, "load", return_value=data), \
             mock.patch.object(host, "remote_runner", return_value=runner) as remote_runner, \
             mock.patch.object(drain, "clear") as clear:
            self.assertEqual(drain.main(["--clear", "--host", "ultron"]), 0)
        remote_runner.assert_called_once_with("ultron", data["hosts"]["ultron"])
        clear.assert_called_once_with(runner)


class DrainDoctorTest(unittest.TestCase):
    def test_expired_flag_ignored_and_reported_by_doctor(self) -> None:
        backend = mock.Mock()
        backend.manager_status.return_value = "running"
        empty_rows = ("factory_rows", "toolchain_rows", "remote_config_rows", "parity_rows",
                      "repo_rows", "port_rows", "policy_rows", "unit_rows")
        with contextlib.ExitStack() as stack:
            root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            stack.enter_context(mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(root)}))
            flag = drain.request(timeout=1)
            created = datetime.now(UTC) - timedelta(hours=1)
            flag.update(created_at=created.isoformat(), expires_at=(created + timedelta(minutes=1)).isoformat())
            drain.path().write_text(json.dumps(flag))
            stack.enter_context(mock.patch.object(doctor.host, "load", return_value={}))
            stack.enter_context(mock.patch.object(doctor.host, "hosts", return_value={}))
            stack.enter_context(mock.patch.object(doctor.host, "repos", return_value={}))
            stack.enter_context(mock.patch.object(doctor.host, "service_backend", return_value=backend))
            stack.enter_context(mock.patch.object(doctor.host, "default", return_value=str(Path.home())))
            stack.enter_context(mock.patch.object(doctor, "gh_auth", return_value=doctor.row("PASS", "gh auth", "ok")))
            for name in empty_rows:
                stack.enter_context(mock.patch.object(doctor, name, return_value=[]))
            rows = doctor.check()
        warning = next(item for item in rows if item["label"] == "drain")
        self.assertEqual(warning["status"], "WARN")
        self.assertIn("expired drain flag", warning["detail"])
        self.assertIn("no longer blocks Factory", warning["detail"])
        self.assertIn("district drain --clear", warning["detail"])


class MetricsLockTest(unittest.TestCase):
    def test_metrics_can_run_while_apply_holds_long_drain_lock(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(root / "cache")}):
            operation = host.operation_lock_path()
            operation.parent.mkdir(parents=True)
            with operation.open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with mock.patch.object(metrics.host, "load", return_value={}), \
                     mock.patch.object(metrics.host, "select", return_value={}), \
                     contextlib.redirect_stdout(io.StringIO()) as output:
                    code = metrics.main([])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), {})


class UpdateDrainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.lock = self.tmp / "cache" / "district" / "apply.lock"
        self.metrics_lock = self.tmp / "cache" / "district" / "metrics.lock"
        self.marker = self.tmp / "apply.json"
        self.entrypoint = self.tmp / "district"
        self.entrypoint.write_text(
            "#!/usr/bin/python3\n"
            "import fcntl,json,os,sys\n"
            "available = {}\n"
            "for name,key in (('operation','DRAIN_TEST_LOCK'),('metrics','DRAIN_TEST_METRICS_LOCK')):\n"
            " with open(os.environ[key], 'a') as lock:\n"
            "  try:\n"
            "   fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB); available[name]=True\n"
            "  except BlockingIOError: available[name]=False\n"
            "open(os.environ['DRAIN_TEST_MARKER'], 'w').write(json.dumps({'argv':sys.argv[1:], **available}))\n"
            "raise SystemExit(0 if all(available.values()) else 9)\n"
        )
        self.entrypoint.chmod(0o755)
        self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.tmp / "home"),
            "XDG_STATE_HOME": str(self.tmp / "state"),
            "XDG_CACHE_HOME": str(self.tmp / "cache"),
            "DRAIN_TEST_LOCK": str(self.lock),
            "DRAIN_TEST_METRICS_LOCK": str(self.metrics_lock),
            "DRAIN_TEST_MARKER": str(self.marker),
        }))

    def plan(self, *, dry_run: bool = False) -> tuple[dict, dict]:
        return ({
            "ok": True,
            "warnings": [],
            "status": "plan",
            "mode": "update",
            "installed": {"sha": "a" * 40, "version": "1"},
            "target": {"sha": "b" * 40, "version": "2"},
            "ci": {"eligible": True, "status": "success"},
            "services": {},
            "operations": [],
            "dry_run": dry_run,
        }, {
            "root": self.tmp / "update",
            "entrypoint": self.entrypoint,
        })

    def invoke(self, plan: dict, context: dict, result: dict, *argv: str) -> tuple[int, dict]:
        output = io.StringIO()
        with mock.patch.object(update, "recover_pending", return_value=None), \
             mock.patch.object(update, "make_plan", return_value=(plan, context)), \
             mock.patch.object(update, "execute", return_value=result), \
             contextlib.redirect_stdout(output):
            code = update.main(list(argv))
        return code, json.loads(output.getvalue())

    def test_successful_self_update_starts_drained_apply_after_releasing_operation_lock(self) -> None:
        plan, context = self.plan()
        result = {"ok": True, "status": "updated", "current": plan["target"]}
        code, emitted = self.invoke(plan, context, result, "--yes", "--json")

        self.assertEqual(code, 0, emitted)
        called = json.loads(self.marker.read_text())
        self.assertEqual(called["argv"], ["apply", "--drain"])
        self.assertTrue(called["operation"], "the new apply must run outside update's operation lock")
        self.assertTrue(called["metrics"], "the new apply must run outside update's metrics lock")

    def test_update_excludes_manual_metrics_during_transaction(self) -> None:
        plan, context = self.plan()
        lock_path = metrics.lock_path()
        lock_path.parent.mkdir(parents=True)
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            output = io.StringIO()
            with mock.patch.object(update, "recover_pending", return_value=None), \
                 mock.patch.object(update, "make_plan", return_value=(plan, context)), \
                 mock.patch.object(update, "execute") as execute, \
                 contextlib.redirect_stdout(output):
                code = update.main(["--yes", "--json"])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertIn("metrics operation", result["error"])
        execute.assert_not_called()


    def test_dry_run_and_failed_update_never_start_drain(self) -> None:
        plan, context = self.plan(dry_run=True)
        code, emitted = self.invoke(plan, context, {"ok": True}, "--dry-run", "--json")
        self.assertEqual((code, emitted["status"]), (0, "plan"))
        self.assertFalse(self.marker.exists())
        self.assertFalse(drain.path().exists())

        plan, context = self.plan()
        result = {"ok": False, "status": "prepare-failed", "error": "failed"}
        code, emitted = self.invoke(plan, context, result, "--yes", "--json")
        self.assertEqual((code, emitted["status"]), (1, "prepare-failed"))
        self.assertFalse(self.marker.exists())
        self.assertFalse(drain.path().exists())

    def test_up_to_date_update_still_drains_pending_engine_but_dry_run_does_not(self) -> None:
        self.lock.parent.mkdir(parents=True)
        plan, context = self.plan()
        plan["target"] = plan["installed"]
        code, emitted = self.invoke(plan, context, {"ok": True}, "--yes", "--json")
        self.assertEqual((code, emitted["status"]), (0, "up-to-date"))
        self.assertTrue(self.marker.exists())
        self.marker.unlink()
        plan, context = self.plan(dry_run=True)
        plan["target"] = plan["installed"]
        code, emitted = self.invoke(plan, context, {"ok": True}, "--dry-run", "--json")
        self.assertEqual(code, 0)
        self.assertFalse(self.marker.exists())

    def test_update_signal_reaches_child_and_waits_for_drain_clear(self) -> None:
        self.entrypoint.write_text(
            "#!/usr/bin/python3\n"
            "import os,pathlib,signal,time\n"
            "flag=pathlib.Path(os.environ['DRAIN_TEST_MARKER'])\n"
            "def stop(signum,frame):\n"
            " flag.unlink(missing_ok=True)\n"
            " raise SystemExit(128+signum)\n"
            "signal.signal(signal.SIGTERM,stop)\n"
            "flag.write_text('active drain')\n"
            "os.kill(int(os.environ['DRAIN_TEST_PARENT']),signal.SIGTERM)\n"
            "time.sleep(10)\n"
        )
        with mock.patch.dict(os.environ, {"DRAIN_TEST_PARENT": str(os.getpid())}):
            with self.assertRaises(SystemExit) as caught:
                update.drained_apply(self.entrypoint)
        self.assertEqual(caught.exception.code, 128 + signal.SIGTERM)
        self.assertFalse(self.marker.exists(), "update returned before the child cleared its drain")


if __name__ == "__main__":
    unittest.main()

"""The host file `$XDG_CONFIG_HOME/factory/config.toml` is the registry.

Factory reads `[defaults.*]` and `[repo."owner/name".*]` (host-owned
tables only); District keeps its own keys beside them: `[defaults]`
clone_dir / factory_source / factory_ref (`latest-odd`, tag, or branch) /
factory_extras / min_package_age / max_failed_passes, `[defaults.engine]` ref /
sha / previous / installed_at (local engine state), `[hosts.*]` SSH / platform /
root / optional factory source / Xcode version / engine (remote engine state), and per repo `path`,
`host`, `toolchain`, `disabled_at`, `disabled_reason`. A repo is managed iff its table has a `path`.
"""

from __future__ import annotations

import base64
import copy
import json
import os
import shlex
import signal
import subprocess
import sys
import tomllib
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

import tomli_w
from district import service
from district.service import service_path

BASE_PORT = 8764  # first allocation is 8765, agent-factory's default
DEFAULTS = {
    "clone_dir": "~/dev/mikeroysoft",
    "factory_source": "~/dev/mikeroysoft/factory",
    "factory_extras": [],
    "min_package_age": "24h",
    "max_failed_passes": 10,
}
SSH_CONNECT_TIMEOUT = 10
# The supervisor lives on the remote host, outside the command's process group.
# It kills descendants even if the controller loses its SSH client.
REMOTE_DEADLINE = """\
import os, signal, subprocess, sys
limit, marker, command = sys.argv[1:]
signal.signal(signal.SIGHUP, signal.SIG_IGN)
child = subprocess.Popen(["/bin/sh", "-c", command], start_new_session=True,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
try:
    stdout, stderr = child.communicate(timeout=float(limit))
except subprocess.TimeoutExpired:
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    stdout, stderr = child.communicate()
    sys.stdout.write(stdout)
    sys.stderr.write(stderr)
    sys.stderr.write("\\n" + marker + "\\n")
    raise SystemExit(124)
sys.stdout.write(stdout)
sys.stderr.write(stderr)
raise SystemExit(child.returncode)
"""


class DistrictError(SystemExit):
    def __init__(self, msg: str) -> None:
        super().__init__(f"district: {msg}")


class RemoteHostError(RuntimeError):
    def __init__(self, host_name: str, detail: str) -> None:
        self.host_name = host_name
        self.detail = detail
        super().__init__(f"host {host_name} unreachable: {detail}")

def path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "factory" / "config.toml"


def unit_dir() -> Path:
    return path().parents[1] / "systemd" / "user"


def operation_lock_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "district" / "apply.lock"


def load() -> dict:
    p = path()
    if not p.exists():
        return {}
    try:
        data = tomllib.loads(p.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise DistrictError(f"{p}: {exc}") from exc
    if "port" in data.get("defaults", {}).get("dashboard", {}):
        raise DistrictError(f"{p}: [defaults.dashboard].port is not allowed; ports are allocated per repo")
    return data


def save(data: dict) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".toml.tmp")
    tmp.write_text(tomli_w.dumps(data))
    os.replace(tmp, p)


def default(data: dict, key: str):
    return data.get("defaults", {}).get(key, DEFAULTS[key])

def factory_source(data: dict, name: str = "local") -> str | None:
    if name != "local":
        table = data.get("hosts", {}).get(name, {})
        if "factory_source" in table:
            return str(table["factory_source"])
    source = str(default(data, "factory_source"))
    return None if name != "local" and Path(source).is_absolute() else source


def repos(data: dict) -> dict[str, dict]:
    return {slug: t for slug, t in data.get("repo", {}).items() if isinstance(t, dict) and "path" in t}


def hosts(data: dict) -> dict[str, dict]:
    configured = data.get("hosts", {})
    if not isinstance(configured, dict):
        raise DistrictError("[hosts] must be a table")
    for name, table in configured.items():
        if name == "local":
            raise DistrictError("[hosts.local] is reserved for this machine")
        if not isinstance(table, dict):
            raise DistrictError(f"[hosts.{name}] must be a table")
        if not isinstance(table.get("ssh"), str) or not table["ssh"].strip():
            raise DistrictError(f"[hosts.{name}].ssh must be a non-empty SSH target")
        if table.get("platform") not in ("linux", "macos"):
            raise DistrictError(f"[hosts.{name}].platform must be `linux` or `macos`")
        factory_source = table.get("factory_source")
        if factory_source is not None and (
            not isinstance(factory_source, str) or not factory_source.strip()
        ):
            raise DistrictError(f"[hosts.{name}].factory_source must be a non-empty string")
        xcode_version = table.get("xcode_version")
        if xcode_version is not None and (
            not isinstance(xcode_version, str) or not xcode_version.strip()
        ):
            raise DistrictError(f"[hosts.{name}].xcode_version must be a non-empty string")
        defaults = table.get("defaults")
        if defaults is not None and not isinstance(defaults, dict):
            raise DistrictError(f"[hosts.{name}.defaults] must be a table")
        defaults_unset = table.get("defaults_unset", [])
        if not isinstance(defaults_unset, list) or any(
            not isinstance(item, str) or not item or "" in item.split(".")
            for item in defaults_unset
        ):
            raise DistrictError(
                f"[hosts.{name}].defaults_unset must be an array of dotted key strings"
            )
    return configured


CLAIMS_ISSUE = "https://github.com/mikeroySoft/factory/issues/526"
HOST_KEYS = ("host", "hosts", "triage_host")  # District-only: never synced into a factory config


def repo_hosts(table: dict, slug: str = "repo") -> list[str]:
    """Hosts that run `slug`'s factory: `hosts = [...]`, else the single `host` (default local)."""
    listed = table.get("hosts")
    if listed is None:
        return [table.get("host", "local")]
    if "host" in table:
        raise DistrictError(f"{slug}: set host or hosts, not both")
    if not isinstance(listed, list) or not listed or any(not isinstance(n, str) or not n for n in listed) \
            or len(set(listed)) != len(listed):
        raise DistrictError(f"{slug}: hosts must be a non-empty array of distinct host names")
    if len(listed) > 1 and table.get("triage_host") not in listed:
        raise DistrictError(f"{slug}: hosts lists {len(listed)} hosts; set triage_host to one of {', '.join(listed)}")
    return list(listed)


def primary_host(table: dict, slug: str = "repo") -> str:
    """The one host District reads a repo's state from: its triage host when several run it."""
    listed = repo_hosts(table, slug)
    return table["triage_host"] if len(listed) > 1 else listed[0]


def multi_host(table: dict, slug: str = "repo") -> bool:
    return len(repo_hosts(table, slug)) > 1


def claims_supported(data: dict, name: str) -> bool:
    """True when `name`'s installed factory coordinates ticket claims across hosts (factory#526)."""
    argv = ["factory", "claims", "--supported"]
    try:
        proc = run(argv, quiet=True) if name == "local" else remote_runner(name, hosts(data)[name])(argv, quiet=True)
    except (OSError, RemoteHostError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and proc.stdout.startswith("host-claims")


def factory_host(data: dict, slug: str) -> str | dict:
    table = repos(data).get(slug)
    if table is None:
        raise DistrictError(f"{slug} is not registered (district add)")
    configured = hosts(data)
    listed = repo_hosts(table, slug)
    for name in listed:
        if name != "local" and name not in configured:
            raise DistrictError(f"{slug}: host {name!r} is not defined in [hosts]")
    name = primary_host(table, slug)
    if name == "local":
        return "local"
    configured = hosts(data)
    if not isinstance(name, str) or name not in configured:
        raise DistrictError(f"{slug}: host {name!r} is not defined in [hosts]")
    return configured[name]


def is_remote(data: dict, slug: str) -> bool:
    """True when the controller runs none of `slug`'s factories (every listed host is remote)."""
    factory_host(data, slug)
    return "local" not in repo_hosts(repos(data)[slug], slug)


FLEET_KEYS = ("host_name", "triage_host")  # District-owned keys of a factory `[fleet]` table


def fleet_table(table: dict, name: str, slug: str = "repo") -> dict:
    """`[repo.SLUG.fleet]` for host `name`: the table's own fleet keys (e.g. claim_ttl) plus, when
    several hosts run the repo, this host's name and the triage host."""
    fleet = {key: value for key, value in table.get("fleet", {}).items() if key not in FLEET_KEYS}
    if len(repo_hosts(table, slug)) > 1:
        fleet.update(host_name=name, triage_host=table["triage_host"])
    return fleet


def sync_local_fleet(data: dict) -> bool:
    """The controller's own factory config is this registry, so keep `[repo.SLUG.fleet]` here for
    repos that run on local and other hosts. Returns True when `data` changed."""
    changed = False
    for slug, table in repos(data).items():
        fleet = fleet_table(table, "local", slug) if "local" in repo_hosts(table, slug) else \
            {key: value for key, value in table.get("fleet", {}).items() if key not in FLEET_KEYS}
        if fleet != table.get("fleet", {}):
            if fleet:
                table["fleet"] = fleet
            else:
                del table["fleet"]
            changed = True
    return changed


def repo_table(data: dict, slug: str) -> dict:
    return data.setdefault("repo", {}).setdefault(slug, {})


def next_port(data: dict) -> int:
    ports = [t.get("dashboard", {}).get("port", 0) for t in data.get("repo", {}).values() if isinstance(t, dict)]
    return max([BASE_PORT, *ports]) + 1


SHARED_TABLES = ("triage", "workers", "worker_wrap", "review", "manager", "install", "gate")  # never dashboard: port is per-repo


def dedupe(data: dict) -> list[str]:
    """Promote host values every registered repo agrees on into `[defaults]`; drop per-repo copies
    that equal the default. Returns `table.key` names promoted."""
    tables = list(repos(data).values())
    defaults = data.setdefault("defaults", {})
    promoted = []
    for name in SHARED_TABLES:
        keys = {k for t in tables for k in t.get(name, {})}
        for key in sorted(keys):
            vals = [t.get(name, {}).get(key, _MISSING) for t in tables]
            if key not in defaults.get(name, {}) and all(v == vals[0] for v in vals) and vals[0] is not _MISSING:
                defaults.setdefault(name, {})[key] = vals[0]
                promoted.append(f"{name}.{key}")
            default = defaults.get(name, {}).get(key, _MISSING)
            if default is _MISSING:
                continue
            for t in tables:
                if t.get(name, {}).get(key, _MISSING) == default:
                    del t[name][key]
                    if not t[name]:
                        del t[name]
    if not defaults:
        del data["defaults"]
    return promoted


_MISSING = object()


def unit_name(slug: str) -> str:
    """agent-factory's unit stem: `factory-<repo basename>`."""
    return f"factory-{slug.rsplit('/', 1)[-1]}"




def select(data: dict, slug: str | None) -> dict[str, dict]:
    """Registry entries to act on: all, or the one named (by slug or basename)."""
    all_repos = repos(data)
    if slug is None:
        return all_repos
    if slug in all_repos:
        return {slug: all_repos[slug]}
    hits = {s: t for s, t in all_repos.items() if s.rsplit("/", 1)[-1] == slug}
    if not hits:
        raise DistrictError(f"{slug} is not registered (district add)")
    return hits


# ---------------------------------------------------------------- subprocess


def run(argv: list[str], cwd: Path | None = None, check: bool = False, quiet: bool = False,
        timeout: float | None = None, env: dict[str, str] | None = None,
        input: str | None = None) -> subprocess.CompletedProcess:
    if timeout is None:
        proc = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, check=False, env=env, input=input,
        )
    else:
        # Collector deadlines cover the command's process group, not just its
        # Python wrapper: inherited output pipes can belong to a surviving child.
        with subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              stdin=subprocess.PIPE if input is not None else None,
                              text=True, start_new_session=True, env=env) as child:
            try:
                stdout, stderr = child.communicate(input=input, timeout=timeout)
            except BaseException:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
                raise
            proc = subprocess.CompletedProcess(argv, child.returncode, stdout, stderr)
    if check and proc.returncode != 0:
        if not quiet:
            sys.stdout.write(proc.stdout)
            sys.stderr.write(proc.stderr)
        raise DistrictError(f"`{' '.join(argv)}` exited {proc.returncode}")
    return proc


class RemotePath:
    def __init__(self, runner: RemoteRunner, value: str | PurePosixPath) -> None:
        self.runner = runner
        self.value = PurePosixPath(value)

    def __truediv__(self, part: str) -> RemotePath:
        return RemotePath(self.runner, self.value / part)

    @property
    def parent(self) -> RemotePath:
        return RemotePath(self.runner, self.value.parent)

    def __str__(self) -> str:
        return str(self.value)

    def __repr__(self) -> str:
        return f"RemotePath({self.runner.name!r}, {str(self)!r})"

    def __eq__(self, other: object) -> bool:
        return str(self) == str(other)

    def resolve(self) -> RemotePath:
        return self

    def exists(self) -> bool:
        script = "import os,sys; raise SystemExit(not os.path.exists(sys.argv[1]))"
        return self.runner(["python3", "-c", script, str(self)]).returncode == 0

    def mkdir(self, parents: bool = False, exist_ok: bool = False) -> None:
        script = "import pathlib,sys; pathlib.Path(sys.argv[1]).mkdir(parents=bool(int(sys.argv[2])), exist_ok=bool(int(sys.argv[3])))"
        self.runner(
            ["python3", "-c", script, str(self), str(int(parents)), str(int(exist_ok))],
            check=True,
        )

    def read_text(self) -> str:
        script = "import pathlib,sys; sys.stdout.write(pathlib.Path(sys.argv[1]).read_text())"
        proc = self.runner(["python3", "-c", script, str(self)])
        if proc.returncode:
            raise OSError(proc.stderr.strip() or f"cannot read {self}")
        return proc.stdout

    def read_bytes(self) -> bytes:
        script = "import base64,pathlib,sys; print(base64.b64encode(pathlib.Path(sys.argv[1]).read_bytes()).decode())"
        proc = self.runner(["python3", "-c", script, str(self)])
        if proc.returncode:
            raise OSError(proc.stderr.strip() or f"cannot read {self}")
        return base64.b64decode(proc.stdout)

    def write_text(self, body: str) -> None:
        script = (
            "import os,pathlib,sys; p=pathlib.Path(sys.argv[1]); p.parent.mkdir(parents=True,exist_ok=True); "
            "t=p.with_name('.'+p.name+'.district.tmp'); t.write_text(sys.stdin.read()); os.replace(t,p)"
        )
        self.runner(["python3", "-c", script, str(self)], input=body, check=True)

    def write_bytes(self, body: bytes) -> None:
        script = (
            "import base64,os,pathlib,sys; p=pathlib.Path(sys.argv[1]); p.parent.mkdir(parents=True,exist_ok=True); "
            "t=p.with_name('.'+p.name+'.district.tmp'); t.write_bytes(base64.b64decode(sys.stdin.read())); os.replace(t,p)"
        )
        self.runner(
            ["python3", "-c", script, str(self)],
            input=base64.b64encode(body).decode(),
            check=True,
        )

    def unlink(self, missing_ok: bool = False) -> None:
        script = "import pathlib,sys; pathlib.Path(sys.argv[1]).unlink(missing_ok=bool(int(sys.argv[2])))"
        self.runner(["python3", "-c", script, str(self), str(int(missing_ok))], check=True)


class RemoteRunner:
    def __init__(self, name: str, target: str, *, transport=run) -> None:
        self.name = name
        self.target = target
        self.transport = transport
        self._facts: dict | None = None

    def __call__(self, argv: list[str], cwd: Path | RemotePath | None = None,
                 check: bool = False, quiet: bool = False, timeout: float | None = None,
                 env: dict[str, str] | None = None, input: str | None = None) -> subprocess.CompletedProcess:
        if not quiet:
            print(f"{self.name}: $ {shlex.join(argv)}")
        command = shlex.join(argv)
        if env:
            assignments = " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
            command = f"env {assignments} {command}"
        if cwd is not None:
            command = f"cd {shlex.quote(str(cwd))} && {command}"
        marker = None
        if timeout is not None:
            marker = f"district-timeout-{uuid.uuid4().hex}"
            # Reserve time for connecting and returning the cleanup result within
            # the controller's deadline. The remote watchdog must expire first.
            limit = max(0, timeout - min(SSH_CONNECT_TIMEOUT, timeout / 2))
            command = shlex.join(["python3", "-c", REMOTE_DEADLINE, str(limit), marker, command])
        ssh = [
            "ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
            self.target, "--", command,
        ]
        try:
            proc = self.transport(ssh, timeout=timeout, input=input)
        except OSError as exc:
            raise RemoteHostError(self.name, str(exc)) from exc
        if marker and proc.returncode == 124 and marker in proc.stderr.splitlines():
            raise subprocess.TimeoutExpired(argv, timeout, output=proc.stdout, stderr=proc.stderr)
        if proc.returncode == 255:
            detail = (proc.stderr or proc.stdout).strip().splitlines()
            raise RemoteHostError(self.name, detail[-1] if detail else f"ssh exited {proc.returncode}")
        result = subprocess.CompletedProcess(argv, proc.returncode, proc.stdout, proc.stderr)
        if check and result.returncode:
            if not quiet:
                sys.stdout.write(result.stdout)
                sys.stderr.write(result.stderr)
            raise DistrictError(f"{self.name}: `{' '.join(argv)}` exited {result.returncode}")
        return result

    def facts(self) -> dict:
        if self._facts is None:
            script = (
                "import json,os; from pathlib import Path; "
                "home=Path.home(); config=Path(os.environ.get('XDG_CONFIG_HOME', home/'.config')); "
                "print(json.dumps({'home':str(home),'config_home':str(config),'path':os.environ.get('PATH',''),"
                "'uid':os.getuid()}))"
            )
            proc = self(["python3", "-c", script])
            if proc.returncode:
                raise DistrictError(
                    f"{self.name}: cannot inspect remote host: "
                    f"{(proc.stderr or proc.stdout).strip() or f'python3 exited {proc.returncode}'}"
                )
            try:
                self._facts = json.loads(proc.stdout)
            except (TypeError, ValueError) as exc:
                raise DistrictError(f"{self.name}: remote host returned invalid environment details") from exc
        return self._facts

    def path(self, value: str | PurePosixPath) -> RemotePath:
        return RemotePath(self, value)

    def which(self, name: str, path: str) -> str | None:
        script = "import shutil,sys; print(shutil.which(sys.argv[1], path=sys.argv[2]) or '')"
        proc = self(["python3", "-c", script, name, path])
        return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None

    @contextmanager
    def temporary_directory(self):
        proc = self(["mktemp", "-d", "-t", "district-engine-XXXXXX"], check=True)
        directory = proc.stdout.strip()
        if not directory:
            raise DistrictError(f"{self.name}: mktemp returned no directory")
        try:
            yield self.path(directory)
        finally:
            self(["rm", "-rf", "--", directory])


def remote_runner(name: str, table: dict) -> RemoteRunner:
    return RemoteRunner(name, table["ssh"])


def _merge(target: dict, overrides: dict) -> None:
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def _unset(target: dict, path: str) -> None:
    parents = []
    current = target
    parts = path.split(".")
    for key in parts[:-1]:
        child = current.get(key)
        if not isinstance(child, dict):
            return
        parents.append((current, key))
        current = child
    if parts[-1] not in current:
        return
    del current[parts[-1]]
    for parent, key in reversed(parents):
        if parent[key]:
            break
        del parent[key]


def _remote_defaults(data: dict, name: str, target: dict) -> dict:
    defaults = copy.deepcopy(data.get("defaults", {}))
    overrides = target.get("defaults", {})
    _merge(defaults, overrides)
    defaults.pop("engine", None)
    if "factory_source" not in overrides:
        source = factory_source(data, name)
        if source is None:
            defaults.pop("factory_source", None)
        elif "factory_source" in target:
            defaults["factory_source"] = source
    engine = target.get("engine")
    if engine is not None:
        defaults["engine"] = copy.deepcopy(engine)
    for path in target.get("defaults_unset", []):
        _unset(defaults, path)
    return defaults


def remote_config(data: dict, name: str) -> dict:
    result = {}
    target = data["hosts"][name]
    defaults = _remote_defaults(data, name, target)
    if defaults:
        result["defaults"] = defaults
    tables = {}
    for slug, table in repos(data).items():
        listed = repo_hosts(table, slug)
        if name in listed:
            tables[slug] = {key: copy.deepcopy(value) for key, value in table.items()
                            if key not in HOST_KEYS and key != "fleet"}
            fleet = fleet_table(table, name, slug)
            if fleet:
                tables[slug]["fleet"] = fleet
    if tables:
        result["repo"] = tables
    return result


def sync_remote_config(data: dict, name: str, runner: RemoteRunner) -> None:
    config = runner.path(PurePosixPath(runner.facts()["config_home"]) / "factory" / "config.toml")
    config.write_text(tomli_w.dumps(remote_config(data, name)))


def engine(data: dict, name: str) -> dict | None:
    container = data.get("defaults", {}) if name == "local" else data.get("hosts", {}).get(name, {})
    value = container.get("engine")
    return value if isinstance(value, dict) else None


def set_engine(data: dict, name: str, value: dict) -> None:
    container = data.setdefault("defaults", {}) if name == "local" else data["hosts"][name]
    container["engine"] = value


def _service_run(argv: list[str], *, check: bool = False,
                 timeout: float | None = None) -> subprocess.CompletedProcess:
    return run(argv, check=check, quiet=True, timeout=timeout)


def service_backend(target: str | dict = "local", *, run=None, **kwargs) -> service.ServiceBackend:
    """Select the service-manager adapter for the target host platform."""
    if target == "local":
        platform = "macos" if sys.platform == "darwin" else "linux"
    elif isinstance(target, dict):
        platform = target.get("platform")
    else:
        platform = None
    runner = _service_run if run is None else run
    if isinstance(runner, RemoteRunner):
        facts = runner.facts()
        if platform == "linux":
            kwargs.setdefault(
                "unit_dir",
                runner.path(PurePosixPath(facts["config_home"]) / "systemd" / "user"),
            )
        elif platform == "macos":
            kwargs.setdefault("home", runner.path(facts["home"]))
            kwargs.setdefault("uid", facts["uid"])
            kwargs.setdefault("raw_path", facts["path"])
    if platform == "linux":
        directory = kwargs.pop("unit_dir") if "unit_dir" in kwargs else unit_dir()
        return service.SystemdUserBackend(runner, unit_dir=directory, **kwargs)
    if platform == "macos":
        return service.LaunchdBackend(runner, **kwargs)
    raise DistrictError(f"unsupported service platform: {platform!r}")



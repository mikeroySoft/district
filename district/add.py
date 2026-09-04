"""`district add <path|url>`: onboard a repository, or adopt one that already has `.factory.toml`.

Host-owned keys never reach the committed file; repo-owned keys are written
exactly once, here. Afterwards the factory owns its floor.
"""

from __future__ import annotations

import argparse
import copy
import difflib
import json
import os
import re
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

import tomli_w

from district import host
from district.host import DistrictError, run

CONFIG_NAME = ".factory.toml"
# Same split as agent-factory's config.HOST_TABLES / HOST_KEYS; `factory doctor`
# reports these as "host settings committed" (the adopt signal).
HOST_TABLES = ("triage", "workers", "review", "install")
HOST_KEYS = {"dashboard": ("port",), "gate": ("lock",)}

HEADER = re.compile(r"^\s*(\[\[?)\s*([^\]]*?)\s*\]\]?\s*(?:#.*)?$")
KEY = re.compile(r"""^\s*([A-Za-z0-9_-]+|"[^"]*"|'[^']*')\s*=""")


# ---------------------------------------------------------------- adopt: span-aware key removal


class Refuse(Exception):
    """The file's shape is outside what line-span removal handles; write nothing."""


def scan(lines: list[str]) -> list[tuple[str | None, str | None, bool]]:
    """Per line: (table name, bare key or None, is_header). Root table is ''."""
    table: str | None = ""
    out = []
    for line in lines:
        m = HEADER.match(line)
        if m:
            table = m.group(2)
            out.append((table, None, True))
            continue
        k = KEY.match(line)
        out.append((table, k.group(1).strip("\"'") if k else None, False))
    return out


def table_span(marks: list, name: str) -> tuple[int, int]:
    """[start, end) of the `[name]` table: header through its last non-blank line."""
    starts = [i for i, (t, _, h) in enumerate(marks) if h and t == name]
    if len(starts) != 1:
        raise Refuse(f"[{name}] must be one plain table header (no inline table, dotted or quoted form)")
    start = starts[0]
    end = next((i for i in range(start + 1, len(marks)) if marks[i][2]), len(marks))
    return start, end


def lift(text: str) -> tuple[str, dict]:
    """Move host-owned keys out of a `.factory.toml` text; returns (new text, lifted values).

    Removes exactly the lines of the moved keys (whole table only when every key
    moved); every other byte stays. Raises Refuse rather than guess."""
    raw = tomllib.loads(text)
    lines = text.splitlines(keepends=True)
    marks = scan(lines)
    lifted: dict = {}
    drop: set[int] = set()
    expected = copy.deepcopy(raw)

    def drop_table(name: str) -> None:
        start, end = table_span(marks, name)
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
        drop.update(range(start, end))
        # one blank separator is enough once the table is gone
        if start > 0 and not lines[start - 1].strip() and end < len(lines) and not lines[end].strip():
            drop.add(end)

    def drop_key(name: str, key: str) -> None:
        start, end = table_span(marks, name)
        hits = [i for i in range(start + 1, end) if marks[i][1] == key]
        if len(hits) != 1:
            raise Refuse(f"{name}.{key} must be one plain `key = value` line")
        drop.add(hits[0])

    for name in HOST_TABLES:
        table = raw.get(name)
        if not table:
            continue
        if any(isinstance(v, dict) for v in table.values()):
            raise Refuse(f"[{name}] has sub-tables or inline tables")
        drop_table(name)
        lifted[name] = expected.pop(name)
    for name, keys in HOST_KEYS.items():
        table = raw.get(name, {})
        moved = [k for k in keys if k in table]
        if not moved:
            continue
        lifted[name] = {k: table[k] for k in moved}
        if set(table) == set(moved):
            drop_table(name)
            expected.pop(name)
        else:
            for k in moved:
                drop_key(name, k)
                expected[name].pop(k)
    new = "".join(line for i, line in enumerate(lines) if i not in drop)
    try:
        if tomllib.loads(new) != expected:
            raise Refuse("removal would change other keys")
    except tomllib.TOMLDecodeError as exc:
        raise Refuse(f"removal leaves an unparsable file ({exc}); a moved value spans several lines?") from exc
    return new, lifted


# ---------------------------------------------------------------- detection


def doctor(root: Path) -> dict:
    proc = run(["factory", "doctor", "--json"], cwd=root)
    try:
        return json.loads(proc.stdout)
    except ValueError:
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        raise DistrictError(f"`factory doctor --json` in {root} produced no report (exit {proc.returncode})") from None


def gh_json(*args: str) -> dict:
    proc = run(["gh", *args], check=True)
    return json.loads(proc.stdout)


def remote_slug(url: str) -> str | None:
    if "github.com" not in url:
        return None
    return url.rsplit("github.com", 1)[-1].strip(":/").removesuffix(".git").lower()


def remotes(root: Path) -> dict[str, str]:
    out = {}
    for line in run(["git", "remote", "-v"], cwd=root, check=True).stdout.splitlines():
        name, url, *_ = line.split()
        out.setdefault(name, url)
    return out


def ensure_upstream(root: Path, parent: str) -> str:
    """Name of the remote pointing at `parent`, adding `upstream` if none does."""
    have = remotes(root)
    for name, url in have.items():
        if remote_slug(url) == parent.lower():
            return name
    if "upstream" in have:
        raise DistrictError(f"remote `upstream` is {have['upstream']}, not the parent {parent}; fix it and re-run")
    run(["git", "remote", "add", "upstream", f"https://github.com/{parent}.git"], cwd=root, check=True)
    print(f"added remote upstream -> https://github.com/{parent}.git")
    return "upstream"


def fork_parent(slug: str) -> str | None:
    info = gh_json("repo", "view", slug, "--json", "isFork,parent")
    parent = (info.get("parent") or {}) if info.get("isFork") else {}
    # gh 2.x returns {name, owner:{login}}; older builds returned nameWithOwner
    return parent.get("nameWithOwner") or (f"{parent['owner']['login']}/{parent['name']}" if parent.get("owner") else None)


def propose_checks(root: Path) -> list[dict]:
    """Gate checks derivable from marker files; each names its source. Nothing is invented."""
    checks = []
    if (root / "Cargo.toml").exists():
        checks += [
            {"name": "fmt", "run": ["cargo", "fmt", "--check"], "source": "Cargo.toml"},
            {"name": "clippy", "run": ["cargo", "clippy", "--workspace", "--all-targets", "--", "-D", "warnings"], "source": "Cargo.toml"},
            {"name": "tests", "run": ["cargo", "test", "--workspace"], "source": "Cargo.toml"},
        ]
    if (root / "package.json").exists():
        try:
            scripts = json.loads((root / "package.json").read_text()).get("scripts", {})
        except ValueError:
            scripts = {}
        if "test" in scripts:
            checks.append({"name": "tests", "run": ["npm", "test"], "source": "package.json scripts.test"})
        if "lint" in scripts:
            checks.append({"name": "lint", "run": ["npm", "run", "lint"], "source": "package.json scripts.lint"})
    if (root / "pyproject.toml").exists():
        try:
            py = tomllib.loads((root / "pyproject.toml").read_text())
        except tomllib.TOMLDecodeError:
            py = {}
        deps = " ".join(py.get("project", {}).get("dependencies", []))
        for group in py.get("project", {}).get("optional-dependencies", {}).values():
            deps += " " + " ".join(group)
        for group in py.get("dependency-groups", {}).values():
            deps += " " + " ".join(d for d in group if isinstance(d, str))
        if "pytest" in deps:
            checks.append({"name": "tests", "run": ["pytest"], "source": "pyproject.toml (pytest dependency)"})
        if "ruff" in deps or "ruff" in py.get("tool", {}):
            checks.append({"name": "lint", "run": ["ruff", "check", "."], "source": "pyproject.toml (ruff)"})
    if (root / "Makefile").exists():
        if re.search(r"^test\s*:", (root / "Makefile").read_text(), re.M):
            checks.append({"name": "tests", "run": ["make", "test"], "source": "Makefile target `test`"})
    seen: set[str] = set()
    return [c for c in checks if not (c["name"] in seen or seen.add(c["name"]))]


def parse_check(spec: str) -> dict:
    name, _, cmd = spec.partition("=")
    argv = shlex.split(cmd)
    if not name or not argv:
        raise DistrictError(f"--check needs name=command, got {spec!r}")
    return {"name": name, "run": argv, "source": "--check"}


def installed_units(unit: str) -> dict:
    """[install] values that reproduce the units already on disk, so re-rendering changes nothing."""
    out: dict = {}
    timer = host.unit_dir() / f"{unit}.timer"
    if timer.exists() and (m := re.search(r"^OnUnitActiveSec=(\S+)", timer.read_text(), re.M)):
        out["every"] = m.group(1)
    dash = host.unit_dir() / f"{unit}-dashboard.service"
    if dash.exists():
        out["dashboard"] = True
        if m := re.search(r"--host (\S+)", dash.read_text()):
            out["host"] = m.group(1)
    return out


# ---------------------------------------------------------------- main


def resolve_target(arg: str, data: dict) -> Path:
    if "://" in arg or arg.startswith("git@"):
        name = arg.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        dest = Path(os.path.expanduser(host.default(data, "clone_dir"))) / name
        if not dest.exists():
            run(["gh", "repo", "clone", arg, str(dest)], check=True)
            print(f"cloned {arg} -> {dest}")
        return dest
    root = Path(arg).expanduser().resolve()
    if not (root / ".git").exists():
        raise DistrictError(f"{root} is not a git checkout")
    return root


def write_repo_file(path: Path, checks: list[dict], upstream: str | None) -> None:
    doc: dict = {}
    if upstream:
        doc["repo"] = {"upstream": upstream}
    doc["gate"] = {"check": [{"name": c["name"], "run": c["run"], "exclusive": c.get("exclusive", False)} for c in checks]}
    header = "# agent-factory configuration. Docs: https://github.com/mikeroySoft/factory\n"
    header += "# Gate checks proposed by District from: " + ", ".join(sorted({c["source"] for c in checks})) + "\n\n"
    path.write_text(header + tomli_w.dumps(doc))


def onboard(root: Path, repo_file: Path, args: argparse.Namespace, upstream: str | None) -> None:
    """Net-new: propose checks from markers (or --check), ask exclusivity, confirm in $EDITOR, write."""
    checks = [parse_check(s) for s in args.check] or propose_checks(root)
    if not checks:
        raise DistrictError(f"no Cargo.toml/package.json/pyproject.toml/Makefile in {root}; pass --check name=cmd")
    for c in checks:
        print(f"  check {c['name']}: {' '.join(c['run'])}   # from {c['source']}")
    if args.exclusive is not None:
        exclusive = {s.strip() for s in args.exclusive.split(",") if s.strip()}
    elif args.no_edit:
        exclusive = set()
    else:
        answer = input("checks needing single-tenant hardware (comma-separated names, empty for none): ")
        exclusive = {s.strip() for s in answer.split(",") if s.strip()}
    if unknown := exclusive - {c["name"] for c in checks}:
        raise DistrictError(f"--exclusive names unknown checks: {', '.join(sorted(unknown))}")
    for c in checks:
        c["exclusive"] = c["name"] in exclusive
    write_repo_file(repo_file, checks, upstream)
    try:
        if not args.no_edit:
            editor = shlex.split(os.environ.get("EDITOR") or "vi")
            if subprocess.run([*editor, str(repo_file)], check=False).returncode != 0:
                raise DistrictError("editor exited nonzero; nothing written")
        try:
            final = tomllib.loads(repo_file.read_text())
        except tomllib.TOMLDecodeError as exc:
            raise DistrictError(f"{CONFIG_NAME} is not valid TOML after editing ({exc}); nothing written") from None
        if not any(c.get("run") and c["run"] != ["true"] for c in final.get("gate", {}).get("check", [])):
            raise DistrictError("no [[gate.check]] with a real command; nothing written")
    except DistrictError:
        repo_file.unlink()
        raise


def dry_run(target: str, args: argparse.Namespace, data: dict) -> int:
    """Print what `add` would do — slug, port, fork parent, checks or lifted keys — and write nothing."""
    print("dry run: nothing written")
    if "://" in target or target.startswith("git@"):
        dest = Path(os.path.expanduser(host.default(data, "clone_dir"))) / target.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        if not dest.exists():
            slug = remote_slug(target)
            if not slug:
                raise DistrictError(f"{target} is not a GitHub URL")
            print(f"repo: {slug} (clone)\nwould clone {target} -> {dest}")
            parent = fork_parent(slug)
            print(f"fork of: {parent}" if parent else "not a fork")
            print("checks: detected after the clone")
            return 0
        target = str(dest)
    root = resolve_target(target, data)
    slug = doctor(root)["repo"]
    repo_file = root / CONFIG_NAME
    adopt = repo_file.exists()
    existing = tomllib.loads(repo_file.read_text()) if adopt else {}
    table = host.repos(data).get(slug, {})
    used = {t.get("dashboard", {}).get("port") for s, t in host.repos(data).items() if s != slug}
    port = table.get("dashboard", {}).get("port") or existing.get("dashboard", {}).get("port")
    print(f"repo: {slug} ({'adopt' if adopt else 'onboard'})\npath: {root}\ndashboard port: {port if port and port not in used else host.next_port(data)}")
    parent = fork_parent(slug)
    print(f"fork of: {parent}" if parent else "not a fork")
    if adopt:
        try:
            _, lifted = lift(repo_file.read_text())
            print("host keys to lift: " + (", ".join(lifted) or "none"))
        except Refuse as exc:
            print(f"cannot adopt: {exc}")
        checks = existing.get("gate", {}).get("check", [])
        print("checks (committed):" if checks else "checks: none committed")
        for c in checks:
            print(f"  {c.get('name', '?')}: {' '.join(c.get('run', []))}{'   # exclusive' if c.get('exclusive') else ''}")
    else:
        checks = [parse_check(s) for s in args.check] or propose_checks(root)
        print("checks (proposed):" if checks else "checks: none detected; pass --check name=cmd")
        for c in checks:
            print(f"  {c['name']}: {' '.join(c['run'])}   # from {c['source']}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="district add", description=__doc__.split("\n", 1)[0])
    parser.add_argument("target", help="path to a checkout, or a GitHub URL to clone")
    parser.add_argument("--check", action="append", default=[], metavar="NAME=CMD", help="gate check (repeatable); replaces detection")
    parser.add_argument("--exclusive", default=None, metavar="A,B", help="check names that need single-tenant hardware")
    parser.add_argument("--no-edit", action="store_true", help="do not ask or open $EDITOR")
    parser.add_argument("--dry-run", action="store_true", help="print the proposal (slug, port, fork parent, checks) and write nothing")
    args = parser.parse_args(argv)

    data = host.load()
    if args.dry_run:
        return dry_run(args.target, args, data)
    root = resolve_target(args.target, data)
    report = doctor(root)
    slug = report["repo"]
    registered = host.repos(data)
    for other in registered:
        if other != slug and host.unit_name(other) == host.unit_name(slug):
            raise DistrictError(f"{slug} would share unit {host.unit_name(slug)} with {other}; rename one checkout's origin")

    repo_file = root / CONFIG_NAME
    adopt = repo_file.exists()
    existing = tomllib.loads(repo_file.read_text()) if adopt else {}

    # 1. registry entry first: both paths depend on the port and path being persisted
    table = host.repo_table(data, slug)
    table["path"] = str(root)
    used = {t.get("dashboard", {}).get("port") for s, t in registered.items() if s != slug}
    port = table.get("dashboard", {}).get("port") or existing.get("dashboard", {}).get("port")
    if not port or port in used:
        port = host.next_port(data)
    table.setdefault("dashboard", {})["port"] = port
    host.save(data)
    print(f"registered {slug} at {root} (dashboard port {port})")

    # 2. fork detection
    parent = fork_parent(slug)
    upstream = ensure_upstream(root, parent) if parent else None
    if parent:
        print(f"fork of {parent}: upstream remote `{upstream}`")

    if adopt:
        old_text = repo_file.read_text()
        try:
            new_text, lifted = lift(old_text)
        except Refuse as exc:
            raise DistrictError(f"cannot adopt {repo_file}: {exc}; move the host settings by hand") from None
        for name, val in lifted.items():
            if name != "dashboard":  # port already decided above
                table[name] = val
        if units := installed_units(host.unit_name(slug)):
            table["install"] = {**table.get("install", {}), **units}
        if promoted := host.dedupe(data):
            print(f"shared by every repo, now in [defaults]: {', '.join(promoted)}")
        host.save(data)
        if new_text != old_text:
            diff = difflib.unified_diff(
                old_text.splitlines(keepends=True), new_text.splitlines(keepends=True), f"a/{CONFIG_NAME}", f"b/{CONFIG_NAME}",
            )
            sys.stdout.write("".join(diff))
            repo_file.write_text(new_text)
            print(f"moved {', '.join(lifted)} into {host.path()}")
        if parent and not existing.get("repo", {}).get("upstream"):
            print(f"WARN {CONFIG_NAME} does not set [repo].upstream = \"{upstream}\"; upstream sync stays off until it does")
    else:
        try:
            onboard(root, repo_file, args, upstream)
        except DistrictError:
            # nothing for `apply` to resume without a repo file: forget the entry
            del data["repo"][slug]
            if not data["repo"]:
                del data["repo"]
            host.save(data)
            raise
        print(f"wrote {repo_file}")

    # 3. the factory finishes its own onboarding; any failure leaves the registry entry so `apply` can resume
    for cmd in (["factory", "init", "--no-labels"], ["factory", "init", "--labels-only"], ["factory", "doctor"], ["factory", "install"]):
        proc = run(cmd, cwd=root)
        sys.stdout.write(proc.stdout)
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr)
            raise DistrictError(f"`{' '.join(cmd)}` exited {proc.returncode}; fix and run `district apply {slug}`")

    files = f"{CONFIG_NAME} .gitignore .github/ISSUE_TEMPLATE/agent_task.md"
    verb = "adopt" if adopt else "onboard"
    print(f"\ncommit in the repo (District never commits):\n  git -C {root} add {files} && git -C {root} commit -s -m 'factory: {verb} under District'")
    return 0

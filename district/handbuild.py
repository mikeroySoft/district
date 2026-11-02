"""Read-only bug packaging preflight and guarded execution of frozen hand steps.

Run with Factory's Python, which supplies the authoritative declaration and test
parsers. District itself does not depend on the Factory package.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import runpy
import subprocess
import sys
from pathlib import Path


class Refused(Exception):
    """Packaging cannot pass Factory's bug-repro gate."""


def _read(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise Refused(f"{' '.join(command)}: {(result.stderr or result.stdout).strip()}")
    return result.stdout


def _tests_at(ref: str, gate) -> dict:
    # Use Factory's pure parsers, not gate.timed: a preflight must never emit
    # lifecycle events, acquire a gate lock, create a worktree, or execute tests.
    tests = {}
    for path in _read(["git", "ls-tree", "-r", "--name-only", "-z", ref]).split("\0"):
        if not path.endswith((".py", ".rs")):
            continue
        source = _read(["git", "show", f"{ref}:{path}"])
        try:
            tests.update(gate._python_tests(path, source) if path.endswith(".py")
                         else gate._rust_tests(path, source))
        except SyntaxError:
            continue  # Factory's inventory also ignores unparseable Python files.
    return tests


def preflight(wt: Path, ticket: int, repo: str, base: str) -> None:
    from factory import dispatch, gate

    with contextlib.chdir(wt):
        issue = json.loads(_read([
            "gh", "issue", "view", str(ticket), "--repo", repo,
            "--json", "labels,body,comments",
        ]))
        labels = {label["name"] for label in issue["labels"]}
        if "bug" in labels:
            handoff = Path(".factory") / f"handoff-{ticket}.md"
            flags = dispatch.gate_flags(
                None, labels, issue.get("body") or "",
                handoff.read_text() if handoff.exists() else "",
                comments=issue.get("comments"),
            )
            if not flags.get("no_repro_test"):
                test_id = flags.get("repro_test")
                if not test_id:
                    raise Refused("bug ticket needs a Repro test: handoff line or No repro test: ticket line")
                revisions = _read([
                    "git", "rev-list", "--reverse", "--first-parent", f"{base}..HEAD",
                ]).split()
                if len(revisions) < 2:
                    raise Refused("test commit must come before the fix commit (at least two first-parent commits required)")
                before = _tests_at(base, gate)
                for revision in revisions:
                    added = any(
                        key not in before and gate._matches_test_id(test_id, record)
                        for key, record in _tests_at(revision, gate).items()
                    )
                    if added:
                        if revision == revisions[-1]:
                            raise Refused("test commit must come before the fix commit")
                        break
                else:
                    raise Refused(f"no commit before the fix adds declared reproduction test {test_id!r}")
    print("PREFLIGHT PASS", flush=True)


def _run_script(args) -> None:
    from factory import dispatch

    original = dispatch.run_gate

    def checked_gate(wt, n, *positional, **keywords):
        if Path(wt).resolve() != args.wt:
            raise Refused(f"gate worktree {wt} differs from preflight worktree {args.wt}")
        base = keywords.get("base") or args.base
        preflight(args.wt, args.ticket, args.repo, base)
        return original(wt, n, *positional, **{**keywords, "base": base})

    dispatch.run_gate = checked_gate
    previous_argv = sys.argv
    sys.argv = args.command
    try:
        runpy.run_path(args.command[0], run_name="__main__")
    finally:
        dispatch.run_gate = original
        sys.argv = previous_argv


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    for mode in ("preflight", "step", "run"):
        command = modes.add_parser(mode)
        command.add_argument("--wt", type=Path, required=True)
        command.add_argument("--ticket", type=int, required=True)
        command.add_argument("--repo", required=True)
        command.add_argument("--base", required=True)
        if mode != "preflight":
            command.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    args.wt = args.wt.resolve()
    if args.mode != "preflight":
        if args.command[:1] == ["--"]:
            args.command = args.command[1:]
        if not args.command:
            parser.error(f"{args.mode} requires a {'Python script' if args.mode == 'run' else 'command'}")
    try:
        preflight(args.wt, args.ticket, args.repo, args.base)
        if args.mode == "step":
            return subprocess.call(args.command)
        if args.mode == "run":
            _run_script(args)
        return 0
    except (Refused, OSError, ValueError, ImportError) as error:
        print(f"PREFLIGHT REFUSED: {error}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

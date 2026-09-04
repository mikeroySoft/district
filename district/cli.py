"""`district` command: one subcommand per fleet operation."""

from __future__ import annotations

import sys
from importlib import import_module

COMMANDS = {
    "add": ("add", "main", "onboard a repository (path or URL), or adopt one that has .factory.toml"),
    "apply": ("apply", "main", "reconcile the host to the registry; --upgrade reinstalls agent-factory"),
    "status": ("status", "main", "one-screen fleet table; nonzero if any factory is unhealthy"),
    "rm": ("rm", "main", "remove a repository's units and registry entry; repo files untouched"),
    "metrics": ("metrics", "main", "per-factory git/gh metrics (cached hourly); --refresh recollects"),
    "dashboard": ("dashboard", "main", "serve the District Atlas and management page (default port 8760)"),
}


def usage() -> str:
    width = max(len(c) for c in COMMANDS)
    lines = ["usage: district <command> [options]", "", "commands:"]
    lines += [f"  {name.ljust(width)}  {desc}" for name, (_, _, desc) in COMMANDS.items()]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        print(usage())
        return 0 if argv else 2
    if argv[0] in ("-V", "--version"):
        from district import __version__

        print(__version__)
        return 0
    entry = COMMANDS.get(argv[0])
    if not entry:
        print(f"district: unknown command `{argv[0]}`\n\n{usage()}", file=sys.stderr)
        return 2
    module, func, _ = entry
    return import_module(f"district.{module}").__dict__[func](argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())

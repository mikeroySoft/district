"""Remove unreferenced per-version Factory environments after engine reconciliation."""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import host

CLEANUP_TIMEOUT = 30

# Runs unchanged on remote hosts, where District is not installed.
_CLEANUP_SCRIPT = r'''
import json
import os
import plistlib
import re
import shutil
import stat
import sys
from pathlib import Path

MAX_REFERENCE_BYTES = 4 * 1024 * 1024
_PATH = re.compile(r"/(?:\\.|[^\s\"'<>\[\]{},;])+" )


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _within(path, directory):
    try:
        return os.path.commonpath((path, directory)) == directory
    except ValueError:
        return False


def _expanded(value, home):
    value = value.replace("${HOME}", home).replace("$HOME", home)
    return re.sub(r"(?<![A-Za-z0-9_])~(?=/)", home, value)


def _expanded_systemd(value, home):
    result = []
    index = 0
    while index < len(value):
        marker = value.find("%", index)
        if marker < 0:
            result.append(value[index:])
            break
        result.append(value[index:marker])
        if marker + 1 == len(value):
            raise ValueError("unresolved systemd specifier %")
        specifier = value[marker + 1]
        if specifier == "%":
            result.append("%")
        elif specifier == "h":
            result.append(home)
        else:
            raise ValueError(f"unresolved systemd specifier %{specifier}")
        index = marker + 2
    return "".join(result)


def _references(value, candidate, home):
    value = _expanded(value, home)
    logical, resolved = candidate
    for spelling in {logical, resolved}:
        if re.search(re.escape(spelling) + r"(?=$|[/\s\"'<>\[\]{},;:)])", value):
            return True
    for match in _PATH.finditer(value):
        token = match.group(0).replace(r"\ ", " ").rstrip(":)")
        if _within(os.path.realpath(token), resolved):
            return True
    if value.startswith("/") and _within(os.path.realpath(value), resolved):
        return True
    return False


def _read(path, errors, *, binary=False):
    try:
        size = path.stat().st_size
        if size > MAX_REFERENCE_BYTES:
            raise OSError(f"larger than {MAX_REFERENCE_BYTES} bytes")
        return path.read_bytes() if binary else path.read_text()
    except (OSError, UnicodeError) as exc:
        errors.append(f"{path}: {exc}")
        return None


def _source_path(path, references):
    references.append(str(path))
    try:
        references.append(str(path.resolve(strict=True)))
    except OSError:
        pass


def _present(path, errors):
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        errors.append(f"{path}: {exc}")
        return False


def _scan_directory(directory, select, errors):
    if not _present(directory, errors):
        return []
    try:
        return [path for path in sorted(directory.iterdir()) if select(path)]
    except OSError as exc:
        errors.append(f"{directory}: {exc}")
        return []


def _reference_scan(payload):
    home = payload["home"]
    config_home = Path(payload["config_home"])
    tool = Path(payload["tool"])
    errors = []
    references = list(_strings(payload.get("data", {})))

    unit_dir = config_home / "systemd" / "user"
    unit_entries = _scan_directory(
        unit_dir,
        lambda path: path.name.startswith("factory-")
        or path.name in ("service.d", "timer.d"),
        errors,
    )
    units = [path for path in unit_entries if path.suffix in (".service", ".timer")]
    drop_ins = [path for path in unit_entries
                if path.name in ("service.d", "timer.d")
                or path.name.endswith((".service.d", ".timer.d"))]
    for directory in drop_ins:
        units.extend(_scan_directory(directory, lambda path: path.suffix == ".conf", errors))
    for path in units:
        text = _read(path, errors)
        if text is not None:
            _source_path(path, references)
            try:
                references.append(_expanded_systemd(text, home))
            except ValueError as exc:
                errors.append(f"{path}: {exc}")

    agent_dir = Path(home) / "Library" / "LaunchAgents"
    plists = _scan_directory(agent_dir, lambda path: path.suffix == ".plist", errors)
    for path in plists:
        raw = _read(path, errors, binary=True)
        if raw is None:
            continue
        named_factory = "factory" in path.name.casefold()
        try:
            document = plistlib.loads(raw)
        except Exception as exc:
            if named_factory or b"factory" in raw.lower():
                errors.append(f"{path}: {exc}")
            continue
        label = document.get("Label") if isinstance(document, dict) else None
        if not named_factory and not (isinstance(label, str) and "factory" in label.casefold()):
            continue
        _source_path(path, references)
        references.extend(_strings(document))

    for path in (config_home / "factory" / "config.toml", tool / "uv-receipt.toml"):
        if _present(path, errors):
            text = _read(path, errors)
            if text is not None:
                _source_path(path, references)
                references.append(text)
    return references, errors


def _candidates(payload, errors):
    roots = [Path(payload["data_home"]) / "district", Path(payload["home"]) / ".local/share/district"]
    candidates = []
    seen_roots = set()
    seen_directories = set()
    for root in roots:
        key = os.path.normpath(str(root))
        if key in seen_roots:
            continue
        seen_roots.add(key)
        if not _present(root, errors):
            continue
        try:
            entries = sorted(root.iterdir())
        except OSError as exc:
            errors.append(f"{root}: {exc}")
            continue
        for path in entries:
            if not path.name.startswith("factory-") or path.name == "factory-":
                continue
            try:
                info = path.lstat()
            except OSError as exc:
                errors.append(f"{path}: {exc}")
                continue
            if stat.S_ISLNK(info.st_mode):
                candidates.append((path, None, "symlink"))
                continue
            if not stat.S_ISDIR(info.st_mode):
                continue
            resolved = os.path.realpath(path)
            if resolved in seen_directories:
                continue
            seen_directories.add(resolved)
            candidates.append((path, (info.st_dev, info.st_ino, resolved), None))
    return candidates


def clean(payload):
    errors = []
    candidates = _candidates(payload, errors)
    references, scan_errors = _reference_scan(payload)
    errors.extend(scan_errors)
    result = {"removed": [], "kept": [], "skipped": [], "scan_errors": errors, "remove_errors": []}
    if errors:
        return result

    home = payload["home"]
    tool = os.path.realpath(payload["tool"])
    removable = []
    for path, identity, reason in candidates:
        if reason:
            result["skipped"].append({"path": str(path), "reason": reason})
            continue
        device, inode, resolved = identity
        if _within(tool, resolved):
            result["skipped"].append({"path": str(path), "reason": "contains uv tool"})
        elif any(_references(value, (str(path), resolved), home) for value in references):
            result["kept"].append(str(path))
        else:
            removable.append((path, device, inode, resolved))

    # Reference discovery is complete before the first deletion. Recheck identity
    # at the destructive boundary so a path swap cannot redirect rmtree.
    for path, device, inode, resolved in removable:
        try:
            current = path.lstat()
            if (not stat.S_ISDIR(current.st_mode) or stat.S_ISLNK(current.st_mode)
                    or (current.st_dev, current.st_ino) != (device, inode)
                    or os.path.realpath(path) != resolved
                    or _within(tool, resolved)):
                result["skipped"].append({"path": str(path), "reason": "changed during cleanup"})
                continue
            shutil.rmtree(path)
            result["removed"].append(str(path))
        except OSError as exc:
            result["remove_errors"].append(f"{path}: {exc}")
    return result


if __name__ == "__main__":
    try:
        print(json.dumps(clean(json.load(sys.stdin)), separators=(",", ":")))
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
'''


def _local_facts(tool: object) -> dict[str, str]:
    home = Path.home()
    return {
        "home": str(home),
        "config_home": str(Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")),
        "data_home": str(Path(os.environ.get("XDG_DATA_HOME") or home / ".local/share")),
        "tool": str(tool),
    }


def _run_local(payload: dict) -> dict:
    namespace: dict[str, object] = {"__name__": "district.engine_cleanup.local"}
    exec(_CLEANUP_SCRIPT, namespace)
    return namespace["clean"](payload)  # type: ignore[operator]


def cleanup(data: dict, *, run_cmd=None) -> None:
    """Remove legacy directories only after a complete reference scan, including unit drop-ins."""
    if run_cmd is None:
        run_cmd = host.run
    remote = isinstance(run_cmd, host.RemoteRunner)
    tool, _ = host.factory_tool_paths(run_cmd if remote else None)
    facts = run_cmd.facts() if remote else _local_facts(tool)
    payload = {
        "home": str(facts["home"]),
        "config_home": str(facts["config_home"]),
        "data_home": str(facts["data_home"]),
        "tool": str(tool),
        "data": data,
    }
    if remote:
        proc = run_cmd(
            ["python3", "-c", _CLEANUP_SCRIPT],
            input=json.dumps(payload),
            timeout=CLEANUP_TIMEOUT,
            quiet=True,
        )
        if proc.returncode:
            detail = (proc.stderr or proc.stdout).strip() or f"python3 exited {proc.returncode}"
            raise host.DistrictError(f"{run_cmd.name}: legacy engine cleanup failed: {detail}")
        try:
            result = json.loads(proc.stdout)
        except (TypeError, ValueError) as exc:
            raise host.DistrictError(f"{run_cmd.name}: legacy engine cleanup returned invalid output") from exc
    else:
        result = _run_local(payload)

    if result["scan_errors"]:
        print("legacy engine cleanup skipped: reference scan incomplete: " + "; ".join(result["scan_errors"]))
        return
    for path in result["kept"]:
        print(f"kept referenced legacy engine {path}")
    for item in result["skipped"]:
        print(f"kept legacy engine {item['path']}: {item['reason']}")
    for path in result["removed"]:
        print(f"removed legacy engine {path}")
    for error in result["remove_errors"]:
        print(f"legacy engine cleanup could not remove {error}")

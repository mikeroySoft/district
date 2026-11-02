"""Build immutable, self-verifying hand-run snapshots."""
from __future__ import annotations

from collections.abc import Iterator
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import sys
import tempfile
from pathlib import Path


_MANIFEST = "checksums.txt"
_LIVE_SCRIPT = re.compile(
    # A complete token, not the slash suffix of ./x.sh, $dir/x.sh or lib/x.py.
    rb"(?:(?<![^\s\"'`=|;&(<>])|(?<=#!))"
    rb"(?P<path>(?:/|\$HOME/|\$\{HOME\}/|~/)[^\x00\r\n\t \"'`|;&()<>]*\.(?:sh|py))(?![A-Za-z0-9._-])"
)
_SHELL_WORD = rb"(?:\"(?:\\.|[^\"\\\r\n])*\"|'[^'\r\n]*'|\\[^\r\n]|[^\s\"'`;|&()<>\\])+"
# Overlapping matches retain literal sources nested inside another argument.
_SOURCE = re.compile(
    rb"(?=(?:^|[\s;|&()`\"'])(?P<command>" + _SHELL_WORD
    + rb")(?P<arguments>(?:[ \t]+" + _SHELL_WORD + rb")+))"
)
_SOURCE_REDIRECTION = re.compile(
    rb"(?=(?:^|[\s;|&()`\"'])(?P<command>" + _SHELL_WORD
    + rb")(?P<arguments>(?:[ \t]+" + _SHELL_WORD + rb")*)[ \t]*(?:[<>]|&>))"
)
_VERIFIER = r'''import hashlib, os, pathlib, stat, sys
root = pathlib.Path(sys.argv[1])
manifest = root / "checksums.txt"

def inventory():
    found = [root]
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                path = pathlib.Path(entry.path)
                if path == manifest:
                    continue
                found.append(path)
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
    return found

def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()

try:
    if manifest.is_symlink() or not manifest.is_file() or stat.S_IMODE(manifest.stat().st_mode) & 0o222:
        raise ValueError("unsafe checksum manifest")
    expected = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        checksum, relative = line.split("  ", 1)
        candidate = pathlib.PurePosixPath(relative)
        if (len(checksum) != 64 or any(c not in "0123456789abcdef" for c in checksum)
                or candidate.is_absolute() or ".." in candidate.parts or relative == "checksums.txt"
                or relative in expected):
            raise ValueError("invalid checksum manifest")
        expected[relative] = checksum
    actual = inventory()
    for path in actual:
        if path.is_symlink() or path.stat().st_mode & 0o222:
            raise ValueError("writable or symlink path: " + str(path))
    files = [path for path in actual if path.is_file()]
    names = {path.relative_to(root).as_posix() for path in files}
    if names != set(expected):
        raise ValueError("frozen run inventory changed")
    for path in files:
        name = path.relative_to(root).as_posix()
        if digest(path) != expected[name]:
            raise ValueError("checksum mismatch: " + name)
except Exception as error:
    print("frozen run checksum verification failed: " + str(error), file=sys.stderr)
    raise SystemExit(70)
'''


def _inside(path: Path, roots: list[Path]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def _existing(path: Path, label: str) -> Path:
    try:
        return path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ValueError(f"{label} does not resolve to an existing path: {path}") from error


def _roots(data: Path, includes: list[Path]) -> list[Path]:
    roots: set[Path] = set()
    try:
        children = list(data.iterdir())
    except OSError as error:
        raise ValueError(f"cannot read District data directory: {data}") from error
    for child in children:
        if child.name.startswith("handland-") and child.is_dir():
            if child.is_symlink():
                raise ValueError(f"source root may not be a symlink: {child}")
            roots.add(child.resolve())
    for include in includes:
        root = _existing(include, "include")
        if root == data or not root.is_relative_to(data) or root.is_relative_to(data / "runs"):
            raise ValueError(f"include must be a source directory below {data}, outside runs: {include}")
        if not root.is_dir():
            raise ValueError(f"include is not a directory: {include}")
        if Path(include).expanduser().absolute().resolve() != Path(include).expanduser().absolute():
            raise ValueError(f"include may not traverse symlinks: {include}")
        roots.add(root)
    ordered = sorted(roots, key=lambda path: (len(path.parts), str(path)))
    return [root for index, root in enumerate(ordered) if not _inside(root, ordered[:index])]


def _validate_sources(roots: list[Path]) -> None:
    for root in roots:
        for directory, dirnames, filenames in os.walk(root, followlinks=False):
            for name in dirnames + filenames:
                path = Path(directory, name)
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode):
                    raise ValueError(f"source symlink must be replaced with a regular frozen dependency: {path}")
                if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    raise ValueError(f"unsupported source path type: {path}")


def _replace_paths(path: Path, replacements: list[tuple[bytes, bytes]]) -> None:
    if not stat.S_ISREG(path.lstat().st_mode):
        return
    try:
        value = path.read_bytes()
    except OSError as error:
        raise ValueError(f"cannot read copied file: {path}") from error
    if b"\0" in value:
        return
    pattern = re.compile(b"|".join(re.escape(old) for old, _ in replacements) + rb"(?![A-Za-z0-9._-])")
    mapping = dict(replacements)
    changed = pattern.sub(lambda match: mapping[match[0]], value)
    if changed != value:
        path.chmod(stat.S_IMODE(path.stat().st_mode) | stat.S_IWUSR)
        path.write_bytes(changed)


def _command_kind(raw: bytes) -> str | None:
    command = shlex.split(os.fsdecode(raw), comments=False)
    if len(command) != 1:
        return None
    if command[0] in {"source", "."}:
        return "source"
    if command[0] == "cd":
        return "cd"
    name = Path(command[0]).name
    if re.fullmatch(r"(?:ba|da|k|z)?sh|python(?:[0-9]+(?:\.[0-9]+)*)?", name):
        return "python" if name.startswith("python") else "shell"
    return None


def _file_arguments(kind: str, words: list[str]) -> Iterator[tuple[str, str]]:
    index = 0
    while index < len(words) and words[index].startswith(("-", "+")):
        option = words[index]
        index += 1
        if option == "--":
            break
        if kind in {"shell", "python"}:
            if option.startswith("--"):
                name, separator, attached = option.partition("=")
                if name in {"--rcfile", "--init-file", "--check-hash-based-pycs"}:
                    if name in {"--rcfile", "--init-file"}:
                        if separator:
                            yield "shell-init", attached
                        elif index < len(words):
                            yield "shell-init", words[index]
                    if not separator:
                        index += 1
            else:
                for offset, flag in enumerate(option[1:], 1):
                    if flag == "c" or (kind == "python" and flag == "m"):
                        index = len(words)
                        break
                    if flag in ({"o", "O"} if kind == "shell" else {"X", "W"}):
                        if offset == len(option) - 1:
                            index += 1
                        break
    if index < len(words):
        yield kind, words[index]


def _source_references(value: bytes, script: Path) -> Iterator[tuple[str, str]]:
    value = value.replace(b"\\\n", b"")
    if b"$'" in value or b'$"' in value:
        raise ValueError(f"cannot inspect sourced dependencies in {script}: unsupported Bash quoting")
    for line in value.splitlines():
        # Ignore comment prose, but conservatively retain substitutions which
        # could execute inside a multiline quoted string beginning with '#'.
        if line.lstrip().startswith(b"#") and b"$(" not in line and b"`" not in line:
            continue
        for match in _SOURCE_REDIRECTION.finditer(line):
            kind = _command_kind(match.group("command"))
            if kind is None:
                continue
            prefix = re.sub(
                rb"(?:^|[ \t])(?:[0-9]+|\{[A-Za-z_][A-Za-z0-9_]*\})[ \t]*$",
                b"", match.group("arguments"),
            )
            words = shlex.split(os.fsdecode(prefix), comments=False)
            if not any(argument_kind == kind for argument_kind, _ in _file_arguments(kind, words)):
                raise ValueError(f"cannot inspect sourced dependencies in {script}: redirection before file argument")
        # Extract only the literal source argument. Tokenizing an entire Bash
        # statement as POSIX sh can hide sources inside substitutions or after
        # parameter expansions such as ${file##*/}.
        for match in _SOURCE.finditer(line):
            try:
                kind = _command_kind(match.group("command"))
                if kind is None:
                    continue
                words = shlex.split(os.fsdecode(match.group("arguments")), comments=False)
            except ValueError as error:
                raise ValueError(f"cannot inspect sourced dependencies in {script}: {error}") from error
            yield from _file_arguments(kind, words)


def _script_inputs(frozen_root: Path, entries: list[Path]) -> set[Path]:
    """Find script inputs, including literal sourced fragments with any name."""
    files = {path for path in frozen_root.rglob("*") if path.is_file()}
    scripts = set(entries)
    for path in files:
        if path.suffix in {".sh", ".py"} or path.read_bytes().startswith(b"#!"):
            scripts.add(path)
    pending = list(scripts)
    sourced: set[Path] = set()
    python_inputs: set[Path] = set()
    inspected: set[Path] = set()
    # Scripts can cd before sourcing. Conservatively search every copied
    # directory instead of assuming the entry script's initial working directory.
    working_dirs = {frozen_root} | {path for path in frozen_root.rglob("*") if path.is_dir()}
    while pending:
        script = pending.pop()
        if script in inspected:
            continue
        value = script.read_bytes()
        if b"\0" in value:
            continue
        first = value.split(b"\n", 1)[0]
        if script not in sourced and (
            script in python_inputs or script.suffix == ".py" or (first.startswith(b"#!") and b"python" in first.lower())
        ):
            continue
        inspected.add(script)
        for kind, text in _source_references(value, script):
            for prefix in ("$HOME/", "${HOME}/"):
                if text.startswith(prefix):
                    text = str(Path.home() / text[len(prefix):])
                    break
            if any(part in text for part in ("$", "`")):
                # Runtime-constructed dependencies cannot be discovered statically.
                continue
            if kind == "source" and "/" not in text:
                raise ValueError(f"cannot inspect sourced PATH lookup {text!r} in {script}; use an explicit ./ path")
            reference = Path(text).expanduser()
            candidates = {reference} if reference.is_absolute() else {
                directory / reference for directory in working_dirs
            }
            resolved = {candidate.resolve() for candidate in candidates}
            if kind == "cd":
                discovered = {candidate for candidate in resolved if candidate.is_dir()} - working_dirs
                if discovered:
                    working_dirs.update(discovered)
                    inspected.clear()
                    pending.extend(scripts)
                continue
            if not any(candidate.is_relative_to(frozen_root) for candidate in resolved):
                raise ValueError(f"cannot relocate live sourced script reference {str(reference)!r} in {script}")
            for candidate in resolved:
                if not candidate.is_relative_to(frozen_root):
                    if candidate.exists():
                        raise ValueError(f"cannot relocate live sourced script reference {str(reference)!r} in {script}")
                    continue
                if candidate in files:
                    scripts.add(candidate)
                    if kind == "python" and candidate not in python_inputs:
                        python_inputs.add(candidate)
                        pending.append(candidate)
                    elif kind != "python" and candidate not in sourced:
                        sourced.add(candidate)
                        pending.append(candidate)
    return scripts


def _reject_live_scripts(path: Path, frozen_root: Path) -> None:
    if not stat.S_ISREG(path.lstat().st_mode):
        return
    value = path.read_bytes()
    if b"\0" in value:
        return
    for match in _LIVE_SCRIPT.finditer(value):
        raw = match.group("path")
        if raw.startswith(b"/") and Path(os.fsdecode(raw)).is_relative_to(frozen_root):
            continue
        reference = os.fsdecode(raw)
        raise ValueError(
            f"cannot relocate live script reference {reference!r} in {path}; "
            "move it below the District data directory and include its directory"
        )


def _entry_command(source: Path, frozen: Path, args: list[str]) -> list[str]:
    try:
        first = frozen.read_bytes().splitlines()[:1]
    except OSError as error:
        raise ValueError(f"cannot read entry script: {source}") from error
    try:
        shebang = first[0][2:].decode("utf-8", "strict") if first and first[0].startswith(b"#!") else ""
        words = shlex.split(shebang) if shebang else []
    except (UnicodeError, ValueError) as error:
        raise ValueError(f"invalid script shebang: {source}") from error
    lowered = shebang.lower()
    if source.suffix == ".py" or "python" in lowered:
        command = words or [str(Path(sys.executable).resolve())]
    elif source.suffix == ".sh" or any(Path(word).name.endswith("sh") for word in words):
        command = words or ["/bin/bash"]
    else:
        raise ValueError(f"entry script must be a shell or Python script: {source}")
    return [*command, str(frozen), *args]


def _wrapper(run: Path, command: list[str], cwd: Path) -> str:
    python = shlex.quote(str(Path(sys.executable).resolve()))
    return (
        "#!/bin/sh\nset -eu\nunset PYTHONPATH PYTHONHOME || true\nexport PYTHONDONTWRITEBYTECODE=1\n"
        f"{python} -I - {shlex.quote(str(run))} <<'DISTRICT_VERIFY'\n"
        f"{_VERIFIER}DISTRICT_VERIFY\n"
        f"cd {shlex.quote(str(cwd))}\n"
        f"exec {' '.join(shlex.quote(part) for part in command)}\n"
    )


def _digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _inventory(root: Path) -> list[Path]:
    paths = [root]
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            path = Path(directory, name)
            if path != root / _MANIFEST:
                paths.append(path)
    return sorted(paths, key=lambda path: (len(path.parts), str(path)))


def _make_read_only(run: Path) -> None:
    paths = _inventory(run)
    for path in reversed(paths):
        if path != run and not path.is_symlink():
            path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)
    final_run_mode = stat.S_IMODE(run.stat().st_mode) & ~0o222
    lines = []
    for path in paths:
        if not path.is_file():
            continue
        relative = path.relative_to(run).as_posix()
        if any(character in relative for character in "\n\r\\"):
            raise ValueError(f"source path contains unsupported checksum characters: {path}")
        lines.append(f"{_digest(path)}  {relative}\n")
    manifest = run / _MANIFEST
    manifest.write_text("".join(lines), encoding="utf-8")
    manifest.chmod(stat.S_IMODE(manifest.stat().st_mode) & ~0o222)
    run.chmod(final_run_mode)


def _remove(run: Path) -> None:
    if not run.exists():
        return
    for directory, dirnames, _ in os.walk(run, topdown=False, followlinks=False):
        for name in dirnames:
            path = Path(directory, name)
            if not path.is_symlink():
                path.chmod(stat.S_IMODE(path.stat().st_mode) | stat.S_IWUSR)
        Path(directory).chmod(stat.S_IMODE(Path(directory).stat().st_mode) | stat.S_IWUSR)
    shutil.rmtree(run)


def freeze(data: Path, script: Path, stop_post: Path | None, includes: list[Path], args: list[str]) -> Path:
    """Copy a hand-run and its source trees into an immutable run directory."""
    data = _existing(data, "District data directory")
    if not data.is_dir():
        raise ValueError(f"District data path is not a directory: {data}")
    roots = _roots(data, includes)
    if not roots:
        raise ValueError(f"no handland or included source directories found below {data}")
    script_path = Path(script).expanduser().absolute()
    if script_path.is_symlink():
        raise ValueError(f"start script may not be a symlink: {script}")
    entries = [_existing(script, "start script")]
    if stop_post is not None:
        stop_path = Path(stop_post).expanduser().absolute()
        if stop_path.is_symlink():
            raise ValueError(f"stop script may not be a symlink: {stop_post}")
        entries.append(_existing(stop_post, "stop script"))
    for entry in entries:
        if not entry.is_file() or not _inside(entry, roots):
            raise ValueError(f"script must be a regular file in a copied source directory: {entry}")
    if any("\0" in value for value in args):
        raise ValueError("script arguments may not contain NUL bytes")
    _validate_sources(roots)

    runs = data / "runs"
    if runs.is_symlink() or (runs.exists() and not runs.is_dir()):
        raise ValueError(f"runs path must be a real directory: {runs}")
    runs.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="", dir=runs))
    binary = run / "bin"
    try:
        binary.mkdir()
        for root in roots:
            shutil.copytree(root, binary / root.relative_to(data), symlinks=True, copy_function=shutil.copy2)

        home = Path.home().resolve()
        mappings: dict[str, str] = {}
        for root in roots:
            destination = binary / root.relative_to(data)
            mappings[str(root)] = str(destination)
            if root.is_relative_to(home):
                suffix = root.relative_to(home).as_posix()
                mappings.update({f"$HOME/{suffix}": str(destination), f"${{HOME}}/{suffix}": str(destination), f"~/{suffix}": str(destination)})
        replacements = sorted(
            ((os.fsencode(old), os.fsencode(new)) for old, new in mappings.items()),
            key=lambda pair: len(pair[0]), reverse=True,
        )
        for path in binary.rglob("*"):
            if not path.is_symlink():
                _replace_paths(path, replacements)
        frozen_entries = [binary / entry.relative_to(data) for entry in entries]
        for path in sorted(_script_inputs(binary, frozen_entries)):
            _reject_live_scripts(path, binary)

        (run / "sources.json").write_text(
            json.dumps({"sources": [str(root) for root in roots]}, indent=2) + "\n", encoding="utf-8"
        )
        start_command = _entry_command(entries[0], frozen_entries[0], args)
        (run / "start").write_text(_wrapper(run, start_command, frozen_entries[0].parent), encoding="utf-8")
        (run / "start").chmod(0o755)
        if stop_post is not None:
            stop_command = _entry_command(entries[1], frozen_entries[1], [])
            (run / "stop").write_text(_wrapper(run, stop_command, frozen_entries[1].parent), encoding="utf-8")
            (run / "stop").chmod(0o755)
        _make_read_only(run)
        return run
    except BaseException:
        _remove(run)
        raise

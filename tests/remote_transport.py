"""Inspect the POSIX script inside RemoteRunner's shell-neutral SSH envelope."""
import base64
import shlex


def remote_command(ssh: list[str]) -> str:
    return base64.b64decode(shlex.split(ssh[-1])[-1]).decode()

"""Inspect the POSIX script inside RemoteRunner's shell-neutral SSH envelope."""
import base64
import json
import shlex
import subprocess
import sys

from district import host


def remote_command(ssh: list[str]) -> str:
    return base64.b64decode(shlex.split(ssh[-1])[-1]).decode()


def with_remote_python(transport):
    """Answer RemoteRunner's interpreter probe before delegating fake SSH commands."""
    def wrapped(ssh: list[str], **kwargs):
        if ssh[-1] == shlex.join(["/bin/sh", "-c", host.REMOTE_PYTHON]):
            result = json.dumps([sys.executable, sys.version_info[:3]]) + "\n"
            return subprocess.CompletedProcess(ssh, 0, result, "")
        return transport(ssh, **kwargs)

    return wrapped

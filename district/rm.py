"""`district rm <slug>`: stop and delete the units, drop the registry entry. Repo files are not touched."""

from __future__ import annotations

import argparse
import time

from district import host
from district.host import DistrictError


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="district rm", description=__doc__.split("\n", 1)[0])
    parser.add_argument("slug", help="owner/name or basename")
    parser.add_argument("--wait", action="store_true", help="block until a running pass finishes instead of refusing")
    parser.add_argument("--keep-units", action="store_true", help="only drop the registry entry")
    args = parser.parse_args(argv)

    data = host.load()
    ((slug, table),) = host.select(data, args.slug).items()
    host.factory_host(data, slug)
    names = host.repo_hosts(table, slug)
    runners = {}
    backends = {}
    for name in names:
        if name == "local":
            backends[name] = host.service_backend()
            continue
        runners[name] = host.remote_runner(name, host.hosts(data)[name])
        try:
            backends[name] = host.service_backend(host.hosts(data)[name], run=runners[name])
        except host.RemoteHostError as exc:
            raise DistrictError(f"{slug}: {exc}") from exc
    unit = host.unit_name(slug)
    if not args.keep_units and not args.wait and len(names) > 1:
        try:
            busy = [name for name, backend in backends.items()
                    if backend.is_active(f"{unit}.service") in ("active", "activating")]
        except host.RemoteHostError as exc:
            raise DistrictError(f"{slug}: {exc}") from exc
        if busy:
            raise DistrictError(f"{unit}.service is running a pass on {', '.join(busy)}; "
                                "wait for it or pass --wait (nothing removed)")
    if not args.keep_units:
        for name, backend in backends.items():
            try:
                _remove_units(backend, unit, args.wait)
            except host.RemoteHostError as exc:
                raise DistrictError(f"{slug}: {exc}") from exc
            except DistrictError as exc:
                raise DistrictError(f"{exc} (on {name})" if len(names) > 1 else str(exc)) from exc
    del data["repo"][slug]
    if not data["repo"]:
        del data["repo"]
    for name, runner in runners.items():
        try:
            host.sync_remote_config(data, name, runner)
        except host.RemoteHostError as exc:
            raise DistrictError(f"{slug}: {exc}") from exc
    host.save(data)
    print(f"removed {slug} from {host.path()}; its files are untouched (district add re-adopts)")
    return 0


def _remove_units(backend, unit: str, wait: bool) -> None:
    service = f"{unit}.service"
    if backend.is_active(service) in ("active", "activating") and not wait:
        raise DistrictError(f"{service} is running a pass; wait for it or pass --wait")
    backend.enable(f"{unit}.timer", False, check=False)
    while backend.is_active(service) in ("active", "activating"):
        if not wait:
            raise DistrictError(f"{service} is running a pass; wait for it or pass --wait")
        time.sleep(5)
    backend.enable(f"{unit}-dashboard.service", False, check=False)
    backend.remove(unit)
    backend.remove(f"{unit}-dashboard")
    left = [name for name in (f"{unit}.timer", f"{unit}-dashboard.service") if backend.status(name).loaded]
    if left:
        raise DistrictError(f"still loaded after removal: {', '.join(left)}")

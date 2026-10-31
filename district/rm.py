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
    host_name = table.get("host", "local")
    runner = None
    if host_name == "local":
        backend = host.service_backend()
    else:
        target = host.factory_host(data, slug)
        runner = host.remote_runner(host_name, target)
        try:
            backend = host.service_backend(target, run=runner)
        except host.RemoteHostError as exc:
            raise DistrictError(f"{slug}: {exc}") from exc
    unit = host.unit_name(slug)
    try:
        if not args.keep_units:
            service = f"{unit}.service"
            if backend.is_active(service) in ("active", "activating") and not args.wait:
                raise DistrictError(f"{service} is running a pass; wait for it or pass --wait")
            backend.enable(f"{unit}.timer", False, check=False)
            while backend.is_active(service) in ("active", "activating"):
                if not args.wait:
                    raise DistrictError(f"{service} is running a pass; wait for it or pass --wait")
                time.sleep(5)
            backend.enable(f"{unit}-dashboard.service", False, check=False)
            backend.remove(unit)
            backend.remove(f"{unit}-dashboard")
            left = [
                name for name in (f"{unit}.timer", f"{unit}-dashboard.service")
                if backend.status(name).loaded
            ]
            if left:
                raise DistrictError(f"still loaded after removal: {', '.join(left)}")
    except host.RemoteHostError as exc:
        raise DistrictError(f"{slug}: {exc}") from exc
    del data["repo"][slug]
    if not data["repo"]:
        del data["repo"]
    if runner is not None:
        try:
            host.sync_remote_config(data, host_name, runner)
        except host.RemoteHostError as exc:
            raise DistrictError(f"{slug}: {exc}") from exc
    host.save(data)
    print(f"removed {slug} from {host.path()}; its files are untouched (district add re-adopts)")
    return 0

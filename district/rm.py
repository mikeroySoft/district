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
    if host.is_remote(data, slug):
        print(f"{slug}: on host {table['host']} (remote hosts not supported yet, #96)")
        return 0
    unit = host.unit_name(slug)
    backend = host.service_backend()
    if not args.keep_units:
        backend.enable(f"{unit}.timer", False, check=False)
        while backend.is_active(f"{unit}.service") in ("active", "activating"):
            if not args.wait:
                raise DistrictError(f"{unit}.service is running a pass; wait for it or pass --wait")
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
    del data["repo"][slug]
    if not data["repo"]:
        del data["repo"]
    host.save(data)
    print(f"removed {slug} from {host.path()}; its files are untouched (district add re-adopts)")
    return 0

#!/usr/bin/env python3
"""Generate/check the public Gateway route catalog; optionally sync its private source."""
import argparse
import json
from pathlib import Path
import re
import yaml

ROOT = Path(__file__).resolve().parents[1]
OWNERS = {
    "User Management": "user-management",
    "Battle": "battle",
    "Tamagotchi": "tamagotchi",
    "Notification": "notification",
    "Map": "map",
    "Monster Raid": "monster-raid",
    "Guild": "guild",
    "Package Registry": "package-registry",
}


def catalog(spec=None):
    if spec is None:
        spec = yaml.safe_load((ROOT / "contracts/openapi.yaml").read_text())
    routes = []
    source_paths = []
    for path, item in spec["paths"].items():
        for method, operation in item.items():
            if method not in (
                "get",
                "post",
                "put",
                "patch",
                "delete",
                "head",
                "options",
            ):
                continue
            if "101" in operation.get("responses", {}):
                continue  # WebSockets are direct; the Gateway only negotiates them.
            pattern = "".join(
                "[^/]+" if part.startswith("{") else re.escape(part)
                for part in re.split(r"(\{[^}]+\})", path)
            )
            routes.append(
                {
                    "service": OWNERS[operation["tags"][0]],
                    "method": method.upper(),
                    "pattern": pattern,
                    "audience": operation["x-audience"],
                    "callers": operation.get("x-allowed-callers", []),
                }
            )
            source_paths.append(path)
    for literal, route in zip(source_paths, routes):
        if "{" in literal:
            continue
        for other in routes:
            if (route["service"], route["method"]) != (
                other["service"],
                other["method"],
            ):
                continue
            if re.fullmatch(other["pattern"], literal) and (
                route["audience"] != other["audience"]
                or set(route["callers"]) != set(other["callers"])
            ):
                raise ValueError(
                    f"Conflicting Gateway permissions: {route['method']} {literal} "
                    f"overlaps {other['pattern']}"
                )
    return json.dumps(routes, indent=2) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--service-dir", type=Path)
    args = parser.parse_args()
    data = catalog()
    paths = [ROOT / "contracts/gateway-routes.json"]
    if args.service_dir:
        paths.append(args.service_dir / "gateway/routes.json")
    for path in paths:
        if args.check:
            if not path.exists() or path.read_text() != data:
                raise SystemExit(f"Gateway catalog is stale: {path}")
        else:
            path.write_text(data)
    print(
        "Gateway route catalog matches OpenAPI."
        if args.check
        else "Gateway route catalog updated."
    )


if __name__ == "__main__":
    main()

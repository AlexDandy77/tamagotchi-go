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


def catalog():
    spec = yaml.safe_load((ROOT / "contracts/openapi.yaml").read_text())
    routes = []
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
            if path.endswith("/chat"):
                continue  # Guild owns the direct WebSocket.
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

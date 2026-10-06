#!/usr/bin/env python3
"""Check real Gateway authentication and Battle -> Gateway -> Users routing.

Uses seeded users. Creates and cancels one challenge with synthetic, unused pet
IDs; never accepts it or changes balances/friendships. This is a transport check,
not a claim that unmigrated live combat dependencies work.
"""
import argparse
from pathlib import Path
import uuid
from lab import environment
from smoke import request

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8080")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    args = parser.parse_args()
    values = environment(args.env_file)
    users = args.base + "/services/user-management"
    battles = args.base + "/services/battle"
    request(args.base, "GET", "/healthz")
    request(args.base, "GET", "/readyz")
    sessions = {
        name: request(
            users,
            "POST",
            "/v1/auth/login",
            {"email": name + "@demo.invalid", "password": values["SEED_PASSWORD"]},
        )
        for name in ("alice", "bob")
    }
    alice, bob = sessions["alice"], sessions["bob"]
    token = alice["accessToken"]
    profile = request(
        users,
        "GET",
        "/v1/users/me",
        token=token,
        extra={"X-Gateway-Identity": "forged", "X-User-ID": bob["user"]["id"]},
    )
    assert profile["id"] == alice["user"]["id"]
    request(users, "GET", "/v1/users/me", expected=401)
    request(users, "GET", "/v1/users/me", token="invalid", expected=401)
    request(
        users,
        "GET",
        "/internal/v1/relationships?userId="
        + alice["user"]["id"]
        + "&otherUserId="
        + bob["user"]["id"],
        token=token,
        expected=401,
    )
    request(users, "GET", "/v1/wallet", token=token)
    request(battles, "GET", "/v1/battles", token=token)
    body = {
        "opponentId": bob["user"]["id"],
        "primaryPetId": str(uuid.uuid4()),
        "secondaryPetId": str(uuid.uuid4()),
        "boostIds": [],
    }
    key = str(uuid.uuid4())
    challenge = request(battles, "POST", "/v1/battles", body, token, key, 201)
    try:
        assert (
            request(battles, "POST", "/v1/battles", body, token, key, 201)["id"]
            == challenge["id"]
        )
        request(
            battles,
            "POST",
            "/v1/battles",
            {**body, "primaryPetId": str(uuid.uuid4())},
            token,
            key,
            409,
        )
        state = request(battles, "GET", "/v1/battles/" + challenge["id"], token=token)
        assert state["status"] == "pending"
    finally:
        request(
            battles,
            "DELETE",
            "/v1/battles/" + challenge["id"],
            token=token,
            key=str(uuid.uuid4()),
            expected=204,
        )
    print(
        "Gateway login, JWT rejection, spoofed-header removal, wallet/list reads, internal-route protection, Battle-to-Users mTLS, idempotency and challenge cancellation passed."
    )


if __name__ == "__main__":
    main()

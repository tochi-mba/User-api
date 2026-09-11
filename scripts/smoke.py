"""End-to-end check against a real user-api and a real keyring.

Not a test. The suite proves the code is right against fakes; this proves a *deployment*
is right against the two processes actually running -- a real keyring minting real tokens,
a real JWKS fetch over real HTTP, and a real SQLite file on real disk. Every one of those
is something the suite deliberately substitutes.

It exits non-zero on the first failure, so it doubles as a deployment gate.

What it proves, in order:

1. Both services are up, and user-api reports keyring reachable.
2. A token minted by keyring is accepted here -- which means the JWKS fetch, the issuer
   pin, the audience family and the signature check all agree between two processes that
   were configured separately.
3. A field and a note are written, and both are findable by search.
4. A scope-restricted token genuinely cannot see a scoped entry. This is the claim the
   whole compartmentalisation design makes, and it is the one worth checking against a
   token a *different service* minted rather than one a test fabricated.
5. Forgetting removes an entry from every read path.
6. A second account sees none of the first account's data.

Usage::

    USER_API_SMOKE_KEYRING=http://127.0.0.1:8001 \\
    USER_API_SMOKE_BASE=http://127.0.0.1:8002 \\
    KEYRING_ADMIN_TOKEN=... uv run python scripts/smoke.py

The keyring side needs an admin token because a fresh keyring is invite-only: the script
invites an address, redeems the invite, logs in, and mints service tokens from that
session. That is the real path an assistant's token takes, and shortcutting it would skip
the half of the arrangement this script exists to check.
"""

from __future__ import annotations

import os
import secrets
import sys

import httpx

KEYRING = os.environ.get("USER_API_SMOKE_KEYRING", "http://127.0.0.1:8001")
BASE = os.environ.get("USER_API_SMOKE_BASE", "http://127.0.0.1:8002")
ADMIN = os.environ.get("KEYRING_ADMIN_TOKEN", "")
PASSWORD = "correct horse battery staple"  # noqa: S105 -- a throwaway for a throwaway account
TIMEOUT = 10.0

SENTINEL = f"smoke-{secrets.token_hex(6)}"


class SmokeError(RuntimeError):
    """Something a working deployment would not have done."""


def check(condition: bool, what: str) -> None:
    """Report one step, and stop the script if it failed."""
    print(f"  {'ok  ' if condition else 'FAIL'}  {what}")
    if not condition:
        raise SmokeError(what)


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def onboard(keyring: httpx.Client, email: str) -> str:
    """Invite an address, redeem it, log in, and return the session token."""
    invited = keyring.post("/v1/admin/invites", json={"email": email}, headers=auth(ADMIN))
    check(invited.status_code == 201, f"keyring invited {email}")
    redeemed = keyring.post(
        "/v1/auth/invites/redeem",
        json={"token": invited.json()["token"], "password": PASSWORD},
    )
    check(redeemed.status_code == 201, "invite redeemed")
    logged_in = keyring.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    check(logged_in.status_code == 200, "logged in")
    session: str = logged_in.json()["token"]
    return session


def mint(keyring: httpx.Client, session: str, audience: str) -> str:
    """Ask keyring for a short-lived signed token for one audience."""
    response = keyring.post(
        "/v1/auth/service-token", json={"audience": audience}, headers=auth(session)
    )
    check(response.status_code == 200, f"minted a token for audience {audience!r}")
    token: str = response.json()["token"]
    return token


def run() -> None:
    """Every step, in order, stopping at the first failure."""
    if not ADMIN:
        msg = "set KEYRING_ADMIN_TOKEN -- a fresh keyring is invite-only"
        raise SmokeError(msg)

    with (
        httpx.Client(base_url=KEYRING, timeout=TIMEOUT) as keyring,
        httpx.Client(base_url=BASE, timeout=TIMEOUT) as api,
    ):
        print("health")
        health = api.get("/healthy")
        check(health.status_code == 200, "user-api is healthy")
        check(
            health.json()["checks"]["keyring"]["detail"]["reachable"],
            "user-api can reach keyring's JWKS document",
        )

        print("tokens")
        email = f"smoke-{secrets.token_hex(4)}@example.invalid"
        session = onboard(keyring, email)
        plain = mint(keyring, session, "user")
        health_scoped = mint(keyring, session, "user.health")

        print("writing")
        loaded = api.get("/v1/user", headers=auth(plain))
        check(loaded.status_code == 200, "a keyring token is accepted by user-api")
        check(loaded.json()["counts"]["fields"] == 0, "a fresh record is empty, not a 404")

        field = api.put(
            "/v1/user/fields/preferred_name",
            json={"value": SENTINEL, "description": "What to call them", "pinned": True},
            headers=auth(plain),
        )
        check(field.status_code == 200, "wrote a field")
        check(field.json()["asserted_by"] == "user", "asserted_by came from the token")

        note = api.post(
            "/v1/user/notes",
            json={
                "body": f"They mentioned {SENTINEL} when we last spoke.",
                "note_kind": "observation",
                "description": "Something said in passing",
            },
            headers=auth(plain),
        )
        check(note.status_code == 201, "wrote a note")

        print("searching")
        found = api.get("/v1/user/entries", params={"q": SENTINEL}, headers=auth(plain))
        check(found.status_code == 200, "search ran")
        check(found.json()["count"] == 2, "search found both the field and the note")

        print("scopes")
        scoped = api.put(
            "/v1/user/fields/blood_type",
            json={
                "value": "O-",
                "description": "Blood type",
                "scopes": ["health"],
                "sensitivity": "sensitive",
            },
            headers=auth(health_scoped),
        )
        check(scoped.status_code == 200, "a health token wrote a health-scoped field")
        hidden = api.get("/v1/user/fields/blood_type", headers=auth(plain))
        check(hidden.status_code == 404, "an unscoped token cannot see it")
        widened = api.get("/v1/user/entries", params={"scope": "health"}, headers=auth(plain))
        check(widened.status_code == 403, "an unscoped token cannot ask for the scope either")
        visible = api.get("/v1/user/fields/blood_type", headers=auth(health_scoped))
        check(visible.status_code == 200, "the health token can")

        print("forgetting")
        forgotten = api.delete(f"/v1/user/entries/{note.json()['entry_id']}", headers=auth(plain))
        check(forgotten.status_code == 200, "forgot the note")
        gone = api.get(f"/v1/user/entries/{note.json()['entry_id']}", headers=auth(plain))
        check(gone.status_code == 404, "it is gone from every read path")
        after = api.get("/v1/user/entries", params={"q": SENTINEL}, headers=auth(plain))
        check(after.json()["count"] == 1, "and gone from search")

        print("isolation")
        other_session = onboard(keyring, f"smoke-other-{secrets.token_hex(4)}@example.invalid")
        other = mint(keyring, other_session, "user")
        theirs = api.get("/v1/user", headers=auth(other))
        check(theirs.json()["counts"]["fields"] == 0, "a second account sees nothing of the first")
        theirs_search = api.get("/v1/user/entries", params={"q": SENTINEL}, headers=auth(other))
        check(theirs_search.json()["count"] == 0, "and finds nothing of theirs by searching")
        stolen = api.get(f"/v1/user/entries/{field.json()['entry_id']}", headers=auth(other))
        check(stolen.status_code == 404, "and gets a 404 for an entry id it was handed")

        print("cleanup")
        erased = api.delete("/v1/user", headers=auth(plain))
        check(erased.status_code == 200, "erased the smoke account's record")
        check(erased.json()["entries"] == 2, "and it reported what went")


def main() -> int:
    try:
        run()
    except SmokeError as failure:
        print(f"\nsmoke FAILED: {failure}", file=sys.stderr)
        return 1
    except httpx.HTTPError as error:
        print(f"\nsmoke could not reach a service: {error}", file=sys.stderr)
        return 2
    print("\nsmoke passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

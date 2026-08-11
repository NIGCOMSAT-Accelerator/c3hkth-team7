"""`python -m app.iam.cli` — provision service accounts without an HTTP round trip.

**Why a CLI as well as an endpoint.** `POST /iam/service-accounts` is guarded by the
legacy shared key, because provisioning the *replacement* for that key cannot itself
require a scoped key. That is a genuine bootstrap necessity, but it means the endpoint
keeps the shared key alive.

This runs inside the container, off the network, using the same Atlas credentials the
app already holds — so a deployment can mint the frontend's key and then **delete
API_KEY entirely**, which is the actual end state. The endpoint exists for
convenience; this exists so the migration can finish.

    make iam-service-account NAME=netlify-frontend EMAIL=ops@example.com
    docker compose exec api python -m app.iam.cli create --name ci --email ci@example.com
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.iam import platform, store
from app.iam.models import FRONTEND_SCOPES, PLATFORM_SCOPES, AccountKind, ApiKeyScope


async def _create(name: str, email: str, scopes: list[ApiKeyScope], days: int | None) -> int:
    if not store.available():
        print("MONGO_URL is not set, so there is no IAM store to provision into.",
              file=sys.stderr)
        return 1
    if not await store.ping():
        print("Atlas is unreachable. Check MONGO_URL and network access.", file=sys.stderr)
        return 1

    await store.ensure_indexes()
    provisioned = await platform.provision_service_account(
        name, scopes, email=email, expires_in_days=days
    )
    if provisioned is None:
        print(f"Could not provision {name!r} — that email may already be in use.",
              file=sys.stderr)
        return 1

    account_id, key = provisioned
    print()
    print("=" * 68)
    print(f"  Service account: {name}")
    print(f"  Account id     : {account_id}")
    print(f"  Scopes         : {', '.join(s.value for s in scopes)}")
    print(f"  Expires        : {f'in {days} days' if days else 'never (rotate or revoke manually)'}")
    print("=" * 68)
    print()
    print("  API KEY — shown once, not recoverable:")
    print()
    print(f"    {key}")
    print()
    print("  Set it in the consumer's environment and send it as the")
    print("  X-SHELTER-API-Key header:")
    print()
    print(f"    SHELTER_API_KEY={key}")
    print()
    print("  Then set IAM_LEGACY_SHARED_KEY_ENABLED=false and remove API_KEY.")
    print("=" * 68)
    await store.close()
    return 0


async def _list() -> int:
    if not store.available():
        print("MONGO_URL is not set.", file=sys.stderr)
        return 1

    accounts = await store.list_accounts_by_kind(AccountKind.SERVICE)
    if not accounts:
        print("No service accounts yet. The shared API_KEY is still the only "
              "platform credential — see `create`.")
        await store.close()
        return 0

    for account in accounts:
        print(f"\n{account.organisation or account.first_name}  ({account.id})")
        print(f"  status: {account.status.value}")
        for key in await store.list_api_keys(account.id):
            health = key.health or {}
            flags = [f for f, on in (("stale", health.get("stale")),
                                     ("expiring-soon", health.get("expiring_soon")),
                                     ("never-used", health.get("never_used"))) if on]
            print(f"  key {key.hint}…{key.last_four}  {key.status:9} "
                  f"uses={key.use_count:<6} {' '.join(flags)}")
            print(f"      scopes: {', '.join(s.value for s in key.scopes)}")
    await store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.iam.cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="Provision a service account and mint its key")
    create.add_argument("--name", required=True, help="e.g. netlify-frontend")
    create.add_argument("--email", required=True, help="Contact for expiry notices")
    create.add_argument(
        "--scopes",
        default=",".join(s.value for s in FRONTEND_SCOPES),
        help="Comma-separated. Defaults to the frontend's least-privilege set, which "
             "excludes platform:broadcast.",
    )
    create.add_argument("--expires-in-days", type=int, default=None)

    sub.add_parser("list", help="Show service accounts and their key health")

    args = parser.parse_args(argv)

    if args.command == "list":
        return asyncio.run(_list())

    try:
        scopes = [ApiKeyScope(s.strip()) for s in args.scopes.split(",") if s.strip()]
    except ValueError as exc:
        print(f"Unknown scope: {exc}", file=sys.stderr)
        print(f"Platform scopes: {', '.join(s.value for s in PLATFORM_SCOPES)}",
              file=sys.stderr)
        return 1

    invalid = [s for s in scopes if s not in PLATFORM_SCOPES]
    if invalid:
        print(f"These are tenant scopes, not platform scopes: "
              f"{[s.value for s in invalid]}", file=sys.stderr)
        return 1

    return asyncio.run(_create(args.name, args.email, scopes, args.expires_in_days))


if __name__ == "__main__":
    sys.exit(main())

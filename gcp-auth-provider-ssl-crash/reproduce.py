"""
Reproduces: GcpAuthProvider SSL crash after connection pool TTL expiry.

The first credential call succeeds. A forced connection pool expiry then causes the
second call to crash with:
  ValueError: Context has already been used to create a Connection, it cannot be mutated again

In production this crash occurs after ~8 minutes of idle time between workspace tool calls.

Usage:
    export IAM_CONNECTOR=projects/YOUR_PROJECT/locations/us-central1/connectors/YOUR_CONNECTOR
    python reproduce.py
"""

import asyncio
import os
import sys


def _force_pool_expiry(provider):
    """
    Force urllib3 to treat all pooled connections as expired so the next request
    attempts to create a new connection — triggering the SSL context mutation crash.
    """
    try:
        client = provider._get_client()
        session = client._transport._session  # requests.Session
        for adapter in session.adapters.values():
            if hasattr(adapter, "poolmanager"):
                for pool in adapter.poolmanager.pools.values():
                    # Mark every connection in the pool as expired
                    with pool.lock:
                        while not pool.pool.empty():
                            try:
                                conn = pool.pool.get_nowait()
                                if conn:
                                    conn.close()
                            except Exception:
                                pass
        print("  (forced pool expiry — existing connections closed)")
    except Exception as exc:
        print(f"  (could not force pool expiry: {exc})")
        print("  Alternatively, wait ~8–10 minutes between calls for the crash to occur naturally.")


async def main():
    connector = os.environ.get("IAM_CONNECTOR")
    if not connector:
        print("Error: IAM_CONNECTOR env var is required.")
        print("  export IAM_CONNECTOR=projects/PROJECT/locations/REGION/connectors/NAME")
        sys.exit(1)

    # Trigger pyopenssl injection (happens automatically in normal Agent Engine setups)
    import google.auth.transport.urllib3  # noqa: triggers inject_into_urllib3()

    from google.adk.integrations.agent_identity import GcpAuthProvider, GcpAuthProviderScheme
    from google.adk.auth.auth_tool import AuthConfig
    from google.adk.auth.credential_manager import CredentialManager

    provider = GcpAuthProvider()
    CredentialManager.register_auth_provider(provider)

    config = AuthConfig(
        auth_scheme=GcpAuthProviderScheme(
            name=connector,
            scopes=["https://www.googleapis.com/auth/userinfo.email", "openid"],
            continue_uri="https://example.com",
        )
    )

    class FakeContext:
        user_id = "repro-user"
        function_call_id = None
        session = None

    cred_mgr = CredentialManager(auth_config=config)

    # ── Call 1: succeeds, SSL context becomes immutable ───────────────────────
    print("Call 1 (expected to succeed)...")
    try:
        cred = await cred_mgr.get_auth_credential(FakeContext())
        print(f"  OK: {type(cred).__name__}")
    except Exception as exc:
        print(f"  UNEXPECTED FAILURE: {exc}")
        sys.exit(1)

    # ── Simulate pool TTL expiry ──────────────────────────────────────────────
    print("Forcing connection pool expiry...")
    _force_pool_expiry(provider)

    # ── Call 2: crashes with ValueError from pyopenssl ────────────────────────
    print("Call 2 (expected to crash with ValueError: Context has already been used...)...")
    try:
        cred = await cred_mgr.get_auth_credential(FakeContext())
        print(f"  OK (no crash observed — bug may already be fixed or pool expiry not triggered)")
    except ValueError as exc:
        print(f"\nBUG REPRODUCED:")
        print(f"  {type(exc).__name__}: {exc}")
    except RuntimeError as exc:
        print(f"\nBUG REPRODUCED (wrapped):")
        print(f"  {type(exc).__name__}: {exc}")
    except Exception as exc:
        print(f"\nUnexpected exception type: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    asyncio.run(main())

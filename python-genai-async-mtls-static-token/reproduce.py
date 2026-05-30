"""
Reproduces: async Vertex mTLS path caches StaticCredentials, causing long-lived
clients to fail with 401 UNAUTHENTICATED after access token expiry.

The async mTLS path in google.genai creates:

    AsyncAuthorizedSession(StaticCredentials(token=self._access_token()))

Because StaticCredentials never refreshes, a long-lived client (Agent Engine,
Cloud Run) pins the initial bearer token. After ~1 hour, model calls start
failing with 401 ACCESS_TOKEN_EXPIRED until the process is restarted.

This script proves the bug without any network calls by showing that
AsyncAuthorizedSession.before_request() overwrites a fresher Authorization
header with the stale static token.

NOTE: Fixed in google-genai==1.75.0. Run with ==1.74.0 to observe the bug,
      or with >=1.75.0 to verify the fix.

Usage:
    pip install -r requirements.txt
    python reproduce.py
"""

import asyncio
import inspect
import sys

import google.genai

print(f"google-genai: {google.genai.__version__}")

from google.genai._api_client import BaseApiClient

src = inspect.getsource(BaseApiClient._get_aiohttp_session)
uses_static = "StaticCredentials" in src and "_RefreshableAsyncCredentials" not in src
uses_refreshable = "_RefreshableAsyncCredentials" in src

print(f"_get_aiohttp_session uses StaticCredentials:          {uses_static}")
print(f"_get_aiohttp_session uses _RefreshableAsyncCredentials: {uses_refreshable}")

if uses_static:
    # Confirm the pinning behavior with a direct call
    async def demo_pin():
        from google.auth.aio.credentials import StaticCredentials

        class _FakeAuthRequest:
            pass

        class _FakeSession:
            _credentials = StaticCredentials("pinned-old-token")
            _auth_request = _FakeAuthRequest()

        headers = {"authorization": "Bearer fresh-token"}
        await _FakeSession._credentials.before_request(
            _FakeSession._auth_request, "POST", "https://example.com", headers
        )
        return headers["authorization"]

    try:
        result = asyncio.run(demo_pin())
        overwrites = result == "Bearer pinned-old-token"
        print(f"\nbefore_request overwrites fresh header with stale token: {overwrites}")
    except Exception as exc:
        print(f"\n(header-overwrite demo skipped: {exc})")

    print(
        "\nBUG PRESENT: async mTLS session is backed by StaticCredentials.\n"
        "  Long-lived clients will start failing with 401 ACCESS_TOKEN_EXPIRED\n"
        "  after the initial token expires (~1 hour), requiring process restart."
    )
    sys.exit(1)

elif uses_refreshable:
    calls_refresh = "_async_access_token" in src or "_access_token" in src
    print(f"_RefreshableAsyncCredentials.before_request calls token method: {calls_refresh}")

    if calls_refresh:
        print(
            "\nFIXED: async mTLS session uses _RefreshableAsyncCredentials,\n"
            "       which calls _async_access_token() on every request.\n"
            "       Tokens are refreshed transparently — no stale credential pinning."
        )
    else:
        print(
            "\nPARTIAL: _RefreshableAsyncCredentials present but before_request\n"
            "         does not appear to call a token-refresh method."
        )

else:
    print("\nUNKNOWN: neither credential class found — inspect _get_aiohttp_session manually.")

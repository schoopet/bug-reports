# Bug: async Vertex mTLS path caches `StaticCredentials`, causing 401 after token expiry

**Component:** `google-genai` / `google/genai/_api_client.py`
**Affected versions:** `google-genai <= 1.74.0` (confirmed on `1.73.1`, `1.74.0`)
**Fixed in:** `google-genai == 1.75.0`
**Severity:** High — long-lived Agent Engine / Cloud Run agents silently degrade after ~1 hour

---

## Summary

In the async Vertex AI + mTLS code path, `google-genai` created:

```python
AsyncAuthorizedSession(StaticCredentials(token=self._access_token()))
```

`StaticCredentials` never refreshes. The initial bearer token is pinned into
the `AsyncAuthorizedSession` for the life of the process. After ~1 hour, every
model call starts failing with `401 UNAUTHENTICATED / ACCESS_TOKEN_EXPIRED` until
the process is restarted.

This only affects the **async mTLS path** (`GOOGLE_API_USE_MTLS_ENDPOINT=auto` with
a default client cert source, as in Vertex AI Agent Engine). The sync path and the
plain non-mTLS async path are unaffected.

---

## Steps to Reproduce

```bash
pip install google-genai==1.74.0
python reproduce.py
```

Expected output (bug):
```
google-genai: 1.74.0
_get_aiohttp_session uses StaticCredentials:            True
_get_aiohttp_session uses _RefreshableAsyncCredentials: False

BUG PRESENT: async mTLS session is backed by StaticCredentials.
  Long-lived clients will start failing with 401 ACCESS_TOKEN_EXPIRED
  after the initial token expires (~1 hour), requiring process restart.
```

To verify the fix:
```bash
pip install google-genai==1.75.0
python reproduce.py
```

Expected output (fixed):
```
google-genai: 1.75.0
_get_aiohttp_session uses StaticCredentials:              False
_get_aiohttp_session uses _RefreshableAsyncCredentials:   True
_RefreshableAsyncCredentials.before_request calls token method: True

FIXED: async mTLS session uses _RefreshableAsyncCredentials,
       which calls _async_access_token() on every request.
       Tokens are refreshed transparently — no stale credential pinning.
```

---

## Root Cause

`_api_client.py`, async mTLS session initialisation (pre-2.0.0):

```python
from google.auth.aio.credentials import StaticCredentials
from google.auth.aio.transport.sessions import AsyncAuthorizedSession

async_creds = StaticCredentials(token=self._access_token())
self._aiohttp_session = AsyncAuthorizedSession(async_creds)
```

`AsyncAuthorizedSession.request()` calls `before_request()` on every call,
which reapplies the session credential's token to the request headers —
overwriting any fresher token that might have been set earlier.
`StaticCredentials.before_request()` always returns the original token, so
the stale token is reinjected on every request for the lifetime of the session.

`StaticCredentials` is documented as intentionally non-refreshing.
The bug is that `google-genai` used it for a long-lived cached session.

---

## Observed Failure Mode

- Agent works normally for ~1 hour after startup
- Model calls start failing with `401 UNAUTHENTICATED` / `ACCESS_TOKEN_EXPIRED`
- Calling Vertex AI over explicit mTLS directly succeeds (credentials are valid)
- Plain non-mTLS calls fail (session is using the stale token)
- Restarting the process restores functionality temporarily

---

## Fix (google-genai 2.0.0)

`StaticCredentials` was replaced with `_RefreshableAsyncCredentials`, an inline
adapter class that calls `_async_access_token()` on every `before_request()` call:

```python
class _RefreshableAsyncCredentials(AsyncCredentials):
    def __init__(self, client):
        self._client = client

    async def before_request(self, request, method, url, headers):
        token = await self._client._async_access_token()
        headers['Authorization'] = f'Bearer {token}'

    @property
    def valid(self):
        return not self._client._credentials.expired

self._aiohttp_session = AsyncAuthorizedSession(_RefreshableAsyncCredentials(self))
```

This means a fresh access token is fetched and injected on every async mTLS
request, matching the refresh semantics of the sync path.

---

## Environment

| | |
|---|---|
| `google-genai` (buggy) | `<= 1.74.0` (confirmed `1.73.1`, `1.74.0`) |
| `google-genai` (fixed) | `>= 1.75.0` |
| Trigger condition | Vertex AI async client with mTLS (`GOOGLE_API_USE_MTLS_ENDPOINT=auto`) |
| Runtime | Vertex AI Agent Engine, Cloud Run (long-lived processes) |

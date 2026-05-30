# Bug: `GcpAuthProvider` SSL crash after connection pool TTL expiry (~8 minutes)

**Component:** `google-adk[agent-identity]` — `GcpAuthProvider` / `IAMConnectorCredentialsServiceClient`  
**Severity:** Critical — all workspace tool calls fail after ~8 minutes of agent uptime  
**Versions:** `google-adk==1.26.0` through `1.33.0` (confirmed in prod)

---

## Summary

`GcpAuthProvider._get_client()` caches a single `IAMConnectorCredentialsServiceClient(transport="rest")`
instance. The REST transport creates an HTTP session using urllib3, which (due to
`google-auth[pyopenssl]`) uses an `OpenSSL.SSL.Context`. pyopenssl's `Context` becomes immutable
after the first `Connection` is created from it. When urllib3's connection pool TTL expires
(default ~8 minutes), urllib3 tries to reconfigure the cached `Context` to create a new
connection — **crash**.

This causes every workspace tool call (Calendar, Gmail, Drive, etc.) to fail with
`RuntimeError: Failed to retrieve credential` after about 8 minutes of agent uptime, producing
empty agent responses.

---

## Root Cause

### 1. Import-time pyopenssl injection

`google-auth[pyopenssl]` injects pyopenssl into urllib3 at import time:

```python
# google/auth/transport/urllib3.py (module level)
try:
    import urllib3.contrib.pyopenssl
    urllib3.contrib.pyopenssl.inject_into_urllib3()
except ImportError:
    pass
```

After this, all urllib3 HTTPS connections use `OpenSSL.SSL.Context` instead of stdlib `ssl.SSLContext`.

### 2. Cached client with cached SSL context

`GcpAuthProvider._get_client()` creates and caches the REST client once:

```python
def _get_client(self) -> Client:
    if self._client is None:
        ...
        self._client = Client(client_options=client_options, transport="rest")
    return self._client  # same client (same HTTP session, same SSL context) forever
```

The first call to `retrieve_credentials` creates an `OpenSSL.SSL.Connection` from the context,
making the context immutable.

### 3. Connection pool TTL crash

urllib3's default connection pool TTL is ~360–480 seconds. When a pool entry expires and urllib3
tries to create a new connection, it calls SSL configuration methods on the cached (now immutable)
`Context`:

```
ValueError: Context has already been used to create a Connection, it cannot be mutated again
  File "OpenSSL/SSL.py", line 860, in _raise_current_error
  File "OpenSSL/SSL.py", line 1600, in use_certificate
  File ".../urllib3/contrib/pyopenssl.py"
  File ".../google/cloud/iamconnectorcredentials_v1alpha/.../transports/rest.py"
```

### 4. Why global extraction doesn't help

Calling `urllib3.contrib.pyopenssl.extract_from_urllib3()` at startup reverts the injection.
However, the Agent Engine's mTLS infrastructure (`_MutualTlsAdapter` in
`google.auth.transport.requests`) accesses `ctx_poolmanager._ctx` — a pyopenssl-specific
attribute — and crashes with `AttributeError: 'SSLContext' object has no attribute '_ctx'` if
pyopenssl is not injected. This breaks every GCS artifact load and telemetry channel setup.

---

## Steps to Reproduce

See [`reproduce.py`](reproduce.py) for a self-contained script.

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export IAM_CONNECTOR=projects/YOUR_PROJECT/locations/us-central1/connectors/YOUR_CONNECTOR
python reproduce.py
```

The script makes two credential calls with a forced connection pool expiry between them.
The first call succeeds; the second crashes with `ValueError: Context has already been used`.

In a live Agent Engine deployment the crash is observed after ~8 minutes of idle time between
workspace tool calls.

---

## Impact

- **All** workspace tool calls (Calendar, Gmail, Drive, Sheets, Docs) fail after ~8 minutes.
- Failure is silent from the user's perspective: the agent returns an empty response.
- Re-deploying the agent resets the timer.
- The crash only appears on reconnects, not the first call, making it look intermittent.

---

## Fix

**Option A — simplest, no caching:** return a fresh client on every call:

```python
def _get_client(self) -> Client:
    client_options = None
    if host := os.environ.get("IAM_CONNECTOR_CREDENTIALS_TARGET_HOST"):
        client_options = ClientOptions(api_endpoint=host)
    return Client(client_options=client_options, transport="rest")
```

Each call gets a fresh HTTP session with a fresh `OpenSSL.SSL.Context`. The context is used once
then discarded. Performance overhead is negligible — IAM connector calls are infrequent (~once per
workspace tool call).

**Option B — better long-term:** use an async gRPC transport, which does not have the pyopenssl
pool issue.

**Option C:** configure the REST transport's session to use stdlib SSL specifically for the IAM
connector client while leaving pyopenssl injected globally for mTLS.

---

## Workaround (until fixed upstream)

Monkey-patch `_get_client` at module load time:

```python
from google.adk.integrations.agent_identity import GcpAuthProvider
from google.cloud.iamconnectorcredentials_v1alpha import IAMConnectorCredentialsServiceClient
from google.api_core.client_options import ClientOptions
import os

def _fresh_iam_client(self):
    client_options = None
    if host := os.environ.get("IAM_CONNECTOR_CREDENTIALS_TARGET_HOST"):
        client_options = ClientOptions(api_endpoint=host)
    return IAMConnectorCredentialsServiceClient(
        client_options=client_options, transport="rest"
    )

GcpAuthProvider._get_client = _fresh_iam_client
```

This is brittle and will break if the method signature changes.

---

## Environment

| | |
|---|---|
| `google-adk[agent-identity]` | 1.26.0 – 1.33.0 |
| `urllib3` | 2.x |
| `pyopenssl` | 24.x – 26.x |
| `google-auth[pyopenssl]` | ≥ 2.47 |
| `google-cloud-iamconnectorcredentials` | 0.1.0 |
| Runtime Python | 3.13 |
| GCP region | us-central1 |

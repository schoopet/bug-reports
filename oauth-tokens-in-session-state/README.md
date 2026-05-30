# Bug: OAuth access tokens stored in session state instead of `CredentialService`

**Component:** `google.adk.tools.openapi_tool`, `google.adk.auth.credential_service`
**Versions:** `google-adk==2.1.0`
**Severity:** High

---

## Summary

After a user completes an OAuth flow, ADK stores the resulting access token and refresh token
directly in session state rather than in `CredentialService`. Session state is logged wholesale
to BigQuery analytics, serialized into the Vertex AI session log, and returned via the
`GET /sessions` API endpoint. Any party with access to these sinks can recover live OAuth tokens
for every user of every tool.

This affects agents with **any number of OAuth-connected tools** — each tool produces a separate
credential entry in session state, so the exposure scales with the number of OAuth services the
agent integrates.

---

## Steps to Reproduce

```bash
pip install -r requirements.txt
python reproduce.py
```

---

## Output

```
=== Path 1: ToolContextCredentialStore ===
BUG REPRODUCED: OAuth tokens found in session.state under key 'oauth2_30afa9299aca0867_oauth2_ea6505764a94fa94_existing_exchanged_credential'
  access_token present: True
  refresh_token present: True

  This state flows into state_delta on every event, reaching:
    - BigQuery analytics plugin (logged on every invocation)
    - Vertex AI session log (persisted under permanent key)
    - GET /sessions API endpoint

=== Path 2: SessionStateCredentialService ===
BUG REPRODUCED: OAuth tokens found in session.state under key 'adk_oauth2_30afa9299aca0867_oauth2_64feae67192bf7d4'
  access_token present: True
  refresh_token present: True
```

---

## Root Cause

Two separate code paths write credentials directly to session state:

### Path 1: `ToolContextCredentialStore` (`tool_auth_handler.py`)

```python
class ToolContextCredentialStore:
    def store_credential(self, key: str, auth_credential: AuthCredential):
        if self.tool_context:
            self.tool_context.state[key] = auth_credential.model_dump(
                exclude_none=True
            )
```

The key uses a permanent (non-`temp:`) prefix, so the credential persists across invocations
and flows through `state_delta` into all session backends. `ToolContext` already exposes
`save_credential()` and `load_credential()` methods backed by `CredentialService`.
`ToolContextCredentialStore` bypasses these entirely and writes to state directly.

### Path 2: `SessionStateCredentialService` (`session_state_credential_service.py`)

```python
async def save_credential(self, auth_config, callback_context):
    callback_context.state[auth_config.credential_key] = (
        auth_config.exchanged_auth_credential
    )
```

The class is marked `@experimental` with an inline warning that "store credential in session
may not be secure", but it remains available and is not deprecated.

---

## Multi-Credential Exposure

An agent with tools for multiple OAuth services produces **one credential entry per service**
in session state. Example state for a three-tool agent after authentication:

```json
{
  "oauth2_<sheets_digest>_existing_exchanged_credential": {
    "authType": "oauth2",
    "oauth2": {"accessToken": "ya29.sheets...", "refreshToken": "1//sheets..."}
  },
  "oauth2_<calendar_digest>_existing_exchanged_credential": {
    "authType": "oauth2",
    "oauth2": {"accessToken": "ya29.calendar...", "refreshToken": "1//cal..."}
  },
  "oauth2_<drive_digest>_existing_exchanged_credential": {
    "authType": "oauth2",
    "oauth2": {"accessToken": "ya29.drive...", "refreshToken": "1//drive..."}
  }
}
```

All three tokens reach every sink simultaneously.

---

## Affected Sinks

### BigQuery analytics plugin

Logs `dict(session.state)` as session metadata on every invocation start. All live OAuth tokens
for all tools are written to BigQuery on every invocation.

Additionally, `HITL_CREDENTIAL_REQUEST_COMPLETED` events log the function response verbatim,
which includes the OAuth authorization code the user just received.

### Vertex AI session log

Every event that writes a credential to state produces a `state_delta` entry persisted to the
Vertex AI session log. Credentials written under permanent keys appear in the log indefinitely.

### `GET /sessions` endpoint

`Session` includes `state` which contains all credential entries. In the dev server this endpoint
has no authentication middleware. In production it is gated by infrastructure-level auth, but
any caller with access can retrieve live tokens.

### Why `temp:` does not fix this

`temp:` keys are stripped from `state_delta` before persistence, so they do not reach SQLite or
Vertex AI backends. However, `temp:` keys are still present in the in-memory `session.state`
during the invocation, so the BQ plugin snapshot still captures them. Additionally, credentials
need to survive across invocations, which `temp:` prevents.

---

## Proposed Fix

### 1. `runners.py` — default `credential_service` to `InMemoryCredentialService`

```python
# Before
credential_service: Optional[BaseCredentialService] = None

# After
credential_service: BaseCredentialService = Field(
    default_factory=InMemoryCredentialService
)
```

### 2. `tool_auth_handler.py` — replace `ToolContextCredentialStore` with credential service

Replace `_store_credential` and `_get_existing_credential` with calls to
`tool_context.save_credential()` / `tool_context.load_credential()`.

### 3. `session_state_credential_service.py` — deprecate

Add a deprecation warning directing users to `InMemoryCredentialService` or a
Secret Manager-backed implementation.

### 4. BigQuery plugin — scrub HITL credential response

Explicitly exclude `adk_request_credential` responses from the `result` field, or redact
known sensitive keys.

---

## Relationship to the `client_secret` wire-leak bug

These are independent bugs with independent fixes, but they interact:

- The `client_secret` wire-leak bug fixes what the server sends **to** the client
- This bug fixes what the server stores **after** the client responds

See also: [`../oauth-client-secret-wire-leak`](../oauth-client-secret-wire-leak)

---

## Environment

| | |
|---|---|
| `google-adk` | 2.1.0 |

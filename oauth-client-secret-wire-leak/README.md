# Bug: OAuth `client_secret` leaks to clients in every ADK event serialization path

**Component:** `google.adk.auth`, `google.adk.events`, all transport layers
**Versions:** `google-adk==2.1.0`
**Severity:** High

---

## Summary

When any tool requests OAuth credentials, the server-side `client_secret` (and `client_id`) is
included verbatim in the JSON payload sent to the client. This happens across every transport
layer — SSE, WebSocket, Agent Engine streaming, A2A, and the Vertex AI session log. A client
that captures any one of these streams can recover the `client_secret` and use it to mint OAuth
tokens on behalf of any user.

---

## Steps to Reproduce

```bash
pip install -r requirements.txt
python reproduce.py
```

---

## Output

```
BUG REPRODUCED: client_secret found in serialized event payload.

Path A (actions.requestedAuthConfigs): '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AVE_SERVER'
Path B (content.parts.functionCall.args): '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AVE_SERVER'

Both assertion paths passed — client_secret recoverable from the wire event.
```

---

## Root Cause

`AuthConfig` is a single Pydantic model that serves two incompatible roles:

1. **Server-side config** — holds `raw_auth_credential.oauth2.client_secret`, used server-side
   to construct the authorization URL and exchange the auth code for tokens.

2. **Wire DTO** — the same object is serialized verbatim by every transport layer via
   `event.model_dump_json(exclude_none=True, by_alias=True)` and sent to the client.

`generate_auth_request()` in `auth_handler.py` returns a deep copy of the full `AuthConfig` —
including `raw_auth_credential` — and places it in two locations in the event:

- `event.actions.requested_auth_configs[id]` — read by transport layers
- `event.content.parts[].functionCall.args.authConfig` — read by the frontend to open the OAuth popup

The `client_secret` appears **four times** in a single event:

| JSON path | Note |
|---|---|
| `actions.requestedAuthConfigs[id].rawAuthCredential.oauth2.clientSecret` | original |
| `actions.requestedAuthConfigs[id].exchangedAuthCredential.oauth2.clientSecret` | deep copy |
| `content.parts[0].functionCall.args.authConfig.rawAuthCredential.oauth2.clientSecret` | in args |
| `content.parts[0].functionCall.args.authConfig.exchangedAuthCredential.oauth2.clientSecret` | in args |

`vertex_ai_session_service.py` compounds this by explicitly re-serializing `requested_auth_configs`
by hand, so fixing `EventActions` alone would not fix the Vertex AI session log path.

---

## Affected Configuration Paths

### Toolsets (`CalendarToolset`, OpenAPI toolsets, `MCPToolset`)

```python
calendar_toolset = CalendarToolset(
    client_id=os.getenv("OAUTH_CLIENT_ID"),
    client_secret=os.getenv("OAUTH_CLIENT_SECRET"),
)
```

The toolset builds `AuthConfig` internally and calls `request_credential()`. The developer has
no indication the secret will be serialized to the event stream.

### `AuthenticatedFunctionTool`

```python
AuthenticatedFunctionTool(
    func=my_tool,
    auth_config=AuthConfig(
        auth_scheme=OAuth2(...),
        raw_auth_credential=AuthCredential(
            auth_type=AuthCredentialTypes.OAUTH2,
            oauth2=OAuth2Auth(
                client_id=os.getenv("OAUTH_CLIENT_ID"),
                client_secret=os.getenv("OAUTH_CLIENT_SECRET"),
            ),
        ),
    ),
)
```

### Manual `tool_context.request_credential()`

```python
def my_tool(tool_context: ToolContext) -> str:
    tool_context.request_credential(AuthConfig(
        auth_scheme=OAuth2(...),
        raw_auth_credential=AuthCredential(
            auth_type=AuthCredentialTypes.OAUTH2,
            oauth2=OAuth2Auth(client_id=..., client_secret=...),
        ),
    ))
```

---

## Affected Transport Layers

| Transport | File | Call |
|---|---|---|
| SSE | `adk_web_server.py` | `event.model_dump_json(exclude_none=True, by_alias=True)` |
| WebSocket | `adk_web_server.py` | `event.model_dump_json(exclude_none=True, by_alias=True)` |
| Agent Engine | `_utils.py` | `json.loads(event.model_dump_json(exclude_none=True))` |
| A2A metadata | `from_adk_event.py` | `value.model_dump(exclude_none=True, ...)` on `event.actions` |
| Vertex AI session log | `vertex_ai_session_service.py` | explicit re-serialization of `requested_auth_configs` |

---

## Impact

A client that captures the `adk_request_credential` event can:

1. Extract `clientSecret` from either path in the JSON
2. Use it with `clientId` (also present) to call the token endpoint directly
3. Exchange any authorization code for a full `access_token` + `refresh_token`, bypassing the
   server entirely
4. Mint tokens on behalf of any user who visits the OAuth consent screen

---

## Proposed Fix

Introduce a purpose-built wire type that structurally cannot carry `client_secret`. This enforces
the invariant at the type level rather than relying on field exclusion.

```python
class ClientAuthRequest(BaseModel):
    """Wire-safe outbound type. Sent server → client.
    Contains only what is needed to complete the browser OAuth redirect.
    Structurally cannot carry client_secret or raw credentials."""
    auth_uri: str        # pre-constructed authorization URL
    credential_key: str  # echo this back with the auth code
    scopes: list[str]    # for UI display

class ClientAuthResponse(BaseModel):
    """Wire-safe inbound type. Sent client → server.
    Only accepts the auth code — structurally cannot carry access_token,
    client_secret, or other server-only fields."""
    credential_key: str
    auth_code: str
```

| File | Change |
|---|---|
| `auth_tool.py` | Add `ClientAuthRequest`, `ClientAuthResponse` |
| `auth_handler.py` | `generate_auth_request()` returns `ClientAuthRequest` instead of `AuthConfig` |
| `event_actions.py` | `requested_auth_configs: dict[str, ClientAuthRequest]` |
| `functions.py` | `AuthToolArguments.auth_config: ClientAuthRequest` |
| `auth_preprocessor.py` | Accept `ClientAuthResponse` instead of raw `AuthCredential` |
| `vertex_ai_session_service.py` | Update explicit re-serialization loop |

The `AuthConfig` public API is unchanged. Tool authors continue to pass `client_secret` in
`raw_auth_credential` as today. The fix is entirely internal to ADK's event-building layer.

---

## Environment

| | |
|---|---|
| `google-adk` | 2.1.0 |

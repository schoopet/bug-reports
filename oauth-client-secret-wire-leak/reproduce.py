"""
Reproduces: OAuth client_secret leaks to clients in every ADK event serialization path.

No web server, no LLM calls, no network. Pure data-model layer.

When a tool requests OAuth credentials, the server-side client_secret (and client_id) is
included verbatim in the JSON payload serialized by every transport layer. A client that
captures any one of these streams can recover the client_secret.

Usage:
    pip install -r requirements.txt
    python reproduce.py
"""

import json

from fastapi.openapi.models import OAuth2, OAuthFlowAuthorizationCode, OAuthFlows
from google.adk.auth.auth_credential import AuthCredential, AuthCredentialTypes, OAuth2Auth
from google.adk.auth.auth_handler import AuthHandler
from google.adk.auth.auth_tool import AuthConfig, AuthToolArguments
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.genai import types

CLIENT_SECRET = "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AVE_SERVER"
CLIENT_ID = "123456789.apps.googleusercontent.com"

auth_config = AuthConfig(
    auth_scheme=OAuth2(
        flows=OAuthFlows(
            authorizationCode=OAuthFlowAuthorizationCode(
                authorizationUrl="https://accounts.google.com/o/oauth2/auth",
                tokenUrl="https://oauth2.googleapis.com/token",
                scopes={
                    "https://www.googleapis.com/auth/spreadsheets.readonly": "Read sheets"
                },
            )
        )
    ),
    raw_auth_credential=AuthCredential(
        auth_type=AuthCredentialTypes.OAUTH2,
        oauth2=OAuth2Auth(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            redirect_uri="http://localhost:8080/callback",
        ),
    ),
)

wire_auth_config = AuthHandler(auth_config).generate_auth_request()
FC_ID = "fake-function-call-id-001"

event = Event(
    invocation_id="test-invocation-id",
    author="my_agent",
    content=types.Content(
        role="model",
        parts=[
            types.Part(
                function_call=types.FunctionCall(
                    name="adk_request_credential",
                    id="fake-euc-call-id-001",
                    args=AuthToolArguments(
                        function_call_id=FC_ID,
                        auth_config=wire_auth_config,
                    ).model_dump(exclude_none=True, by_alias=True),
                )
            )
        ],
    ),
    actions=EventActions(requested_auth_configs={FC_ID: wire_auth_config}),
)

payload = json.loads(event.model_dump_json(exclude_none=True, by_alias=True))

# Check if client_secret appears anywhere in the serialized payload
payload_str = json.dumps(payload)
if CLIENT_SECRET not in payload_str:
    print("FIXED: client_secret not found in serialized event payload.")
    raise SystemExit(0)

print("BUG REPRODUCED: client_secret found in serialized event payload.\n")

try:
    path_a = payload["actions"]["requestedAuthConfigs"][FC_ID]["rawAuthCredential"]["oauth2"]["clientSecret"]
    print(f"Path A (actions.requestedAuthConfigs): {path_a!r}")
except (KeyError, TypeError) as exc:
    print(f"Path A not present: {exc}")

try:
    path_b = payload["content"]["parts"][0]["functionCall"]["args"]["authConfig"]["rawAuthCredential"]["oauth2"]["clientSecret"]
    print(f"Path B (content.parts.functionCall.args): {path_b!r}")
except (KeyError, TypeError) as exc:
    print(f"Path B not present: {exc}")

assert path_a == CLIENT_SECRET, f"Expected {CLIENT_SECRET!r}, got {path_a!r}"
assert path_b == CLIENT_SECRET, f"Expected {CLIENT_SECRET!r}, got {path_b!r}"
print("\nBoth assertion paths passed — client_secret recoverable from the wire event.")

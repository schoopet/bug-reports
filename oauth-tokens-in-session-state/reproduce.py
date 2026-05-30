"""
Reproduces: OAuth access tokens stored in session state instead of CredentialService.

After a user completes an OAuth flow, ADK stores the resulting access token and refresh token
directly in session state via two independent code paths. Session state is logged to analytics
backends, persisted to Vertex AI session logs, and returned via the GET /sessions API.

No web server, no LLM calls, no network. Pure data-model layer.

Usage:
    pip install -r requirements.txt
    python reproduce.py
"""

import asyncio

from fastapi.openapi.models import OAuth2, OAuthFlowAuthorizationCode, OAuthFlows
from google.adk.auth.auth_credential import AuthCredential, AuthCredentialTypes, OAuth2Auth
from google.adk.auth.auth_tool import AuthConfig
from google.adk.auth.credential_service.session_state_credential_service import (
    SessionStateCredentialService,
)
from google.adk.tools.openapi_tool.openapi_spec_parser.tool_auth_handler import (
    ToolContextCredentialStore,
)

ACCESS_TOKEN = "ya29.LIVE_ACCESS_TOKEN_SHOULD_NEVER_BE_IN_SESSION_STATE"
REFRESH_TOKEN = "1//LIVE_REFRESH_TOKEN_SHOULD_NEVER_BE_IN_SESSION_STATE"

_auth_scheme = OAuth2(
    flows=OAuthFlows(
        authorizationCode=OAuthFlowAuthorizationCode(
            authorizationUrl="https://accounts.google.com/o/oauth2/auth",
            tokenUrl="https://oauth2.googleapis.com/token",
            scopes={"https://www.googleapis.com/auth/calendar": "Calendar"},
        )
    )
)

_credential = AuthCredential(
    auth_type=AuthCredentialTypes.OAUTH2,
    oauth2=OAuth2Auth(
        client_id="client-id",
        client_secret="client-secret",
        access_token=ACCESS_TOKEN,
        refresh_token=REFRESH_TOKEN,
    ),
)


def test_path1_tool_context_credential_store():
    """Path 1: ToolContextCredentialStore writes OAuth tokens directly to session state."""
    print("=== Path 1: ToolContextCredentialStore ===")

    class FakeToolContext:
        def __init__(self):
            self.state = {}

    tool_ctx = FakeToolContext()
    store = ToolContextCredentialStore(tool_ctx)
    key = store.get_credential_key(_auth_scheme, _credential)
    store.store_credential(key, _credential)

    stored = tool_ctx.state.get(key)
    if stored is None:
        print("FIXED: no token written to session state.")
        return

    state_str = str(stored)
    if ACCESS_TOKEN in state_str or REFRESH_TOKEN in state_str:
        print(f"BUG REPRODUCED: OAuth tokens found in session.state under key {key!r}")
        print(f"  access_token present: {ACCESS_TOKEN in state_str}")
        print(f"  refresh_token present: {REFRESH_TOKEN in state_str}")
        print(
            "\n  This state flows into state_delta on every event, reaching:\n"
            "    - BigQuery analytics plugin (logged on every invocation)\n"
            "    - Vertex AI session log (persisted under permanent key)\n"
            "    - GET /sessions API endpoint"
        )
    else:
        print("FIXED: stored value does not contain live tokens.")


async def test_path2_session_state_credential_service():
    """Path 2: SessionStateCredentialService.save_credential() writes tokens to session state."""
    print("\n=== Path 2: SessionStateCredentialService ===")

    class FakeCallbackContext:
        def __init__(self):
            self.state = {}

    auth_config = AuthConfig(
        auth_scheme=_auth_scheme,
        raw_auth_credential=AuthCredential(
            auth_type=AuthCredentialTypes.OAUTH2,
            oauth2=OAuth2Auth(client_id="cid", client_secret="csec"),
        ),
    )
    auth_config.exchanged_auth_credential = AuthCredential(
        auth_type=AuthCredentialTypes.OAUTH2,
        oauth2=OAuth2Auth(
            client_id="cid",
            access_token=ACCESS_TOKEN,
            refresh_token=REFRESH_TOKEN,
        ),
    )

    ctx = FakeCallbackContext()
    svc = SessionStateCredentialService()
    await svc.save_credential(auth_config, ctx)

    state_str = str(ctx.state)
    if ACCESS_TOKEN in state_str or REFRESH_TOKEN in state_str:
        print(
            f"BUG REPRODUCED: OAuth tokens found in session.state "
            f"under key {auth_config.credential_key!r}"
        )
        print(f"  access_token present: {ACCESS_TOKEN in state_str}")
        print(f"  refresh_token present: {REFRESH_TOKEN in state_str}")
    else:
        print("FIXED: SessionStateCredentialService does not write tokens to session state.")


def main():
    test_path1_tool_context_credential_store()
    asyncio.run(test_path2_session_state_credential_service())


if __name__ == "__main__":
    main()

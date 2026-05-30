"""
Reproduces: Agent Engine omits newline between streamed events when tool response
contains artifact_delta, causing UnknownApiResponseError and silent data loss.

Usage:
    export PROJECT=your-project-id
    export STAGING_BUCKET=gs://your-staging-bucket
    export ARTIFACT_BUCKET=your-artifact-bucket-name   # no gs:// prefix

    python reproduce.py

The script deploys a minimal agent that calls save_artifact(), triggers a query,
and prints whether the bug is hit (UnknownApiResponseError / missing final response)
or the stream completes cleanly.
"""

import asyncio
import os
import sys

import vertexai
from vertexai import agent_engines
from vertexai.preview.reasoning_engines import AdkApp
from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool
from google.adk.tools.tool_context import ToolContext
from google.adk.artifacts import GcsArtifactService
from google.genai import types as genai_types


# Minimal 1×1 PNG
_TINY_PNG = bytes([
    0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0x00, 0x00, 0x0d,
    0x49, 0x48, 0x44, 0x52, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,
    0x08, 0x02, 0x00, 0x00, 0x00, 0x90, 0x77, 0x53, 0xde, 0x00, 0x00, 0x00,
    0x0c, 0x49, 0x44, 0x41, 0x54, 0x08, 0xd7, 0x63, 0xf8, 0xcf, 0xc0, 0x00,
    0x00, 0x00, 0x02, 0x00, 0x01, 0xe2, 0x21, 0xbc, 0x33, 0x00, 0x00, 0x00,
    0x00, 0x49, 0x45, 0x4e, 0x44, 0xae, 0x42, 0x60, 0x82,
])


async def save_and_return(tool_context: ToolContext) -> str:
    """Save a small artifact then return a result."""
    part = genai_types.Part(
        inline_data=genai_types.Blob(mime_type="image/png", data=_TINY_PNG)
    )
    await tool_context.save_artifact("test.png", part)
    return "done"


def build_agent() -> LlmAgent:
    return LlmAgent(
        name="repro",
        model="gemini-2.0-flash",
        tools=[FunctionTool(save_and_return)],
        instruction="Call save_and_return, then reply with a one-sentence summary.",
    )


def deploy(project: str, location: str, staging_bucket: str, artifact_bucket: str):
    print("Deploying agent to Agent Engine...")
    app = AdkApp(
        agent=build_agent(),
        artifact_service=GcsArtifactService(bucket_name=artifact_bucket),
    )
    remote = agent_engines.create(
        app,
        requirements=["google-adk==1.33.0", "google-genai==1.73.1"],
        staging_bucket=staging_bucket,
    )
    engine_id = remote.api_resource.name.split("/")[-1]
    print(f"Deployed engine ID: {engine_id}")
    return remote, engine_id


async def trigger(remote, engine_id: str):
    print("Creating session...")
    session = remote.create_session(user_id="repro-user")
    session_id = session["id"]

    print("Streaming query (expects function_call → function_response → model text)...")
    events = []
    bug_hit = False
    try:
        async for event in remote.async_stream_query(
            user_id="repro-user",
            session_id=session_id,
            message="Please call save_and_return.",
        ):
            events.append(event)
            print(f"  event: {str(event)[:120]}")
    except Exception as exc:
        bug_hit = True
        print(f"\nBUG HIT: {type(exc).__name__}: {str(exc)[:300]}")

    print()
    if bug_hit:
        print("RESULT: bug reproduced — stream raised an exception, final model response lost.")
    else:
        has_text = any(
            isinstance(e, dict) and e.get("content", {}).get("role") == "model"
            for e in events
        )
        if has_text:
            print("RESULT: no bug observed — stream completed and final model response received.")
        else:
            print("RESULT: stream completed without exception but final model response is missing.")


def main():
    project = os.environ.get("PROJECT")
    staging_bucket = os.environ.get("STAGING_BUCKET")
    artifact_bucket = os.environ.get("ARTIFACT_BUCKET")
    location = os.environ.get("LOCATION", "us-central1")

    if not project or not staging_bucket or not artifact_bucket:
        print("Error: PROJECT, STAGING_BUCKET, and ARTIFACT_BUCKET env vars are required.")
        sys.exit(1)

    vertexai.init(project=project, location=location, staging_bucket=staging_bucket)

    remote, engine_id = deploy(project, location, staging_bucket, artifact_bucket)
    asyncio.run(trigger(remote, engine_id))

    print(f"\nClean up: ae -p {project} delete {engine_id}")


if __name__ == "__main__":
    main()

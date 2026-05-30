"""
Reproduces: Agent Engine python_version in update() is silently ignored.

Usage:
    # Step 1 – create engine with default Python 3.10
    python reproduce.py --step create

    # Step 2 – update with python_version="3.13" (run from a Python 3.13 venv)
    python reproduce.py --step update --engine-id <ID from step 1>

Environment variables:
    PROJECT         GCP project ID (required)
    STAGING_BUCKET  GCS bucket URI, e.g. gs://my-bucket (required)
    LOCATION        GCP region (default: us-central1)
"""

import argparse
import os
import sys

import vertexai
from vertexai import Client
from google.adk.agents import LlmAgent


def make_agent() -> LlmAgent:
    return LlmAgent(name="python-version-test", model="gemini-2.0-flash")


def make_config(staging_bucket: str, python_version: str | None = None) -> dict:
    config = {
        "requirements": "requirements.txt",
        "extra_packages": [],
        "staging_bucket": staging_bucket,
        "agent_framework": "google-adk",
        "display_name": "python-version-test",
    }
    if python_version:
        config["python_version"] = python_version
    return config


def step_create(client: Client, staging_bucket: str) -> str:
    """Create an engine without specifying python_version (defaults to 3.10)."""
    print("Creating engine with default python_version (3.10)...")
    engine = client.agent_engines.create(
        agent=make_agent(),
        config=make_config(staging_bucket),  # no python_version → defaults to 3.10
    )
    engine_id = engine.api_resource.name.split("/")[-1]
    print(f"Created engine ID: {engine_id}")
    print(f"Full resource name: {engine.api_resource.name}")
    print()
    print("Next step — run from a Python 3.13 venv:")
    print(f"  python reproduce.py --step update --engine-id {engine_id}")
    return engine_id


def step_update(client: Client, project: str, location: str, staging_bucket: str, engine_id: str):
    """
    Update the engine with python_version="3.13" from a Python 3.13 local environment.

    Expected: engine runtime changes to Python 3.13.
    Actual:   engine runtime stays at Python 3.10; engine fails to start because
              the 3.13 pickle cannot be loaded by the 3.10 runtime:

                TypeError: code expected at most 16 arguments, got 18
                  File "/usr/local/lib/python3.10/..."
    """
    import platform
    local_version = platform.python_version()
    print(f"Local Python version: {local_version}")
    if not local_version.startswith("3.13"):
        print("WARNING: this step should be run from a Python 3.13 venv to produce the 3.13 pickle.")

    resource_name = f"projects/{project}/locations/{location}/reasoningEngines/{engine_id}"
    print(f"Updating engine {engine_id} with python_version='3.13'...")

    updated = client.agent_engines.update(
        name=resource_name,
        agent=make_agent(),
        config=make_config(staging_bucket, python_version="3.13"),
    )

    print("update() returned without API error.")
    print()
    print("Check the engine logs in Cloud Logging — you will see:")
    print("  TypeError: code expected at most 16 arguments, got 18")
    print("  File '/usr/local/lib/python3.10/...'")
    print()
    print("The runtime is still Python 3.10 despite python_version='3.13' being passed.")
    print()
    print(f"View logs:")
    print(f"  gcloud logging read \\")
    print(f"    'resource.type=\"aiplatform.googleapis.com/ReasoningEngine\" AND resource.labels.reasoning_engine_id=\"{engine_id}\"' \\")
    print(f"    --project={project} --limit=50")


def main():
    parser = argparse.ArgumentParser(description="Reproduce Agent Engine python_version update bug")
    parser.add_argument("--step", choices=["create", "update"], required=True)
    parser.add_argument("--engine-id", help="Engine ID from the create step (required for --step update)")
    args = parser.parse_args()

    project = os.environ.get("PROJECT")
    staging_bucket = os.environ.get("STAGING_BUCKET")
    location = os.environ.get("LOCATION", "us-central1")

    if not project:
        print("Error: PROJECT environment variable is required.")
        sys.exit(1)
    if not staging_bucket:
        print("Error: STAGING_BUCKET environment variable is required.")
        sys.exit(1)
    if args.step == "update" and not args.engine_id:
        print("Error: --engine-id is required for --step update.")
        sys.exit(1)

    vertexai.init(project=project, location=location, staging_bucket=staging_bucket)
    client = Client(project=project, location=location)

    if args.step == "create":
        step_create(client, staging_bucket)
    else:
        step_update(client, project, location, staging_bucket, args.engine_id)


if __name__ == "__main__":
    main()

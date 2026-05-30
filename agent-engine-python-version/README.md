# Bug: `python_version` in `update()` is silently ignored — engine runtime stays at Python 3.10

**Component:** Vertex AI Agent Engine (Reasoning Engines)  
**SDK:** `google-cloud-aiplatform` 1.154.0 / `google-adk` 2.1.0 / `cloudpickle` 3.1.2

---

## Summary

After an Agent Engine is created without an explicit `python_version` (which defaults to Python 3.10
per the API docs), calling `agent_engines.update()` with `python_version="3.13"` in the config
**does not change the engine's runtime**. The engine continues to run Python 3.10 for all subsequent
deployments. Because the pickle artifact is generated locally with Python 3.13, the update deployment
succeeds at the upload step but the engine fails to start at runtime with a cloudpickle
deserialization error.

---

## Steps to Reproduce

See [`reproduce.py`](reproduce.py) for a self-contained script.

The sequence is:

**Step 1** — Create an engine without specifying `python_version`. The API default is `3.10`.

```python
engine = client.agent_engines.create(
    agent=agent,
    config={
        "requirements": "requirements.txt",
        "extra_packages": [],
        "staging_bucket": STAGING_BUCKET,
        "agent_framework": "google-adk",
        "display_name": "python-version-test",
        # python_version omitted → defaults to "3.10"
    },
)
```

**Step 2** — Update the engine from a Python 3.13 local environment, explicitly passing
`python_version="3.13"`.

```python
client.agent_engines.update(
    name=engine.api_resource.name,
    agent=agent,
    config={
        "requirements": "requirements.txt",
        "extra_packages": [],
        "staging_bucket": STAGING_BUCKET,
        "agent_framework": "google-adk",
        "display_name": "python-version-test",
        "python_version": "3.13",  # ← explicitly requesting 3.13
    },
)
```

**Expected:** the engine runtime switches to Python 3.13 for the new revision.

**Actual:** the update call returns without error, but the engine fails to start:

```
ERROR Failed to load object from pickle file.
TypeError: code expected at most 16 arguments, got 18
  File ".../cloudpickle.loads(f.read())"
  File "/usr/local/lib/python3.10/..."   ← runtime is still 3.10
```

The `python3.10` path in the runtime traceback confirms the version did not change despite
`python_version="3.13"` being passed in the update config.

---

## Root Cause (observed)

The Python runtime version is **locked at engine creation time** and cannot be changed via
`update()`. The `python_version` field in the update config is accepted by the API without error
but has no effect on the running container.

This means any engine originally created with the default Python 3.10 runtime is permanently stuck
on 3.10. The only workaround is to delete the engine and recreate it using `agent_engines.create()`
with `python_version="3.13"` explicitly set.

---

## Why Recreation Is Not a Viable Workaround

The obvious workaround — delete and recreate the engine with `python_version="3.13"` — is **not
acceptable for engines using Agent Identity** (`identity_type = AGENT_IDENTITY`).

When Agent Identity is enabled, the engine's effective principal is derived from its resource ID:

```
principal://agents.global.org-{ORG}.system.id.goog/resources/aiplatform/projects/{PROJECT}/locations/{REGION}/reasoningEngines/{ENGINE_ID}
```

Every IAM binding granting the agent access to project resources (Cloud Tasks, Firestore,
Secret Manager, Cloud Storage, Cloud Run, etc.) references this principal directly. Recreating
the engine assigns a new `ENGINE_ID`, which means:

1. The effective principal changes.
2. All downstream IAM bindings become invalid and must be revoked and re-granted for the new principal.
3. Any infrastructure-as-code managing those bindings (e.g. Terraform) must be updated and re-applied.

In practice this is equivalent to rotating a service account identity across an entire project,
which carries significant operational risk and toil. It is not a reasonable ask for what should
be a routine runtime upgrade.

---

## Expected Behavior

Either:
1. `update()` should honour `python_version` and migrate the engine to the requested runtime, **or**
2. `update()` should return an explicit error when `python_version` differs from the engine's
   current runtime, rather than silently ignoring it and producing a broken deployment.

A `python_version` migration path via `update()` is especially important for Agent Identity
engines, where recreation is operationally expensive.

---

## Reproduction

### Prerequisites

- Python 3.13 (local)
- A GCP project with Vertex AI API enabled
- A GCS bucket for staging

### Setup

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Run

```bash
export PROJECT=your-project-id
export STAGING_BUCKET=gs://your-staging-bucket

# Step 1: create engine with default Python 3.10
python reproduce.py --step create

# Step 2: update with python_version="3.13" — observe it is silently ignored
python reproduce.py --step update --engine-id <ID from step 1>
```

---

## Environment

| | |
|---|---|
| `google-cloud-aiplatform` | 1.154.0 |
| `google-adk` | 2.1.0 |
| `cloudpickle` | 3.1.2 |
| Local Python | 3.13 |
| Engine default Python | 3.10 |
| GCP region | us-central1 |

# Bug: Agent Engine omits newline separator between streamed events when tool response contains `artifact_delta`

**Component:** Vertex AI Agent Engine / Google ADK  
**Versions:** `google-adk==1.33.0`, `google-genai==1.73.1`, `google-cloud-aiplatform==1.151.0`  
**Severity:** High — causes silent data loss on every invocation that saves an artifact

---

## Summary

When an ADK tool calls `tool_context.save_artifact()`, the resulting `function_response` event
carries a non-empty `artifact_delta` in its `actions` field. Agent Engine serializes this event
and the immediately-following model response event to the HTTP stream **without a newline separator**
between them, producing concatenated JSON:

```
{...function_response_event...}{...model_response_event...}
```

The `google-genai` streaming parser (`_aiter_response_stream`) reads lines via
`aiohttp.content.readline()`, so both objects arrive as a single "line". A downstream
`json.loads()` call then raises `JSONDecodeError: Extra data`, aborting the stream and discarding
all remaining events including the final model response.

---

## Steps to Reproduce

See [`reproduce.py`](reproduce.py) for a self-contained script.

### Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Run

```bash
export PROJECT=your-project-id
export STAGING_BUCKET=gs://your-staging-bucket
export ARTIFACT_BUCKET=gs://your-artifact-bucket

python reproduce.py
```

---

## Error

```
google.genai.errors.UnknownApiResponseError: Failed to parse response as JSON.
Raw response: {"content":{"parts":[{"function_response":{"id":"adk-...","name":"save_and_return","response":{"result":"done"}}}],"role":"user"},"invocation_id":"e-...","author":"repro","actions":{"state_delta":{},"artifact_delta":{"test.png":0},"requested_auth_configs":{},"requested_tool_confirmations":{}},"id":"...","timestamp":...}{"model_version":"gemini-2.0-flash","content":{"parts":[{"text":"I called save_and_return successfully."}],"role":"model"},"finish_reason":"STOP",...}
```

Full traceback:

```
File "...vertexai/_genai/_agent_engines_utils.py", in _method
    async for http_response in self.api_client._async_stream_query(...)
File "...vertexai/_genai/agent_engines.py", in _async_stream_query
    async for response in async_iterator:
File "...google/genai/_api_client.py", in async_generator
    async for chunk in response:
File "...google/genai/_api_client.py", in __anext__
    return await self.segment_iterator.__anext__()
File "...google/genai/_api_client.py", in async_segments
    yield self._load_json_from_response(chunk)
File "...google/genai/_api_client.py", in _load_json_from_response
    raise errors.UnknownApiResponseError(
google.genai.errors.UnknownApiResponseError: Failed to parse response as JSON.
```

---

## Root Cause

### Agent Engine: missing newline between consecutive events

Agent Engine serializes ADK events to the HTTP response stream. When a `function_response` event
has a **non-empty `artifact_delta`**, Agent Engine writes that event and the immediately-following
model response event **without a `\n` between them**:

```
# What Agent Engine sends:
{function_response_event_json}{model_response_event_json}\n

# What it should send:
{function_response_event_json}\n{model_response_event_json}\n
```

Events that do **not** involve `artifact_delta` are separated correctly. Only the
`artifact_delta`-bearing event triggers the missing newline.

### Why this breaks the client

`_aiter_response_stream` reads the stream line-by-line via `aiohttp.content.readline()`. With the
missing separator, both events arrive as a single line. The brace-counting accumulator yields
`"{event1}{event2}"` as one chunk. `_load_json_from_response` then calls
`json.loads("{event1}{event2}")`, which raises `JSONDecodeError: Extra data`.

---

## Impact

- Any agent tool that calls `tool_context.save_artifact()` silently drops the final model response
  on every invocation.
- Callers receive an unhandled exception from the streaming iterator.
- Affects both the async path (`async_stream_query`) and the sync path (`stream_query`).

---

## Suggested Fix

The bug belongs in Agent Engine's `save_artifact` handling: the code path that serializes an event
with a non-empty `artifact_delta` is failing to emit a trailing `\n`. It should write the newline
separator unconditionally, regardless of `artifact_delta` content.

---

## Client-Side Workaround

Catch `UnknownApiResponseError` and recover the concatenated payload using `raw_decode`:

```python
import json

_RAW_RESPONSE_PREFIX = "Failed to parse response as JSON. Raw response: "

def _split_json_objects(text: str) -> list[dict]:
    decoder = json.JSONDecoder()
    objects: list[dict] = []
    idx = 0
    while idx < len(text):
        while idx < len(text) and text[idx] in " \t\n\r":
            idx += 1
        if idx >= len(text):
            break
        try:
            obj, end = decoder.raw_decode(text, idx)
            if isinstance(obj, dict):
                objects.append(obj)
            idx = end
        except json.JSONDecodeError:
            break
    return objects

events = []
try:
    async for event in remote.async_stream_query(...):
        events.append(event)
except Exception as exc:
    exc_str = str(exc)
    if _RAW_RESPONSE_PREFIX not in exc_str:
        raise
    raw = exc_str[exc_str.index(_RAW_RESPONSE_PREFIX) + len(_RAW_RESPONSE_PREFIX):]
    recovered = _split_json_objects(raw)
    if not recovered:
        raise
    events.extend(recovered)
```

This depends on the exception message format remaining stable and should be removed once
Agent Engine is fixed.

---

## Environment

| | |
|---|---|
| `google-cloud-aiplatform` | 1.151.0 |
| `google-adk` | 1.33.0 |
| `google-genai` | 1.73.1 |
| GCP region | us-central1 |

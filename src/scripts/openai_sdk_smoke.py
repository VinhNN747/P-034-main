#!/usr/bin/env python3
"""Smoke test the backend's OpenAI-compatible API with the official SDK.

This script sends requests to the Gateway, using the official OpenAI SDK as
the client. ``GATEWAY_API_KEY`` must be an application key created in the
Gateway; the SDK's ``api_key`` parameter carries this key as a Bearer token.
It is not the upstream OpenAI provider key. The script automatically loads
these variables from the repository-root ``.env`` file. Variables already
set in the shell take precedence over values from ``.env``.

    export GATEWAY_OPENAI_BASE_URL=http://127.0.0.1:8001/v1
    export GATEWAY_API_KEY=ak_...
    python src/scripts/openai_sdk_smoke.py

Set ``GATEWAY_OPENAI_BASE_URL`` to the Gateway's OpenAI-compatible endpoint;
it defaults to ``http://127.0.0.1:8001/v1``. Do not use ``GATEWAY_BASE_URL``
from the project SDK, because that URL points to the separate ``/api/v1`` API.
Optional settings: GATEWAY_OPENAI_MODEL (defaults to ``auto``) and
GATEWAY_OPENAI_SMOKE_PROMPT. The smoke test uploads a temporary text file,
checks the Files API, references its ``file_id`` in the chat request, and then
deletes that file.

PowerShell:

    $env:GATEWAY_OPENAI_BASE_URL = 'http://127.0.0.1:8001/v1'
    $env:GATEWAY_API_KEY = 'ak_...'
    python src/scripts/openai_sdk_smoke.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "http://127.0.0.1:8001/v1"
DEFAULT_MODEL = "auto"
DEFAULT_PROMPT = "What reference code is in the attached file? Reply with the code only."
FILE_SMOKE_CONTENT = b"Gateway Files API smoke test. Reference code: BLUE-42.\n"


class SmokeTestError(Exception):
    """A local assertion failed while exercising a Gateway endpoint."""


def _format_api_error(exc: Any) -> str:
    """Extract the OpenAI-style error envelope without printing credentials."""
    try:
        body = exc.response.json()
    except (AttributeError, ValueError):
        return str(exc)

    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        message = error.get("message", "API request failed")
        code = error.get("code")
        error_type = error.get("type")
        details = ", ".join(str(value) for value in (error_type, code) if value)
        return f"{message} ({details})" if details else str(message)
    return str(exc)


def main() -> int:
    try:
        from dotenv import load_dotenv
    except ImportError:
        print(
            "python-dotenv is required to load the repository .env file. "
            "Install it with: python -m pip install python-dotenv",
            file=sys.stderr,
        )
        return 2

    env_path = Path(__file__).resolve().parents[2] / ".env"
    load_dotenv(dotenv_path=env_path, override=False)

    base_url = os.getenv("GATEWAY_OPENAI_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    api_key = os.getenv("GATEWAY_API_KEY", "")
    model = os.getenv("GATEWAY_OPENAI_MODEL", DEFAULT_MODEL)
    prompt = os.getenv("GATEWAY_OPENAI_SMOKE_PROMPT", DEFAULT_PROMPT)

    if not api_key:
        print(
            "Missing GATEWAY_API_KEY. Set it to the application key created in the Gateway "
            "(usually starts with ak_). This is not the upstream OpenAI provider key.",
            file=sys.stderr,
        )
        return 2

    try:
        from openai import APIConnectionError, APIStatusError, OpenAI, OpenAIError
    except ImportError:
        print(
            "The official OpenAI Python SDK is not installed. Install it with: python -m pip install openai",
            file=sys.stderr,
        )
        return 2

    print(f"Gateway OpenAI-compatible endpoint: {base_url}")
    print(f"Model: {model}")

    cleanup_failed = False
    try:
        # Avoid automatic retries so a smoke test does not silently repeat a
        # billable completion when the connection drops after the server runs it.
        with OpenAI(base_url=base_url, api_key=api_key, timeout=30.0, max_retries=0) as client:
            uploaded_file_id = None
            print("[1/3] Listing models...")
            models = client.models.list()
            model_ids = [entry.id for entry in models.data]
            print(f"OK: received {len(model_ids)} model(s)")
            if model_ids:
                print("Available models: " + ", ".join(model_ids))
            if model not in model_ids:
                print(f"Note: '{model}' was not listed; continuing to check chat completions.")

            try:
                print("[2/3] Uploading and checking a small text file...")
                uploaded = client.files.create(
                    file=("openai_sdk_smoke.txt", FILE_SMOKE_CONTENT, "text/plain"),
                    purpose="user_data",
                )
                uploaded_file_id = uploaded.id
                retrieved = client.files.retrieve(uploaded_file_id)
                listed_ids = {entry.id for entry in client.files.list(limit=1000).data}
                downloaded = client.files.content(uploaded_file_id).read()
                if retrieved.id != uploaded_file_id or uploaded_file_id not in listed_ids:
                    raise SmokeTestError("uploaded file was not returned by retrieve/list")
                if downloaded != FILE_SMOKE_CONTENT:
                    raise SmokeTestError("downloaded file content did not match the uploaded bytes")
                print(f"OK: upload, retrieve, list, and content work (id={uploaded_file_id})")

                print("[3/3] Creating a chat completion with the uploaded file_id...")
                completion = client.chat.completions.create(
                    model=model,
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "file", "file": {"file_id": uploaded_file_id}},
                        ],
                    }],
                )
            finally:
                if uploaded_file_id:
                    try:
                        deleted = client.files.delete(uploaded_file_id)
                        if deleted.deleted:
                            print("OK: removed the temporary smoke-test file")
                        else:
                            cleanup_failed = True
                            print(
                                f"Warning: temporary file {uploaded_file_id} was not deleted; "
                                "remove it with client.files.delete(file_id).",
                                file=sys.stderr,
                            )
                    except OpenAIError as cleanup_error:
                        cleanup_failed = True
                        print(
                            f"Warning: could not delete temporary file {uploaded_file_id}: {cleanup_error}",
                            file=sys.stderr,
                        )

    except APIConnectionError as exc:
        print(
            f"Could not connect to {base_url}. Check that the AI backend is running and that the URL includes /v1.\n"
            f"Details: {exc}",
            file=sys.stderr,
        )
        return 1
    except APIStatusError as exc:
        print(
            f"API returned HTTP {exc.status_code}: {_format_api_error(exc)}\n"
            "Check the application key, GATEWAY_RUNTIME_ENABLED, and the application's gateway/model-pool setup.",
            file=sys.stderr,
        )
        return 1
    except OpenAIError as exc:
        print(f"OpenAI SDK error: {exc}", file=sys.stderr)
        return 1
    except SmokeTestError as exc:
        print(f"Files API smoke test failed: {exc}", file=sys.stderr)
        return 1

    if cleanup_failed:
        print("Smoke test failed because the temporary file could not be cleaned up.", file=sys.stderr)
        return 1

    answer = completion.choices[0].message.content if completion.choices else None
    if answer is None:
        print("The request succeeded but the response did not contain a text choice.", file=sys.stderr)
        return 1

    print(f"OK: completion id={completion.id}, model={completion.model}")
    print("Response:")
    print(answer)
    if completion.usage is not None:
        print(
            "Tokens: "
            f"prompt={completion.usage.prompt_tokens}, "
            f"completion={completion.usage.completion_tokens}, "
            f"total={completion.usage.total_tokens}"
        )
    print("OpenAI SDK compatibility smoke test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

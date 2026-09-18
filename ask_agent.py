#!/usr/bin/env python3
"""Send a prompt to an Agent Runtime ADK agent through streamQuery.

The default streamQuery route is the one Model Armor screens when used with a
Client-to-Agent Agent Gateway. Configure PROJECT_ID, LOCATION_ID, and
RESOURCE_ID in .env, then run:

    python ask_agent.py "How many active veterinary companies are in Bristol?"
"""

import argparse
import json
import os
import secrets
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional


def load_dotenv() -> None:
    """Load simple KEY=VALUE entries from a local .env file without packages."""
    env_file = Path(__file__).with_name(".env")
    if not env_file.exists():
        return

    for line_number, line in enumerate(env_file.read_text().splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            sys.exit(f"{env_file}:{line_number}: expected KEY=VALUE")
        key, value = line.split("=", maxsplit=1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if not key:
            sys.exit(f"{env_file}:{line_number}: missing variable name")
        os.environ.setdefault(key, value)


def access_token() -> str:
    try:
        result = subprocess.run(
            ["gcloud", "auth", "print-access-token"],
            capture_output=True,
            check=True,
            text=True,
        )
    except FileNotFoundError:
        sys.exit("gcloud was not found. Install the Google Cloud CLI, then run `gcloud auth login`.")
    except subprocess.CalledProcessError as error:
        sys.exit(f"Could not get a Google Cloud access token:\n{error.stderr.strip()}")

    token = result.stdout.strip()
    if not token:
        sys.exit("gcloud returned an empty access token. Run `gcloud auth login` and try again.")
    return token


def engine_url(project: str, location: str, resource: str) -> str:
    return (
        f"https://{location}-aiplatform.googleapis.com/v1/projects/{project}"
        f"/locations/{location}/reasoningEngines/{resource}"
    )


def post(
    url: str, body: dict, token: str, traceparent: Optional[str] = None
) -> tuple[int, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if traceparent:
        headers["traceparent"] = traceparent

    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")
    except urllib.error.URLError as error:
        sys.exit(f"Could not connect to Agent Runtime: {error.reason}")


def create_session(args: argparse.Namespace, token: str) -> str:
    status, response = post(
        engine_url(args.project, args.location, args.resource) + ":query",
        {
            "class_method": "async_create_session",
            "input": {"user_id": args.user_id},
        },
        token,
    )
    if status != 200:
        report_error(status, response)

    try:
        return json.loads(response)["output"]["id"]
    except (KeyError, TypeError, json.JSONDecodeError):
        sys.exit(f"Session creation returned an unexpected response:\n{response}")


def report_error(status: int, response: str) -> None:
    try:
        message = json.loads(response)["error"]["message"]
    except (KeyError, TypeError, json.JSONDecodeError):
        message = response.strip() or "No error message returned."

    if status == 403 and "Model Armor" in message:
        print("BLOCKED BY MODEL ARMOR")
        print(message)
        raise SystemExit(2)

    print(f"REQUEST FAILED (HTTP {status})")
    print(message)
    raise SystemExit(1)


def print_events(response: str, raw: bool) -> None:
    if raw:
        print(response)
        return

    printed_text = False
    for line in response.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line.removeprefix("data:").strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue

        parts = event.get("parts") or event.get("content", {}).get("parts", [])
        for part in parts:
            if text := part.get("text"):
                print(text, end="", flush=True)
                printed_text = True

    if printed_text:
        print()
    else:
        print("No text found in the response. Re-run with --raw to inspect it.")


def parse_args() -> argparse.Namespace:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Test a deployed Agent Runtime agent through Model Armor-screened streamQuery."
    )
    parser.add_argument("prompt", help="Prompt to send to the agent.")
    parser.add_argument("--project", default=os.getenv("PROJECT_ID"))
    parser.add_argument("--location", default=os.getenv("LOCATION_ID"))
    parser.add_argument("--resource", default=os.getenv("RESOURCE_ID"), help="Agent Runtime numeric ID.")
    parser.add_argument("--user-id", default=os.getenv("USER_ID", "gateway-test-user"))
    parser.add_argument("--session-id", help="Resume an existing Agent Platform Session.")
    parser.add_argument("--raw", action="store_true", help="Print the full SSE response.")
    args = parser.parse_args()

    missing = [
        name
        for name, value in (
            ("PROJECT_ID", args.project),
            ("LOCATION_ID", args.location),
            ("RESOURCE_ID", args.resource),
        )
        if not value
    ]
    if missing:
        parser.error(
            f"Missing {', '.join(missing)}. Copy .env.example to .env and fill in the values."
        )
    return args


def main() -> None:
    args = parse_args()
    token = access_token()
    session_id = args.session_id or create_session(args, token)
    trace_id = secrets.token_hex(16)
    print(f"Session: {session_id}\n", file=sys.stderr)

    status, response = post(
        engine_url(args.project, args.location, args.resource) + ":streamQuery?alt=sse",
        {
            "class_method": "async_stream_query",
            "input": {
                "message": args.prompt,
                "user_id": args.user_id,
                "session_id": session_id,
            },
        },
        token,
        traceparent=f"00-{trace_id}-{secrets.token_hex(8)}-01",
    )
    if status != 200:
        report_error(status, response)
    print_events(response, args.raw)


if __name__ == "__main__":
    main()

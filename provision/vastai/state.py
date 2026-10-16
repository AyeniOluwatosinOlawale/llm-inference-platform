"""
FIX #3: Remote state for the Vast.ai provisioner.
Stores platform state in a Supabase Storage bucket so it survives local machine loss
and is accessible to CI/CD pipelines.

Requires:
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY env vars
  Supabase Storage bucket named 'llm-inference-platform-state' (public: false)
"""

import os
import json
import httpx

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
BUCKET = "llm-inference-platform-state"
OBJECT_KEY = "vastai/state.json"


def _headers() -> dict:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


def push_state(state: dict) -> None:
    """Upload state JSON to Supabase Storage."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise EnvironmentError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set")

    url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{OBJECT_KEY}"
    payload = json.dumps(state, indent=2).encode()

    # upsert
    resp = httpx.put(
        url,
        headers={**_headers(), "Content-Type": "application/octet-stream", "x-upsert": "true"},
        content=payload,
    )
    resp.raise_for_status()


def pull_state() -> dict:
    """Download state JSON from Supabase Storage."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise EnvironmentError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set")

    url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{OBJECT_KEY}"
    resp = httpx.get(url, headers=_headers())
    if resp.status_code == 404:
        return {}
    resp.raise_for_status()
    return resp.json()

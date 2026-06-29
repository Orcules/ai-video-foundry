"""Supabase Auth access-token verification for Video Studio (browser JWT)."""

import logging
import time
import threading
from typing import Optional

import requests

from api_pipeline.supabase_client import resolve_supabase_public_credentials

logger = logging.getLogger(__name__)

# In-memory cache: token → (user_id, expiry_timestamp)
# Prevents hammering Supabase on every SSE heartbeat / poll.
_token_cache: dict = {}
_token_cache_lock = threading.Lock()
_TOKEN_CACHE_TTL = 90  # seconds — short enough to catch revocations; long enough to avoid flood


def get_supabase_user_id_from_access_token(access_token: str) -> Optional[str]:
    """Return Supabase Auth user id for a valid access token, or None."""
    token = (access_token or "").strip()
    if not token:
        return None

    now = time.monotonic()
    with _token_cache_lock:
        cached = _token_cache.get(token)
        if cached is not None:
            user_id, expiry = cached
            if now < expiry:
                return user_id
            # Expired — remove and fall through to verify
            del _token_cache[token]

    url, key = resolve_supabase_public_credentials()
    if not url or not key:
        return None
    try:
        r = requests.get(
            f"{url.rstrip('/')}/auth/v1/user",
            headers={"Authorization": f"Bearer {token}", "apikey": key},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        user_id = (r.json() or {}).get("id")
        if user_id:
            with _token_cache_lock:
                _token_cache[token] = (user_id, now + _TOKEN_CACHE_TTL)
        return user_id
    except Exception as e:
        logger.warning("Supabase access token verify failed: %s", e)
        return None

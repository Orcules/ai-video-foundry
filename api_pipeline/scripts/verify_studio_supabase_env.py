#!/usr/bin/env python3
"""Check api_pipeline/.env for Studio cloud auth (Supabase). Does not connect to the DB."""

from __future__ import annotations

import os
import sys

# Repo root / api_pipeline (same layout as server.py)
_API_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(_API_DIR)

if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from dotenv import load_dotenv

_MONOLITH_ENV = os.path.join(_REPO_ROOT, "Comp_Videos", ".env")
if os.path.isfile(_MONOLITH_ENV):
    load_dotenv(_MONOLITH_ENV)
_env_file = os.path.join(_API_DIR, ".env")
if os.path.isfile(_env_file):
    load_dotenv(_env_file)
load_dotenv()


def main() -> int:
    from api_pipeline.supabase_client import resolve_supabase_public_credentials

    url, pub = resolve_supabase_public_credentials()
    sr = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()

    print("Video API - Studio / Supabase env check")
    print(f"  api_pipeline/.env exists: {os.path.isfile(_env_file)}")
    print(f"  SUPABASE_URL: {'set' if url else 'MISSING'}")
    if url:
        print(f"    -> {url}")
    print(f"  Anon/publishable key (ANON or PUBLISHABLE): {'set' if pub and len(pub) > 20 else 'MISSING'}")
    print(f"  SUPABASE_SERVICE_ROLE_KEY: {'set' if sr else 'MISSING (gallery user_videos insert will skip)'}")
    print()
    m0 = os.path.join(_API_DIR, "migrations", "000_video_pipeline_bootstrap.sql")
    m1 = os.path.join(_API_DIR, "migrations", "001_user_auth_sessions_videos.sql")
    print("  New Supabase project: run SQL in order (SQL Editor):")
    print(f"    1) {m0}")
    print(f"    2) {m1}")
    print()
    print("  Supabase Dashboard: Authentication -> Providers -> enable Email")
    print("  Same page: turn OFF \"Confirm email\" if you want instant sign-in without verification (dev / early stage).")
    print("  Table api_tenants: need at least one row with api_key (sk-tvd-...) for tenant auth;")
    print("    cloud sign-in + auto-tenant needs exactly one eligible row (is_active not false) OR STUDIO_FALLBACK_API_KEY in .env.")
    print("  Authentication -> URL Configuration -> add redirect/site for your Studio URL,")
    print("    e.g. http://localhost:8000/studio")
    print()

    if url and pub and len(pub) > 20:
        print("OK: Server should expose studio_cloud_available=true after restart.")
        print("    GET /api/config on your API host to confirm.")
        return 0
    print("BLOCKED: Set SUPABASE_URL and SUPABASE_ANON_KEY or SUPABASE_PUBLISHABLE_KEY in api_pipeline/.env")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

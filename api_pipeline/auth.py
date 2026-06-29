"""API key authentication for tenant isolation."""

import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, HTTPException, Query, Request

from api_pipeline.studio_auth import get_supabase_user_id_from_access_token

logger = logging.getLogger(__name__)

# Cache TTL in seconds
_CACHE_TTL = 300  # 5 minutes


@dataclass
class Tenant:
    id: str
    name: str
    api_key: str


# Module-level state
_supabase = None
_cache: dict = {}  # api_key -> (Tenant, expires_at)


def init_auth(supabase_client):
    """Call once during server lifespan to set the Supabase client."""
    global _supabase
    _supabase = supabase_client
    logger.info("Auth module initialized")


def _lookup_tenant(api_key: str) -> Optional[Tenant]:
    """Look up a tenant by API key, with caching."""
    now = time.time()

    # Check cache
    cached = _cache.get(api_key)
    if cached:
        tenant, expires_at = cached
        if now < expires_at:
            return tenant
        # Expired — remove and re-fetch
        del _cache[api_key]

    # When Supabase is not configured (local dev), accept any non-empty key as "dev" tenant
    if not _supabase:
        if api_key:
            tenant = Tenant(id="dev", name="Dev", api_key=api_key)
            _cache[api_key] = (tenant, now + _CACHE_TTL)
            return tenant
        return None

    try:
        fetch = getattr(_supabase, "fetch_tenant_row_by_api_key", None)
        if callable(fetch):
            row = fetch(api_key)
        else:
            result = (
                _supabase.client.table("api_tenants")
                .select("id, name, api_key, is_active")
                .eq("api_key", api_key)
                .execute()
            )
            rows = result.data or []
            row = rows[0] if rows else None
        if not row:
            # Graceful fallback: when Supabase is unreachable (DNS / paused project),
            # fetch returns None (the SupabaseClient swallows the network error).
            # If the api_key matches STUDIO_FALLBACK_API_KEY, admit as dev tenant.
            # Toggle off with DEV_BYPASS_SUPABASE=0.
            bypass = (os.environ.get("DEV_BYPASS_SUPABASE", "1") or "1").strip().lower()
            if bypass not in ("0", "false", "no", ""):
                fallback_key = _studio_fallback_api_key()
                if fallback_key and api_key == fallback_key:
                    logger.warning(
                        "Supabase returned no row; admitting api_key=%s as dev tenant via STUDIO_FALLBACK_API_KEY",
                        api_key[:12] + "…",
                    )
                    tenant = Tenant(id="dev-fallback", name="Dev (Supabase offline)", api_key=api_key)
                    _cache[api_key] = (tenant, now + _CACHE_TTL)
                    return tenant
            return None

        if not row.get("is_active", True):
            # Return a sentinel so we can distinguish inactive vs not-found
            return "inactive"

        tenant = Tenant(id=row["id"], name=row["name"], api_key=row["api_key"])
        _cache[api_key] = (tenant, now + _CACHE_TTL)
        return tenant
    except Exception as e:
        logger.error(f"Auth lookup failed: {e}")
        # Graceful degradation for dev: if Supabase is unreachable (DNS / network /
        # paused project) AND the api_key matches the configured fallback key, let
        # the request through as a "dev" tenant. Production keys still get rejected
        # because they won't match the fallback. Toggle off by unsetting
        # STUDIO_FALLBACK_API_KEY (or setting DEV_BYPASS_SUPABASE=0).
        bypass = (os.environ.get("DEV_BYPASS_SUPABASE", "1") or "1").strip().lower()
        if bypass not in ("0", "false", "no", ""):
            fallback_key = _studio_fallback_api_key()
            err_text = str(e).lower()
            is_network = (
                "name or service not known" in err_text
                or "name resolution" in err_text
                or "getaddrinfo" in err_text
                or "connection refused" in err_text
                or "timed out" in err_text
                or "nodename nor servname" in err_text
            )
            if is_network and fallback_key and api_key == fallback_key:
                logger.warning(
                    "Supabase unreachable; admitting api_key=%s as dev tenant via STUDIO_FALLBACK_API_KEY",
                    api_key[:12] + "…",
                )
                tenant = Tenant(id="dev-fallback", name="Dev (Supabase offline)", api_key=api_key)
                _cache[api_key] = (tenant, now + _CACHE_TTL)
                return tenant
        return None


def _extract_api_key(request: Request, token: Optional[str] = None) -> str:
    """Extract API key from Authorization header or query param."""
    # 1. Authorization: Bearer sk-tvd-...
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:].strip()

    # 2. ?token=sk-tvd-... (for SSE EventSource which can't set headers)
    if token:
        return token

    return ""


def _studio_fallback_api_key() -> str:
    """Tenant API key used when Studio sends a valid Supabase user JWT (see STUDIO_FALLBACK_API_KEY)."""
    return (os.environ.get("STUDIO_FALLBACK_API_KEY") or "").strip()


def _implicit_studio_tenant_api_key(
    request: Request, *, allow_query_studio_jwt: bool
) -> str:
    """When Bearer/?token= absent: use env fallback or single api_tenants row if JWT is valid."""
    hdr = (request.headers.get("X-Studio-User-Token") or "").strip()
    jwt_ok = bool(hdr and get_supabase_user_id_from_access_token(hdr))
    if not jwt_ok and allow_query_studio_jwt:
        st = (request.query_params.get("studio_user_token") or "").strip()
        jwt_ok = bool(st and get_supabase_user_id_from_access_token(st))
    if not jwt_ok:
        return ""

    env_key = _studio_fallback_api_key()
    if env_key:
        return env_key

    resolver = getattr(_supabase, "resolve_single_tenant_api_key_for_studio", None)
    if callable(resolver):
        auto = resolver()
        if auto:
            return (auto or "").strip()
    return ""


def _tenant_from_api_key(api_key: str) -> Tenant:
    tenant = _lookup_tenant(api_key)
    if tenant == "inactive":
        raise HTTPException(status_code=403, detail="API key is deactivated")
    if tenant is None:
        raise HTTPException(
            status_code=401,
            detail=(
                "Invalid API key — no matching active row in api_tenants. "
                "Paste the same sk-tvd-… key as in your Supabase table, or set STUDIO_FALLBACK_API_KEY "
                "to that key. If api_tenants uses RLS, set SUPABASE_SERVICE_ROLE_KEY on the server."
            ),
        )
    return tenant


_INVALID_API_KEY_DETAIL = (
    "Invalid API key — no matching active row in api_tenants. "
    "Paste the same sk-tvd-… key as in your Supabase table, or set STUDIO_FALLBACK_API_KEY "
    "to that key. If api_tenants uses RLS, set SUPABASE_SERVICE_ROLE_KEY on the server."
)


def _resolve_tenant_for_request(
    request: Request,
    *,
    token_query: Optional[str],
    allow_query_studio_jwt: bool,
) -> Tenant:
    """Resolve tenant: valid Bearer wins; if Bearer is present but unknown, retry Studio JWT path.

    Studio often sends ``Authorization: Bearer dev`` together with ``X-Studio-User-Token``.
    The client may delete ``dev`` on some code paths but not others, or the user may paste a stale
    sk-tvd key. If JWT is valid and the server has STUDIO_FALLBACK_API_KEY or a single api_tenants
    row, we still authenticate instead of failing on the bad Bearer first.
    """
    bearer_or_query = _extract_api_key(request, token_query)
    if bearer_or_query:
        looked = _lookup_tenant(bearer_or_query)
        if looked == "inactive":
            raise HTTPException(status_code=403, detail="API key is deactivated")
        if isinstance(looked, Tenant):
            return looked
        # Unknown / placeholder Bearer (e.g. dev) — fall through to JWT + implicit tenant key
        implicit = _implicit_studio_tenant_api_key(
            request, allow_query_studio_jwt=allow_query_studio_jwt
        )
        if implicit:
            t2 = _lookup_tenant(implicit)
            if t2 == "inactive":
                raise HTTPException(status_code=403, detail="API key is deactivated")
            if isinstance(t2, Tenant):
                return t2
        raise HTTPException(status_code=401, detail=_INVALID_API_KEY_DETAIL)

    implicit_only = _implicit_studio_tenant_api_key(
        request, allow_query_studio_jwt=allow_query_studio_jwt
    )
    if not implicit_only:
        raise HTTPException(
            status_code=401,
            detail=(
                "Missing API key. Paste sk-tvd-... in the Studio header, or sign in with a "
                "cloud account and ensure SUPABASE_SERVICE_ROLE_KEY is set so the server can "
                "resolve a single api_tenants row (or set STUDIO_FALLBACK_API_KEY to that key)."
                + (
                    " For SSE or <audio> preview, add ?token=sk-tvd-... or ?studio_user_token=..."
                    if allow_query_studio_jwt
                    else ""
                )
            ),
        )
    return _tenant_from_api_key(implicit_only)


async def require_tenant(request: Request) -> Tenant:
    """FastAPI dependency: require a valid API key via Bearer header.

    If the request includes a valid ``X-Studio-User-Token`` (Supabase JWT) and
    there is no Bearer key: uses ``STUDIO_FALLBACK_API_KEY`` if set, else if
    exactly one active ``api_tenants`` row exists (service role), uses that key.

    If Bearer is set but does not match any tenant, the same JWT + implicit path is tried
    (handles ``Bearer dev`` left in headers while cloud sign-in is active).
    """
    return _resolve_tenant_for_request(
        request, token_query=None, allow_query_studio_jwt=False
    )


async def require_tenant_or_token(
    request: Request,
    token: Optional[str] = Query(default=None, description="API key for SSE (EventSource can't set headers)"),
) -> Tenant:
    """FastAPI dependency: accept Bearer header OR ?token= query param.

    Used for the SSE /events endpoint where EventSource API cannot set
    custom headers. When ``?token=`` is absent, ``?studio_user_token=`` may
    be used with a valid Supabase access token and single-tenant or env fallback.
    """
    return _resolve_tenant_for_request(
        request, token_query=token, allow_query_studio_jwt=True
    )

# Supabase migrations (run order)

Run in **Supabase Dashboard → SQL Editor** for your video pipeline project.

| Order | File | Purpose |
|------:|------|---------|
| 1 | `000_video_pipeline_bootstrap.sql` | Creates `api_tenants`, `video_jobs`, `generation_usage` and one default tenant. **Use on a new empty project.** |
| 2 | `001_user_auth_sessions_videos.sql` | Studio: `user_sessions`, `user_videos`, extra columns on `video_jobs`. |
| 3 | `002_studio_characters.sql` | Studio: `studio_characters` (character library per auth user). `user_id` references `auth.users(id)` — Studio sign-in must use a real Supabase Auth user UUID. |

**Default API key** after `000` (if no tenants existed): `sk-tvd-studio-bootstrap-orcules` — paste in Studio or set `STUDIO_FALLBACK_API_KEY` in `api_pipeline/.env`.

If `video_jobs` / `api_tenants` already exist from another setup, **do not** run `000` blindly; align columns manually or adjust the script.

## Character library (`002_studio_characters.sql`) — if POST `/api/characters` returns 502

1. Run `002_studio_characters.sql` in the **same** Supabase project as `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY`.
2. The API error body lists the **Postgres message first**. Foreign key `23503` on `user_id` almost always means the Studio **JWT was issued by a different Supabase project** than the one the server uses — align `VITE_SUPABASE_URL` / anon key in Studio with server `SUPABASE_URL`, or the Auth user row will not exist in `auth.users` on the server DB.
2. Ensure the API server has **`SUPABASE_SERVICE_ROLE_KEY`** set (not only the anon key).
3. **`user_id` foreign key:** the Studio `X-Studio-User-Token` must be a normal Supabase Auth session for that project (`/auth/v1/user` returns an `id` that exists in `auth.users`). A mismatch (wrong project URL, or user never signed up in that project) causes FK violations.
4. Check API container logs for `create_studio_character failed:` — the Postgres message (e.g. `42P01` missing relation, `23503` FK) is the source of truth.

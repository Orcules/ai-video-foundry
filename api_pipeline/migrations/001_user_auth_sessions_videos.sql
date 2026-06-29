-- Studio: user sessions, video gallery, job linkage.
-- Run in Supabase SQL editor (or migration tool).
-- After migration: enable Email provider in Authentication > Providers.
-- Set SUPABASE_SERVICE_ROLE_KEY on the API server so completed jobs can insert into user_videos (RLS bypass).

-- ---------------------------------------------------------------------------
-- user_sessions: full Studio wizard state per authenticated user
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.user_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES auth.users (id) ON DELETE CASCADE,
    name TEXT NOT NULL DEFAULT 'Untitled session',
    payload JSONB NOT NULL DEFAULT '{}',
    video_type TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_user_sessions_user_updated
    ON public.user_sessions (user_id, updated_at DESC);

ALTER TABLE public.user_sessions ENABLE ROW LEVEL SECURITY;

CREATE POLICY user_sessions_select_own ON public.user_sessions
    FOR SELECT TO authenticated
    USING (auth.uid() = user_id);

CREATE POLICY user_sessions_insert_own ON public.user_sessions
    FOR INSERT TO authenticated
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY user_sessions_update_own ON public.user_sessions
    FOR UPDATE TO authenticated
    USING (auth.uid() = user_id)
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY user_sessions_delete_own ON public.user_sessions
    FOR DELETE TO authenticated
    USING (auth.uid() = user_id);

-- ---------------------------------------------------------------------------
-- video_jobs: optional Studio user + session linkage (API still uses tenant_id)
-- ---------------------------------------------------------------------------
ALTER TABLE public.video_jobs
    ADD COLUMN IF NOT EXISTS user_id UUID,
    ADD COLUMN IF NOT EXISTS studio_session_id UUID;

CREATE INDEX IF NOT EXISTS idx_video_jobs_user_id ON public.video_jobs (user_id)
    WHERE user_id IS NOT NULL;

-- Optional: let authenticated users read their own jobs (Studio polling uses API key, not JWT — informational)
-- Uncomment if you add client-side job reads via Supabase:
-- ALTER TABLE public.video_jobs ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY video_jobs_select_own_user ON public.video_jobs
--     FOR SELECT TO authenticated
--     USING (user_id IS NOT NULL AND auth.uid() = user_id);

-- ---------------------------------------------------------------------------
-- user_videos: gallery rows (INSERT via service role from API server only)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.user_videos (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES auth.users (id) ON DELETE CASCADE,
    session_id UUID REFERENCES public.user_sessions (id) ON DELETE SET NULL,
    job_id UUID REFERENCES public.video_jobs (id) ON DELETE SET NULL,
    title TEXT NOT NULL DEFAULT 'Video',
    video_type TEXT,
    thumbnail_url TEXT,
    video_url TEXT,
    video_no_subs_url TEXT,
    duration_s REAL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_user_videos_user_created
    ON public.user_videos (user_id, created_at DESC);

ALTER TABLE public.user_videos ENABLE ROW LEVEL SECURITY;

CREATE POLICY user_videos_select_own ON public.user_videos
    FOR SELECT TO authenticated
    USING (auth.uid() = user_id);

CREATE POLICY user_videos_delete_own ON public.user_videos
    FOR DELETE TO authenticated
    USING (auth.uid() = user_id);

-- No INSERT/UPDATE for authenticated — server uses service_role to insert.

COMMENT ON TABLE public.user_sessions IS 'Video Studio wizard state per Supabase Auth user';
COMMENT ON TABLE public.user_videos IS 'Completed Studio videos; rows inserted by API (service role)';

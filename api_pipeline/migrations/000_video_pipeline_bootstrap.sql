-- Video API — bootstrap schema for a NEW Supabase project (empty public schema).
-- Run once in Supabase → SQL Editor, then run 001_user_auth_sessions_videos.sql
--
-- After this script:
--   1. Default tenant API key is: sk-tvd-studio-bootstrap-orcules
--      Paste it in Video Studio header OR set STUDIO_FALLBACK_API_KEY to the same value in api_pipeline/.env
--   2. Rotate api_tenants.api_key in production (Dashboard → Table Editor).
--
-- RLS is OFF on api_tenants, video_jobs, generation_usage so the backend (anon/publishable key) can CRUD.
-- Studio tables in 001 use RLS for authenticated users only.

-- ---------------------------------------------------------------------------
-- api_tenants
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.api_tenants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    api_key TEXT NOT NULL UNIQUE,
    is_active BOOLEAN NOT NULL DEFAULT true,
    max_concurrent_jobs INTEGER,
    max_concurrent_per_customer INTEGER,
    max_queued_jobs INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE public.api_tenants DISABLE ROW LEVEL SECURITY;

-- One default tenant only if the table is empty
INSERT INTO public.api_tenants (name, api_key, is_active)
SELECT 'studio', 'sk-tvd-studio-bootstrap-orcules', true
WHERE NOT EXISTS (SELECT 1 FROM public.api_tenants LIMIT 1);

-- ---------------------------------------------------------------------------
-- video_jobs
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.video_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES public.api_tenants (id) ON DELETE RESTRICT,
    customer_id TEXT,
    user_id UUID,
    studio_session_id UUID,
    video_type TEXT NOT NULL,
    input_params JSONB NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    progress INTEGER NOT NULL DEFAULT 0,
    current_step TEXT NOT NULL DEFAULT 'queued',
    intermediates JSONB NOT NULL DEFAULT '{}',
    output JSONB NOT NULL DEFAULT '{}',
    error TEXT,
    error_details JSONB,
    failed_at_step TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    step_timings JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE public.video_jobs DISABLE ROW LEVEL SECURITY;

CREATE INDEX IF NOT EXISTS idx_video_jobs_tenant_id ON public.video_jobs (tenant_id);
CREATE INDEX IF NOT EXISTS idx_video_jobs_tenant_status ON public.video_jobs (tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_video_jobs_created_at ON public.video_jobs (created_at DESC);

-- ---------------------------------------------------------------------------
-- generation_usage
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.generation_usage (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id UUID NOT NULL REFERENCES public.video_jobs (id) ON DELETE CASCADE,
    total_cost NUMERIC NOT NULL DEFAULT 0,
    pricing_version TEXT,
    total_input_tokens INTEGER NOT NULL DEFAULT 0,
    total_output_tokens INTEGER NOT NULL DEFAULT 0,
    total_video_seconds NUMERIC NOT NULL DEFAULT 0,
    total_image_count INTEGER NOT NULL DEFAULT 0,
    breakdown JSONB NOT NULL DEFAULT '{}',
    entries JSONB NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE public.generation_usage DISABLE ROW LEVEL SECURITY;

CREATE INDEX IF NOT EXISTS idx_generation_usage_job_id ON public.generation_usage (job_id);

COMMENT ON TABLE public.api_tenants IS 'Video API tenant isolation + API keys (sk-tvd-...)';
COMMENT ON TABLE public.video_jobs IS 'Video generation jobs (wrapper + monolith)';
COMMENT ON TABLE public.generation_usage IS 'Per-job cost breakdown';

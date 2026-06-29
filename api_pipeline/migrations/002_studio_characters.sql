-- Studio: reusable character library per Supabase Auth user.
-- Run after 001_user_auth_sessions_videos.sql
-- API uses SUPABASE_SERVICE_ROLE_KEY and scopes all queries by verified user_id (X-Studio-User-Token).

CREATE TABLE IF NOT EXISTS public.studio_characters (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES auth.users (id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'uploaded',
    status TEXT NOT NULL DEFAULT 'active',
    tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    thumbnail TEXT,
    reference_images JSONB NOT NULL DEFAULT '[]'::jsonb,
    voice_reference TEXT,
    default_language TEXT,
    preferred_formats JSONB NOT NULL DEFAULT '[]'::jsonb,
    character_dna JSONB NOT NULL DEFAULT '{}'::jsonb,
    style_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    voice_profile JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_studio_characters_user_updated
    ON public.studio_characters (user_id, updated_at DESC);

ALTER TABLE public.studio_characters ENABLE ROW LEVEL SECURITY;

CREATE POLICY studio_characters_select_own ON public.studio_characters
    FOR SELECT TO authenticated
    USING (auth.uid() = user_id);

CREATE POLICY studio_characters_insert_own ON public.studio_characters
    FOR INSERT TO authenticated
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY studio_characters_update_own ON public.studio_characters
    FOR UPDATE TO authenticated
    USING (auth.uid() = user_id)
    WITH CHECK (auth.uid() = user_id);

CREATE POLICY studio_characters_delete_own ON public.studio_characters
    FOR DELETE TO authenticated
    USING (auth.uid() = user_id);

COMMENT ON TABLE public.studio_characters IS 'Saved spokesperson/character presets for Video Studio; full CRUD also via API (service role + JWT user scope)';

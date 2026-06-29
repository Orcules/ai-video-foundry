# Project Overview

## Purpose

AI Video Foundry is an automated AI video generation platform that produces short-form marketing videos from a text prompt and a set of optional media assets. It orchestrates multiple AI services end-to-end — text analysis, image generation, video animation, voiceover, music, subtitles, and final assembly — and delivers a finished video file through a CDN.

The system exists in two layers:

- **Monolith** (`Comp_Videos/`) — the core generation engine. Contains all pipeline logic, service integrations, and prompt templates.
- **API Wrapper** (`api_pipeline/`) — a thin FastAPI layer on top of the monolith. Adds job management, SSE streaming, Supabase storage, Mux CDN upload, cost tracking, simulation mode, and the Studio web UI.

---

## Target Audience

- **End users (business / marketing):** Submit a product or campaign prompt through the Studio UI, review AI-generated text and scene descriptions, and receive a finished social-media video.
- **Internal operators:** Monitor jobs via the Dashboard, inspect logs, retry or abort failed jobs.
- **Developers:** Run the monolith directly from Google Sheets for batch processing and algorithmic development.

---

## Pipeline Types

| Type | Alias | Description |
|------|-------|-------------|
| **Product Video** | `"product video"` | Third-person product ad. Gemini images + Veo/Kling animation, standard VO, Suno music. |
| **Influencer** | `"influencer"` / `"UGC-style video"` | First-person UGC/Instagram style. Influencer character in every scene, expressive VO, 0.4s dissolve transitions, asset clip insertion. |
| **Personal Brand** | `"personal-brand"` / `"personal-service"` | Professional self-promotion. VO-first flow with `|||` scene markers, beat-sync trim. |

---

## Main Features

- **Phased generation with checkpoints** — each pipeline pauses after key steps (parse, VO, scenes) so the user can review and edit before continuing.
- **Multi-AI orchestration** — Gemini (text + image), Veo 3 (video), Kling / Runway (video fallback), ElevenLabs (TTS), Suno (music), Rendi (FFmpeg concat), ZapCap (subtitles).
- **Studio UI** — step-by-step wizard with Supabase authentication, cloud video gallery, file uploads, and SSE real-time progress.
- **Dashboard** — full job monitoring, cost display, abort/pause/retry controls, simulation mode.
- **Simulation mode** — runs the full step flow with mock services, no real API calls, for UI testing.
- **Cost tracking** — per-call cost computed from `pricing.json` and stored per job.
- **Checkpoint / resume / abort** — jobs can be paused at any step and resumed later with edited intermediates.
- **Video failover** — if primary video model fails, falls back through a configurable chain (e.g. Veo → Runway → Kling).

---

## User Flow

```
User enters prompt + uploads assets (images, logo, character)
        │
        ▼
Studio wizard Step 1 — select video type, style, duration
        │
        ▼
API POST /api/generate  (pause_after_step=step_1)
        │
        ▼
Phase 1: Parse prompt → Headline / Key Message / CTA appear for review
        │
        ▼ (user edits if needed, clicks "Generate VO")
Phase 2: VO script generation → VO text appears for review
        │
        ▼ (user edits if needed, clicks "Generate Scenes")
Phase 3: Scene prompts → images → animation → music → assembly → subtitles
        │
        ▼
Final video delivered via Mux CDN → saved to cloud gallery
```

---

## Tech Stack

| Layer | Technology |
|-------|------------|
| Backend runtime | Python 3.11, FastAPI, Uvicorn |
| Job store | Supabase (PostgreSQL) + in-memory fallback |
| File storage | Google Cloud Storage (GCS) |
| CDN delivery | Mux |
| Container | Docker + docker-compose (bind mounts for live reload) |
| Frontend | Vanilla JS + HTML/CSS (no framework) |
| LLM — text | Google Gemini (Vertex AI), OpenAI GPT-4o, Vercel AI Hub |
| LLM — image | Gemini Image (Vertex AI), Nano Banana (Kie.ai) |
| Video generation | Veo 3 / 3.1 (Vertex AI), Kling 2.5/2.6 (Kie.ai), Runway Gen4/4.5, fal.ai |
| TTS | ElevenLabs |
| Music | Suno (via Kie.ai) |
| FFmpeg | Rendi.dev (cloud), local FFmpeg fallback |
| Subtitles | ZapCap |
| Auth | Supabase Auth |

---

## Repository Structure

```
ai-video-foundry/
├── Comp_Videos/          # Monolith: generation engine + tvd_pipeline package
│   └── tvd_pipeline/     # Pipelines, services, providers, tasks, config, prompts
├── api_pipeline/         # API wrapper: FastAPI server, Studio UI, Dashboard
│   ├── wrapper/          # monolith_bridge, input_translator, progress_callback
│   ├── playgrounds/      # Studio UI, Dashboard, Playground
│   ├── services/         # ServiceRegistry, SimServiceRegistry, sim runner
│   └── config/           # pricing.json, resolution_tiers.json, server.json
├── README.md             # Start here
├── PROJECT_OVERVIEW.md   # This file
└── ARCHITECTURE.md       # System architecture and data flows
```

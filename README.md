# AI Video Foundry

**An AI video generation platform that turns a text prompt and a handful of assets into a finished, CDN-delivered short-form marketing video.**

![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white) ![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white) ![Docker](https://img.shields.io/badge/Docker-2496ED?logo=docker&logoColor=white) ![Supabase](https://img.shields.io/badge/Supabase-3FCF8E?logo=supabase&logoColor=white) ![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)

---

## 🎯 Overview

Producing short-form marketing video is slow and expensive: it means coordinating copywriting, image creation, animation, voiceover, music, editing, and subtitling. AI Video Foundry collapses that whole workflow into a single prompt.

I built an end-to-end pipeline that orchestrates a dozen AI services — text analysis, image generation, video animation, voiceover, music, subtitles, and final assembly — and delivers a ready-to-publish video through a CDN. A user submits a prompt and optional assets (product images, a logo, a character), reviews the AI-generated copy and scene plan at built-in checkpoints, and receives the finished file in their gallery.

The system runs as two layers:

- **Generation engine (monolith)** — `Comp_Videos/tvd_pipeline`. All pipeline logic, service integrations, and prompt templates. It can run standalone, driven from a Google Sheet for batch processing.
- **API wrapper** — `api_pipeline`. A thin FastAPI layer on top of the engine that adds job management, real-time progress, persistence, CDN delivery, cost tracking, simulation mode, and the web UIs. It contains **zero pipeline logic** — when the engine changes, every pipeline reflects the change automatically.

## ✨ Key Features

- **Phased generation with human-review checkpoints** — pipelines pause after key steps (prompt parse, voiceover script, scene planning) so the user can review and edit intermediates before continuing.
- **Multi-AI orchestration** — coordinates Gemini, GPT-4o, Veo, Kling, Runway, ElevenLabs, Suno, Rendi, and ZapCap behind a single request.
- **Video-model failover** — if the primary video model fails, the pipeline falls back through a configurable chain (e.g. Veo → Runway → Kling).
- **Pause / resume / abort** — jobs can be paused at any step and resumed later with edited intermediates, or aborted cleanly mid-run.
- **Per-call cost tracking** — every model call is priced from `pricing.json` and accumulated per job.
- **SSE real-time progress** — step events, cost updates, and warnings stream to the UI live.
- **Simulation mode** — runs the full step flow with mock services and no real API spend, for UI testing and CI.
- **Three pipeline types** — Product, Influencer/UGC, and Personal-brand, each with its own narrative flow.
- **Two front-ends** — a Studio creation wizard (with Supabase auth and a cloud video gallery) and an operator Dashboard (job monitoring, cost display, abort/pause/retry, simulation toggle).

## 🏗️ Architecture

The codebase is intentionally split in two so generation logic and infrastructure evolve independently:

- **Monolith — `Comp_Videos/`** holds the `VideoSceneProcessor` and all pipeline, service, provider, and task code. No API or web concerns.
- **Wrapper — `api_pipeline/`** receives HTTP requests, translates parameters into engine kwargs, imports and calls the engine directly at runtime, and bridges engine progress events to SSE, Supabase, and cost tracking. After the engine returns, the wrapper uploads the final video to Mux and persists the result.

```mermaid
flowchart LR
    Prompt([Prompt + assets]) --> Parse[Parse prompt]
    Parse -.->|checkpoint: review copy| VO[Voiceover script]
    VO -.->|checkpoint: review VO| Scenes[Scene prompts]
    Scenes --> Images[Image generation]
    Images --> Anim[Video animation]
    Anim --> Music[Music generation]
    Music --> Assembly[Concat + VO/music mix]
    Assembly --> Subs[Subtitles]
    Subs --> CDN([Mux CDN delivery])
```

Two execution modes share the same engine:

| Mode | Entry point | Use |
|------|-------------|-----|
| **API server** | `POST /api/generate` → `monolith_bridge` → `VideoSceneProcessor` | Studio / Dashboard, job-managed runs |
| **Google Sheets** | `python Comp_Videos/video_scene_processor.py` | Batch processing, algorithmic development |

The wrapper exposes an SSE stream (`GET /api/jobs/{id}/events`) and lifecycle endpoints for pause, resume, abort, and retry. See [ARCHITECTURE.md](ARCHITECTURE.md) for full sequence and data-flow diagrams.

## 🧰 Tech Stack

| Layer | Technology |
|-------|------------|
| Backend runtime | Python 3.11, FastAPI, Uvicorn |
| Job store / auth | Supabase (PostgreSQL), in-memory fallback |
| File storage | Google Cloud Storage |
| CDN delivery | Mux |
| Containerization | Docker + docker-compose (bind mounts for live reload) |
| Frontend | Vanilla JS + HTML/CSS (no framework) |
| LLM — text | Google Gemini (Vertex AI), OpenAI GPT-4o |
| LLM — image | Gemini Image (Vertex AI), Nano Banana (Kie.ai) |
| Video generation | Veo 3 / 3.1 (Vertex AI), Kling (Kie.ai), Runway Gen4 / 4.5, fal.ai |
| Text-to-speech | ElevenLabs |
| Music | Suno (via Kie.ai) |
| FFmpeg | Rendi.dev (cloud), local FFmpeg fallback |
| Subtitles | ZapCap |

## 🚀 Getting Started

### Prerequisites

- **Docker** and **docker-compose** (recommended), or **Python 3.11** with FFmpeg installed locally
- A **Supabase** project (required for real jobs, Studio sign-in, and the cloud gallery)
- API keys for the AI services you intend to use (see `.env.example` for the full list)

### Setup

```bash
# 1. Copy the environment template and fill in your own keys
cp api_pipeline/.env.example api_pipeline/.env

# 2. Edit api_pipeline/.env — set SUPABASE_URL + key, and the
#    provider API keys you need (OpenAI, Kie, ElevenLabs, Rendi, ZapCap, Mux, GCS).
#    The wrapper itself only needs GCS + Mux; the rest are used by the engine.

# 3. Start the stack
docker-compose -f api_pipeline/docker-compose.yml up
```

The API and both UIs are served on port **8000**:

- Studio wizard — `http://localhost:8000/studio`
- Dashboard — `http://localhost:8000/dashboard`
- Pipeline flow diagrams — `http://localhost:8000/playground`

> **Tip:** start in **simulation mode** (toggle in the Dashboard) to exercise the full flow without spending on real API calls.

To run without Docker, install dependencies and start Uvicorn directly:

```bash
pip install -r api_pipeline/requirements.txt
uvicorn api_pipeline.server:app --reload --port 8000
```

> 🔒 **Never commit secrets.** `.env.example` ships with placeholders only. Keep your real `.env`, service-account JSON, and any tenant keys out of version control.

## 🎬 Pipeline Types

| Type | Style | Highlights |
|------|-------|------------|
| **Product video** | Third-person product ad | Gemini images + Veo/Kling animation, standard voiceover, Suno music |
| **Influencer / UGC** | First-person UGC / Instagram style | Influencer character in every scene, expressive voiceover, 0.4s dissolve transitions, asset-clip insertion, logo + slogan CTA |
| **Personal brand** | Professional self-promotion | Voiceover-first flow with `\|\|\|` scene markers, beat-synced trimming |

Internally, Product runs `process_product_video()`; Influencer and Personal-brand both route through `process_ugc_video()` with a distinguishing subtype.

## 📁 Project Structure

```
ai-video-foundry/
├── Comp_Videos/                 # Monolith — generation engine
│   ├── video_scene_processor.py # Entry point: VideoSceneProcessor, dispatch, main()
│   └── tvd_pipeline/
│       ├── pipelines/           # product.py · ugc.py · shared helpers
│       ├── services/            # gemini · veo3 · kie · elevenlabs · suno · rendi · zapcap · gcs
│       │   ├── providers/       # LLM provider clients (vertex · openai · vercel)
│       │   └── tasks/           # provider-agnostic task functions (parse, VO, scenes, ...)
│       └── config/              # models.json · pipeline_defaults.json · prompts/ · arcs/
│
├── api_pipeline/                # API wrapper — FastAPI, no pipeline logic
│   ├── server.py                # FastAPI routes + job lifecycle
│   ├── pipeline_runner.py       # routing shim: simulation ↔ monolith bridge
│   ├── wrapper/                 # monolith_bridge · input_translator · progress_callback
│   ├── services/                # ServiceRegistry · SimServiceRegistry · sim runner
│   ├── config/                  # pricing.json · resolution_tiers.json · server.json
│   ├── playgrounds/             # studio/ · dashboard/ · playground/ UIs
│   └── migrations/              # Supabase schema
│
├── PROJECT_OVERVIEW.md          # Product overview, user flow, pipeline types
└── ARCHITECTURE.md              # System architecture, data flows, diagrams
```

## 📚 Documentation

- **[PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md)** — purpose, target audience, user flow, and feature summary.
- **[ARCHITECTURE.md](ARCHITECTURE.md)** — two-layer design, request data flow, progress-callback protocol, input translation, resolution tiers, and the full set of diagrams.

---

*Built by an Automation + Gen AI engineer as a portfolio project. The platform integrates many third-party AI services; running it end-to-end requires your own API keys.*

# The Director's full arsenal — every tool, every category

You build videos in five fundamentally different ways. Most video briefs solve best by combining 2–3 of them. This document maps every approach available to you, with capabilities, limits, costs, and the situations where each one wins.

The five categories:

1. **AI video generation models** — T2V / I2V / multi-shot / lipsync (closed-API and open-source)
2. **Programmatic frame-accurate frameworks** — HTML/CSS, React, Python, JSON-vector. You write code (or the agent does) → the framework renders MP4.
3. **Image-first models** — T2I / I2I / Ingredients-style references. The first frame the user sees and approves.
4. **Audio generation** — voiceover (ElevenLabs), music (Suno), SFX, lipsync audio
5. **Editing & assembly** — FFmpeg/Rendi for concat, ZapCap for subtitles, transition rendering, audio mix

Every storyboard you build is a composition across these. The skill is matching each scene's intent to the right category, not jamming everything through the same model.

---

## 1. AI VIDEO GENERATION MODELS

### 1a. Closed-API models we have wired today

| Model | Provider | Modality | When it wins | Duration | Cost/sec |
|---|---|---|---|---|---|
| **Veo 3.1 Fast** | vertex direct | I2V + native audio | **Default for everything**. Spatial audio (48kHz stereo), strongest lip-sync, 40-60% better frame consistency vs 3.0 | 4/6/8s | $0.10 |
| **Veo 3.1** | vertex direct | I2V + native audio | Hero quality | 4/6/8s | $0.20 |
| **Veo 3.1 Ref Fast** | vertex direct | **Ref-to-Video (Ingredients)** | Character/venue lock across shots — up to 3 reference images | 8s only | $0.10 |
| **Veo 3.1 Ref** | vertex direct | Ref-to-Video | Ref Fast at hero quality | 8s only | $0.20 |
| **Seedance 2.0** | kie | **Multi-shot I2V/T2V** | "Cut to / then" beats in ONE call. 9 image refs + 3 video refs + 3 audio refs | 4–15s | $0.09 |
| **Kling 2.6** | kie | I2V | Heavy camera motion (dolly, orbit, whip pan) + best text legibility for motion graphics | 5 / 10s | $0.055 |
| **Kling Avatar Pro** | kie | **Lipsync** (still + audio) | Talking-head clips with pre-recorded VO | from audio | $0.08 / clip |
| **Runway Gen 4.5** | runway direct | I2V | **Top photorealism** (Elo #1, 1247). Cinematic photography polish, brand-quality | 2–10s | $0.12 |
| **Runway Gen 4 Turbo** | runway direct | I2V | Budget cinematic | 2–10s | $0.05 |

### 1b. New closed-API models worth adopting

| Model | Provider | What's new | Why it matters |
|---|---|---|---|
| **Gemini Omni** ([Google, May 2026](https://www.mindstudio.ai/blog/what-is-google-gemini-omni-multimodal-video-model)) | Vertex | Any-input-to-video (text / image / audio). 2026 benchmark winner for **Object Permanence** — characters walking behind objects emerge with identical clothing/face. Flash variant <15s for 5-sec 1080p preview. | When the brief needs **multi-modal grounding** (start from a song clip, end with brand audio cue). Object permanence beats Veo on continuity scenes. |
| **OmniHuman** ([ByteDance](https://www.datacamp.com/blog/omnihuman)) | TBD (Volc/ByteDance) | One image + audio → realistic human animation via "omni-conditions" training (text + audio + body movements). | Use when the user uploads a single founder/host photo and pre-recorded VO and wants a talking head WITHOUT Kling Avatar Pro's stylized look. |
| **Qwen 3.5 Omni** ([Alibaba](https://www.mindstudio.ai/blog/what-is-qwen-3-5-omni-alibaba-multimodal)) | TBD | Full multimodal in + out | Backup omni-multimodal option. |

### 1c. Open-source video models (self-host or via Wavespeed/Replicate/fal)

When the user's brief screams "I want a stylized look" or cost is dominant, open-source models beat closed APIs on $/quality.

| Model | License | Strength | Hardware | Best for |
|---|---|---|---|---|
| **Wan 2.2** ([Alibaba](https://www.aimagicx.com/blog/open-source-ai-video-models-comparison-2026)) | Open | **Best photorealistic humans in open source** — face detail, skin texture, hair. MoE architecture. | 24GB+ GPU | Realistic human shots when Veo budget too high |
| **HunyuanVideo 1.5** | Tencent Community License (free <100M MAU) | Top overall quality | 40–80GB (cloud) | Premium-quality open-source generation |
| **LTX-Video 13B** ([Lightricks](https://ltx.io/blog/best-open-source-video-generation-models)) | Open | **Fastest** — 700M variant generates 5s in <10s on consumer GPU | 8–16GB | Real-time iteration, drafts |
| **Mochi 1** ([Genmo](https://www.aimagicx.com/blog/open-source-ai-video-models-comparison-2026)) | **Apache 2.0** | Best T2V open-source. Cleanest commercial license. | 24GB | Production work where licensing matters |
| **CogVideoX** (Zhipu) | Open | Best I2V open-source | 8–16GB | I2V on commodity hardware |
| **Open-Sora**, **Allegro** | Open | Alternative architectures | varies | Experimentation |

**Integration path**: route open-source models through [Wavespeed.ai](https://wavespeed.ai/landing/models/best-open-source-video-models-2026), Replicate, or fal.ai — all expose them as REST APIs with per-second pricing similar to Kie. No GPU infrastructure needed.

---

## 2. PROGRAMMATIC FRAME-ACCURATE FRAMEWORKS

These are not AI models. They are **deterministic renderers** — same input always produces same MP4. The agent (or a code-generator sub-agent) writes code in the framework's language; the framework renders. Best for: stat callouts, charts, kinetic text, UI demos, transitions, brand cards, end screens — anything where pixel-perfect repeatability beats aesthetic creativity.

### 2a. HyperFrames (HeyGen, open source) — HTML/CSS/JS → MP4

[HyperFrames](https://www.mindstudio.ai/blog/what-is-hyperframes-ai-video-rendering) is the newest entry, designed *for* AI agents. The agent writes HTML+CSS+JS describing keyframes, transitions, overlays; HyperFrames renders to video via API.

**When it wins**:
- The user uploads a brand site or PDF; agent extracts colors/fonts/logos and writes HTML that matches their brand exactly.
- Frame-accurate timing matters (3.7s wait, exactly 12 frames of fade).
- Stat callouts, "feature reveal" screens, brand cards, end screens.
- The user wants a *deterministic* result they can re-render with different copy.

**LLM advantage**: Claude/GPT speak HTML/CSS fluently — they can write a polished 6-second branded scene in one prompt without hallucinating frames.

**Workflow**: agent captures brand assets → writes storyboard as HTML → POSTs HTML to HyperFrames API → receives MP4 URL.

### 2b. Remotion (React/TSX → MP4) — the most-installed agent skill globally

[Remotion](https://www.remotion.dev/) is React-based programmatic video. With 126K+ installs, the **#1 most-installed agent skill globally** and the most popular skill for programmatic video.

**When it wins**:
- Component-based videos (a `<Stat>` component reused across 8 scenes with different props).
- Data-driven videos (a report video where each frame's content comes from a JSON).
- High-volume rendering (a 10K personalized-name videos batch via **Remotion Lambda** on AWS).
- Anything React developers already know how to build for the web.

**Cloud rendering**: Lambda (AWS) or Cloud Run (GCP) for parallel rendering. The agent can split a 60-second video into 600 chunks and render in 30 seconds total.

**LLM advantage**: every modern LLM can write polished React/TSX without help. Remotion ships an [official "System Prompt for LLMs"](https://www.remotion.dev/docs/ai/system-prompt) and an Agent Skill.

### 2c. Manim (Python) — math, physics, explainer animations

[Manim](https://www.manim.community/) is the engine behind 3Blue1Brown's videos. Pure Python.

**When it wins**:
- The brief is **explanatory** — math, physics, algorithms, "how it works" diagrams.
- The user wants visual proofs, equation manipulations, geometric constructions.
- Educational content where the animation IS the explanation.

**LLM-driven workflow** ([Manimator](https://arxiv.org/html/2507.14306v1) / [Math-To-Manim](https://github.com/HarleyCoops/Math-To-Manim)):
1. LLM agent parses the user's prompt (or research paper) into a scene description.
2. Second agent writes the Manim Python code.
3. Code runs to render the video.
4. Optional self-repair loop on render errors.

Math-To-Manim uses a 6-agent planning chain: `intent → cartographer → curriculum → math-director → cinematographer → scene-composer` then `codegen → static checks → render → self-repair`.

### 2d. Lottie (JSON vector) — UI animations, kinetic text, embeddable loops

Lottie is the JSON-vector animation format from Airbnb. Small file size, scales infinitely, plays on web/mobile/After Effects.

**When it wins**:
- Loading spinners, success checkmarks, micro-interactions
- Kinetic typography that needs to be perfectly sharp at any size
- Animation that will be embedded in a website or mobile app (not just exported as MP4)
- The video needs to play smoothly on a 4-year-old phone

**Generators** ([LottieGen](https://lottiegenai.webflow.io/), [vizGPT](https://vizgpt.ai/usecases/ai-generate-lottie), [Lottie Creator](https://lottiefiles.com/lottie-creator)):
Text prompt → JSON Lottie file. Can be played as-is or rendered to MP4 via Lottie-Web + headless browser.

**Hybrid workflow**: generate the Lottie JSON for a kinetic-text element, overlay it on top of a Veo-generated scene as a transparent layer in FFmpeg.

### 2e. When to reach for a framework vs an AI model

| Need | Reach for |
|---|---|
| Real-world photoreal moments | AI video model (Veo / Seedance / Runway) |
| Brand cards, end screens, stat reveals, UI demos | HyperFrames or Remotion |
| Math / physics / algorithm explainer | Manim |
| Tiny embeddable animation (loading, success) | Lottie |
| Pixel-perfect repeatable rendering at multiple sizes | Remotion |
| Mixing brand chrome over a generated scene | Lottie overlay on top of Veo output |

The frameworks are **free to call** (no per-generation API cost). They cost in render compute, which is trivial.

---

## 3. IMAGE-FIRST MODELS (storyboard preview phase)

Covered in detail in `director_models_reference.md`. Quick recap:

| Model | When it wins |
|---|---|
| **Nano Banana Pro** | Hero shots, on-image text, multi-ref (up to 8) |
| **Nano Banana 2** | Fast drafts + **Image Search Grounding** for real landmarks/brands/public figures |
| **Gemini 3 Pro Image** | Vertex-native equivalent of Banana Pro |
| **Gemini 3.1 Flash Image** | Fast batch, low cost |
| **Imagen 4 Ultra** (not yet wired) | **#1 for product photography** — micro-detail, physics-correct fabric/light |
| **Seedream 4.5** (not yet wired) | Best text rendering in any image model. Multi-image fusion (up to 14 refs) |

---

## 4. AUDIO

| Tool | Modality | Wired? | Use for |
|---|---|---|---|
| **ElevenLabs Eleven v3** | TTS (voice) | ✓ | Voiceover. Per-clip lipsync audio for Kling Avatar Pro / OmniHuman. |
| **Suno V5** | Music gen | ✓ | Background music. Pure mode + cover + upload-reference. |
| **OmniHuman** (audio in) | Audio + image → talking video | Future | Talking head when the user has already recorded VO. |
| **Veo 3.1 native audio** | Spatial audio (48kHz stereo) | ✓ | Automatic when Veo 3.1 generates a scene — no separate call needed. |

---

## 5. EDITING & ASSEMBLY

### 5a. What we have

- **Rendi (FFmpeg-as-a-service)** — concat, trim, dissolve transitions, Ken Burns pan/zoom, audio mix, slow-motion
- **ZapCap** — burned-in subtitles with styling templates

### 5b. What's industry-best (and what we can adopt)

| Tool | What it does best | Adoption path for us |
|---|---|---|
| **[FFmpeg + AI agent](https://aividpipeline.com/skills/aivp-edit)** (AIVP Edit Skill) | Manifest-driven pipeline: ffprobe → transcode-to-spec → scene-detect → concat/overlay/transition graph builder | We already have Rendi as the FFmpeg layer. Adopt the **manifest-driven approach**: emit a `pipeline_manifest.json` per job documenting every FFmpeg call. |
| **[CapCut AI](https://www.capcut.com/resource/8-top-ai-video-platforms)** | Auto-captions, BG removal, TTS, multi-platform format presets (TikTok/Reels/YT Shorts) | Presets we should adopt: per-platform aspect ratio + safe-zone + max-duration + caption style. |
| **[Descript](https://dupple.com/learn/best-ai-for-video-editing)** | Edit-via-transcript. Delete a sentence in the script → corresponding video clip removed. Voice clone fixes. | Useful for the "user wants to tweak VO" loop — re-transcribe with Whisper, edit text, re-render only the affected scenes. |
| **[Pictory](https://www.capcut.com/resource/8-top-ai-video-platforms)** | Long-form → short clips (highlight extraction from webinars/podcasts) | Adopt the **"highlight extraction" mode** as a new pipeline preset: upload a 30-min video, Gemini analyzes for moments, FFmpeg cuts. |

### 5c. End-to-end FFmpeg pipeline best practices (from research)

1. **Normalize on ingest**: ffprobe metadata → if mismatch with target spec, transcode → write a manifest.
2. **Scene detection**: optional PySceneDetect pass (we have this in `legacy.py`) to mark cut points.
3. **Manifest as source of truth**: every FFmpeg call records its inputs/filter/output in a JSON; lets the agent debug failures by replaying the manifest.
4. **Per-platform presets**: YouTube / TikTok / Reels / Twitter — each has aspect ratio, max duration, caption-safe zones, audio LUFS targets.
5. **Human-in-the-loop checkpoints**: storyboard, image previews, rough cut — surface intermediate outputs before final render. This is the single biggest "feels professional" lever.

---

## ROUTING DECISION TREE — pick the right tool

When you build a scene, decide WHICH CATEGORY first, THEN which tool within it.

### Step 1: Classify each scene by intent

| Brief intent | Category to use |
|---|---|
| "Real moment" (a chef plating, a customer talking, a landscape) | AI video model (1) |
| "Branded stat / feature card / end screen" | Programmatic framework (2) — HyperFrames or Remotion |
| "Math / physics / algorithm visualization" | Manim (2c) |
| "UI animation / loader / micro-interaction" | Lottie (2d) |
| "Repurpose existing footage" | Editing & assembly (5) — Pictory-style highlight extraction |
| "Talking head with my recorded voice" | OmniHuman or Kling Avatar Pro (1) |

### Step 2: Within AI video models — model picking

(See `director_models_reference.md` for the full flow.)

Short version:
1. Multi-shot with locked refs → Seedance 2.0
2. Single shot with character lock → Veo 3.1 Ref Fast (8s) or Wan 2.2 (open-source budget)
3. Heavy camera motion → Kling 2.6
4. Hero photoreal → Runway Gen 4.5
5. Default I2V → Veo 3.1 Fast
6. Cost-dominant brief → LTX-Video or Mochi 1 via Wavespeed/fal

### Step 3: Within programmatic frameworks — framework picking

| Need | Pick |
|---|---|
| Agent writes branded scene from brand site/PDF | HyperFrames |
| Component-based, data-driven, high-volume batch | Remotion |
| Educational/math visualization | Manim |
| Tiny embeddable vector loop | Lottie |
| Static text overlay on AI scene | Lottie or FFmpeg drawtext |

### Step 4: Stitch with the editing layer

Always end with the editing & assembly layer:
1. Concat scenes with dissolve transitions (Rendi)
2. Mix VO + music (Rendi)
3. Burn subtitles (ZapCap)
4. Apply per-platform preset (aspect, safe zone, LUFS)
5. Upload to CDN (Mux)

---

## WORKED EXAMPLES — how to think across categories

These examples show the kind of multi-category reasoning that distinguishes a good Director from a one-trick agent. Read them as a pattern, not a recipe.

### Example A — "30s explainer about how compound interest works"

The brief is **educational**. Don't pick AI video for math — pick Manim. But the user wants 30 seconds, so blend:

| Scene | Duration | Category | Tool / Model | Why |
|---|---|---|---|---|
| 1 — Hook ("What if $1 became $40?") | 3s | Programmatic framework | Remotion or HyperFrames | Kinetic text reveal. Sharp typography. Brand-friendly. |
| 2 — Person curious | 2s | AI video | Veo 3.1 Fast (T2I → I2V) | Real human reaction, 2 seconds. |
| 3 — Math derivation (A = P(1+r/n)^nt) | 12s | Programmatic framework | **Manim** | The formula draws itself, terms highlight as the VO explains them. AI video cannot do this. |
| 4 — Comparison chart (10y vs 30y) | 6s | Programmatic framework | Remotion | Bar chart animates as numbers count up. Data-driven. |
| 5 — CTA ("Start investing today") | 4s | Programmatic framework | HyperFrames or Lottie | Branded card, clean text. Logo at the end. |
| Audio | — | Audio | ElevenLabs (narration) + Suno (light ambient) | VO is the spine. Music is low. |

Note: scenes 1, 3, 4, 5 = framework. Scene 2 = AI video. **80% of this video is NOT AI video** — that's the right call for an explainer.

### Example B — "60s influencer promo for my new coffee brand. Here's my photo"

The brief is **brand+person**. Veo 3.1 Ref Fast for character lock, generated product shots, branded chrome at the end.

| Scene | Duration | Category | Tool / Model | Why |
|---|---|---|---|---|
| 1 — Hook (founder talks to camera) | 5s | AI video | Veo 3.1 Ref Fast | 8s ref-locked clip. Character consistency mandatory. |
| 2 — Coffee being poured (close-up) | 4s | AI video | Runway Gen 4.5 or Veo 3.1 | Photoreal hero shot. Runway wins on photography polish. |
| 3 — Multi-shot lifestyle (cafe, beans, cup) | 12s | AI video | **Seedance 2.0** | One call, three shots, locked character. |
| 4 — Founder again with bag (face + product) | 6s | AI video | Veo 3.1 Ref Fast | Re-lock face + product as ingredient refs. |
| 5 — Customer reactions (3 quick shots) | 8s | AI video | Veo 3.1 Fast (3 × short clips) | No character lock needed. |
| 6 — Branded CTA card (logo + URL) | 5s | Programmatic framework | **HyperFrames** | Brand colors exact. Frame-accurate. |
| Audio | — | Audio | ElevenLabs (founder VO) + Suno (upbeat coffee jingle) | Music carries energy. |

Note: scene 6 is the ONLY framework scene. The rest is AI video, but the model varies per scene by intent.

### Example C — "Talking head — I recorded my voice already, just use my photo"

The brief is **lipsync from existing audio**. There's exactly one right tool: OmniHuman or Kling Avatar Pro.

| Scene | Duration | Category | Tool / Model | Why |
|---|---|---|---|---|
| 1 — Whole video, talking to camera | 60s | AI video | **OmniHuman** (or Kling Avatar Pro fallback) | Specialist tool. Veo cannot do this from still+audio. |
| Optional intro card | 2s | Programmatic framework | HyperFrames | If user wants a brand frame before they appear. |
| Optional B-roll cutaway | 3s each | AI video | Veo 3.1 Fast | Generated b-roll for visual variety while VO continues. |

Note: the WHOLE video can be one OmniHuman clip if the user has a 60s audio file. Don't over-engineer.

### Example D — "Promo for my SaaS dashboard — show the product UI in motion"

The brief is **UI + branded chrome**. AI video can't render UI faithfully. Use Remotion + screen captures.

| Scene | Duration | Category | Tool / Model | Why |
|---|---|---|---|---|
| 1 — Hook ("Cut your reporting time in half") | 3s | Programmatic framework | Remotion (kinetic text) | Brand colors, sharp text. |
| 2 — Dashboard demo (3 features) | 15s | Programmatic framework | **Remotion** | Real UI rendered as React. NOT AI video — it must show the actual product. |
| 3 — Customer testimonial (a person says one line) | 5s | AI video | Veo 3.1 Fast | A generated face is fine for a generic testimonial. |
| 4 — Bar chart "300% ROI" | 4s | Programmatic framework | Remotion | Data-driven chart. |
| 5 — Loading-to-success transition | 2s | Programmatic framework | Lottie | Tiny vector animation overlay. |
| 6 — CTA + logo | 3s | Programmatic framework | HyperFrames | Brand card. |
| Audio | — | Audio | ElevenLabs (narration, professional voice) + Suno (corporate-light) | |

Note: 5 of 6 scenes are framework. Only ONE is AI video. For SaaS demos, this is the right shape.

### Example E — "I have raw 15-minute interview. Cut me a 60s highlight."

The brief is **repurpose existing footage**. Don't generate anything new — extract.

| Step | Tool | Why |
|---|---|---|
| 1 — Transcribe interview | Whisper | Get a searchable text transcript. |
| 2 — Identify highlight moments | LLM (Gemini Flash) | Score each segment for hook potential, then pick top 4–6 segments adding up to ~60s. |
| 3 — Cut clips | FFmpeg (Rendi) | Trim each segment to its timestamps. |
| 4 — Add titles + transitions | Remotion or HyperFrames overlay | Lower-third with speaker name, B-roll caption. |
| 5 — Captions | ZapCap | Burned-in subtitles. |
| 6 — Final mix | FFmpeg (Rendi) | Concat + audio normalize. |

Note: zero AI video generation. The whole video already exists; the agent just curates.

---

## SELF-CHECK BEFORE YOU OUTPUT

Before returning the storyboard, mentally walk through this checklist:

1. **For every scene, did I justify the tool choice?** Could I explain to the user why scene 3 uses Seedance and not Kling? If not, reconsider.
2. **Did I avoid using AI video for non-real moments?** Check: any stats, charts, UI demos, branded text, math equations should be framework, NOT Veo.
3. **Did I mix categories?** A great 30–60s video usually uses 2–4 distinct categories. If 100% of clips are Veo, that's a sign you missed framework opportunities.
4. **Did I lock identity when needed?** If a character appears in 2+ scenes, every clip with them MUST be Ref-Fast or Seedance.
5. **Did I match cost to value?** Hero scenes deserve Runway/Veo Pro. Filler scenes can be Veo Fast or Ken Burns.
6. **Did I pick OmniHuman / Kling Avatar Pro instead of regular Veo** when the brief is "lipsync from my audio"?
7. **Did I check the assets I have vs assets the storyboard needs?** If the storyboard references uploaded photos that don't exist, return a `needs_assets` payload instead.

If any check fails, revise before emitting.

---

## ANTI-PATTERNS — when the agent picks wrong

- **Forcing AI video for stat reveals**: a "$1B raised in 2025" callout via Veo will be ugly + garbled text. Use Remotion or HyperFrames.
- **Using Veo for math explainers**: a derivation of the chain rule via Veo produces visual nonsense. Use Manim.
- **Using Kling Avatar Pro for non-lipsync clips**: it's specialized for "still photo + audio → talking head". Wrong tool for any other use.
- **Generating a kinetic-text scene via T2I → I2V**: the text will distort in motion. Use Lottie for the text layer, Veo for the background, FFmpeg overlay to composite.
- **One model for everything**: a great 60s promo uses 3–4 categories. AI video for product close-ups, HyperFrames for brand cards, Lottie for the CTA arrow, Rendi to assemble.

---

## THE BIG IDEA

You have FIVE different ways to make video. The user doesn't know or care which one you use. They want the result to look professional and feel intentional.

Your job is to classify each scene's intent, pick the right category, pick the right tool within the category, and stitch everything together — all without asking the user to make tool-level decisions.

The single biggest leverage point: **don't use AI video for things that aren't real-world moments**. Branded chrome, charts, kinetic text, UI, math — those want a framework, not Veo.

Read the per-tool details below or in `director_models_reference.md` before stamping `preview_image_model` and `video_model_override` on each scene/clip. When the scene type calls for a framework (HyperFrames / Remotion / Manim / Lottie), record that in `clip.type = "framework_render"` and set `clip.framework = "hyperframes" | "remotion" | "manim" | "lottie"` — these clip types are reserved for future integration phases.

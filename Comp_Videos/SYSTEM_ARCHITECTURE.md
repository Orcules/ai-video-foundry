# Video Scene Processor – Full System Architecture (AI Video Foundry Pipeline)

This document describes the **complete structure** of the automated video generation system so that any developer (or AI) can return later and understand exactly how everything is built. All in English.

---

## 1. Overview

- **Entry point:** `video_scene_processor.py` → `main()` → `VideoSceneProcessor()`.
- **Input:** Google Sheets (one sheet: `Sheet1`). Each **row** is one job. The **Video type** column decides which pipeline runs.
- **Output:** Final video URL (and intermediate URLs) written back to the sheet; assets stored in **GCS** (bucket `automatiq`).

**Two main pipelines:**

| Video type (column value) | Pipeline | Description |
|---------------------------|----------|-------------|
| `product video` | Product video | Product-focused: parse prompt → TEXT 1–4, product/scene images, scene videos, VO, music, concat, subtitles. Uses product reference images (image 1–5). |
| `UGC-style video` | UGC | Influencer-style: Character (generated or from sheet), prompt → TEXT 1–4, scene prompts with influencer, scene images (Nano Banana/Gemini), Kling/Veo/Runway animation, assets as-is, music (Suno), VO (ElevenLabs, expressive + sentence pauses), concat with **gentle dissolve**, logo+slogan in CTA, ZapCap subtitles. |

Other values (`service video`, `explainer video`) are recognized but not implemented (skipped).

---

## 2. Google Sheet Columns (Full Reference)

### 2.1 Routing and core input

| Column name | Config constant | Purpose |
|-------------|-----------------|---------|
| **Video type** | `VIDEO_TYPE_COLUMN` | Must be `product video` or `UGC-style video` (case-sensitive) to run. |
| **Prompt** | `PROMPT_COLUMN` | Main text: product/experience description. Parsed into TEXT 1–4 (UGC always re-parses). |
| **Duration** | `DURATION_COLUMN` | Target length in seconds (e.g. 25–30). Used for scene count and VO length. |
| **Style** | `STYLE_COLUMN` | Visual style (e.g. "Auto", "Modern flat 2d"). For UGC, "Auto" = ultra-realistic UGC. |
| **Language** | `LANGUAGE_COLUMN` | Language code for VO and ZapCap subtitles (e.g. `he`, `en`). |
| **Country** | `COUNTRY_COLUMN` | Used for influencer ethnicity and cultural adaptation (UGC). |
| **Add subtitles** | `ADD_SUBTITLES_COLUMN` | "Yes" / other → ZapCap subtitles on final video. |

### 2.2 UGC-specific input

| Column name | Config / note | Purpose |
|-------------|----------------|---------|
| **Character** | `CHARACTER_COLUMN` | URL of influencer image. If empty, system **generates** one (Gemini) and writes URL here. |
| **Gender** | `GENDER_COLUMN` | `m` = male influencer + male voice; `f` = female influencer + female voice. |
| **Image 1** … **Image 5** | `IMAGE_1_COLUMN` … `IMAGE_5_COLUMN` | Reference images for scenes. Analyzed by Gemini; each scene can reference one (by index). Case-insensitive fallback for header names. |
| **Asset 1**, **Asset 2**, **Asset 3** | `ASSET_1_COLUMN` … | URLs of images/videos inserted **as-is** (max 3 s each, zoom effect). All provided assets are included. |
| **Logo** | `LOGO_COLUMN` | URL of logo. Used **only in the last (CTA) scene**; combined with slogan in one image (Gemini). |
| **Slogan** / **Slogen** | `SLOGAN_COLUMN` | Slogan text for CTA. Column is looked up as "Slogan" then "Slogen" (typo), then case-insensitive. If empty, default e.g. "Try it now!". |

### 2.3 Product-video reference images

| Column name | Config constant | Purpose |
|-------------|-----------------|---------|
| **image 1** … **image 5** | `PRODUCT_IMAGE_1_COLUMN` … | Product reference images for product video pipeline. |
| **Video reference** | `VIDEO_REFERENCE_COLUMN` | Optional. Product video only: if URL is present, Gemini analyzes the video and uses its structure (scene count, narrative roles, durations) as reference for the new video; content still comes from the new prompt and product images. If empty, behavior unchanged. |

### 2.4 Optional / legacy

| Column name | Purpose |
|-------------|---------|
| **Voice id** | Custom ElevenLabs voice ID (optional). |
| **Animation model** | e.g. "kling", "runway", "google" (Veo). |
| **Time** | Number of scenes (UGC default 6 if empty). |
| **Manual text for VO**, **Manual music link**, **Free text** | Overrides / manual content. |
| **Input Videos**, **Article**, **CTA**-related, **Opening Text**, etc. | Used in other/legacy flows not detailed here. |

### 2.5 Output columns (written by pipeline)

| Column name | Content |
|-------------|---------|
| **TEXT 1**, **TEXT 2**, **TEXT 3**, **TEXT 4** | Parsed from Prompt (what video is about, goal, requirements, structure). |
| **Scene 1 - First prompt** … **Scene N - First prompt** | Image prompt per scene. |
| **Scene 1 - Second prompt** … **Scene N - Second prompt** | Motion/animation prompt per scene. |
| **Scene 1 - new image** … **Scene N - new image** | Generated scene image URLs. |
| **Scene 1 - new video** … **Scene N - new video** | Generated scene video URLs. |
| **VO** | Final voiceover script text. |
| **New Voice** | VO audio file URL. |
| **New music** | Background music URL. |
| **RENDI Scene** | Concatenated video (no audio or with audio, depending on step). |
| **RENDI Scene & Voice** | Video + VO + music mixed. |
| **Subtitled Video** | After ZapCap. |
| **Final Video** | Final output URL (with or without subtitles). |

---

## 3. Main class: `VideoSceneProcessor`

- **Location:** `video_scene_processor.py`, class `VideoSceneProcessor`.
- **Initialization:** Reads config, validates API keys, instantiates all services (see §5). No Google Sheet read in `__init__`.

**Main methods:**

- `process_product_video(...)` – Full product video pipeline (parse → images → videos → VO → music → concat → subtitles).
- `process_ugc_video(...)` – Full UGC pipeline (influencer → parse → scene prompts → images → animations → assets → music → VO → concat with dissolve → logo+slogan CTA → subtitles).
- `_generate_influencer_image(...)` – Generates or describes Character image (Gemini).
- Helper methods for assets, trimming, etc.

**Row processing:**

- `main()` reads sheet, gets headers and rows, then for each row calls a **process_row**-style helper that:
  - Reads **Video type**, **Prompt**, **Duration**, **Style**, **Language**, **Country**, **Character**, **Gender**, **Image 1–5**, **Asset 1–3**, **Logo**, **Slogan/Slogen**, etc.
  - If `video_type == "product video"` → `process_product_video(...)`.
  - If `video_type == "ugc-style video"` → `process_ugc_video(...)`.

---

## 4. UGC-style video pipeline (step-by-step)

1. **Step 0 – Influencer (Character)**  
   If **Character** is empty: generate influencer image with Gemini (gender + country/language for ethnicity). Upload to GCS, write URL to **Character**. If Character exists, optionally get a short description (Gemini) for later prompts.

2. **Step 1 – Parse prompt**  
   Always re-parse **Prompt** (with reference images if present) into TEXT 1–4 via `GeminiService.parse_product_prompt`. Write TEXT 1–4 to sheet.

3. **Step 2 – Reference image analysis**  
   For each of Image 1–5 (that have URLs), call Gemini to get a short text description. Stored for later use in scene image generation.

4. **Step 3 – Scene prompts**  
   `GeminiService.generate_influencer_prompts`: input TEXT 1–4, style, scene count, reference image descriptions, influencer description. Output: list of scenes, each with `scene_number`, `shows_influencer`, `reference_image_index`, `first_prompt`, `second_prompt`. Truncated JSON is repaired with `_fix_truncated_scene_json`. Written to **Scene N - First prompt** / **Scene N - Second prompt**.

5. **Step 4–7 – Parallel generation**  
   - **Scenes:** For each scene, `generate_scene_visual`: build image prompt (with ref + influencer context), call **GeminiImageService** (Nano Banana / Gemini image) → upload image → call **Kling V2.5** (or Veo 3 / Runway from animation model) to animate → scene video URL. CTA scene (last) uses **Logo** URL and **slogan_text**: one combined image with logo + slogan, no influencer.  
   - **Assets:** Each Asset 1–3: if image → Ken Burns (e.g. Runway); if video → trim to 3 s, scale/crop to frame.  
   - **Music:** Suno (description from Gemini) → music URL.  
   - **VO:** `GeminiService.generate_influencer_vo_script` → script. Then **ElevenLabs** `text_to_speech(script, voice_id, language, expressive=True, sentence_pause_seconds=0.5)`. VO script saved to **VO**, audio to **New Voice**.

6. **Step 8 – Concatenate**  
   Order: body scenes (with duration budget) + asset clips (each up to 3 s) + CTA scene.  
   `RendiService.concatenate_videos(video_data, video_only=True, dissolve_seconds=0.45)`: **video-only** concat with **0.45 s gentle dissolve** (xfade) between clips. Trim each clip to target duration. Output → **RENDI Scene**.

7. **Step 9 – VO + music**  
   `RendiService.add_vo_and_music_to_video(video_url, vo_url, music_url)`: mix VO (apad, volume) + music (volume), `amix duration=first`, `-shortest`. Result → **RENDI Scene & Voice**.

8. **Step 10 – Subtitles**  
   If **Add subtitles** = Yes: ZapCap with **subtitle_language**. Result → **Subtitled Video** and **Final Video**.

---

## 5. Product video pipeline (summary)

- Parse prompt → TEXT 1–4 (Gemini).
- **Optional – Video reference:** If **Video reference** column has a URL, download video → Gemini `analyze_reference_video_structure` → extract scene count and narrative roles/durations; this structure is passed into `generate_product_video_scenes` so the new video follows the same flow. If no URL, this step is skipped.
- Product/scene prompts (Gemini); clean product image (optional); scene images (Gemini Image); scene videos (Kling/Veo/Runway).
- VO: per-scene or single script; ElevenLabs TTS.
- Music: Suno or manual link.
- Concat (no dissolve by default), add VO + music, then ZapCap if requested.
- **Logo** and **Slogan/Slogen** are used in the ending/CTA scene (slogan passed where CTA/ending is built).

---

## 6. Services (classes and roles)

| Service | Class | Responsibility |
|---------|--------|-----------------|
| **Google Sheets** | `GoogleSheetsService` | Read/write sheet; column index by name (exact + optional case-insensitive for some columns). Slogan column: try "Slogan", then "Slogen", then case-insensitive. |
| **Gemini (text)** | `GeminiService` | Parse prompt → TEXT 1–4; generate influencer scene prompts; generate influencer VO script; describe character; music description; **analyze_reference_video_structure** (product video: extract scene count and narrative roles/durations from reference video URL). Uses Vertex AI / configured Gemini model. |
| **Gemini Image** | `GeminiImageService` | Generate scene images (and product/CTA images). Uses Gemini image models (e.g. product vs scene). Reference images (character, location, logo) passed as URLs or base64. |
| **Veo 3** | `Veo3Service` | Animate image → video (Google Veo 3). Used when animation model is "google" or fallback. |
| **Kling** | (Kie.ai / external) | Kling V2.5 used for UGC scene animation (often preferred over Veo for policy reasons). |
| **Runway** | `KieAIService` | Runway for video-from-image (and asset Ken Burns). Used when animation model is Runway. |
| **Rendi** | `RendiService` | FFmpeg-based: trim, concatenate (simple, with transitions, or **video_only** with optional **dissolve**), add VO + music (`add_vo_and_music_to_video`). All concat methods normalize to 1080x1920, 30 fps, trim to duration. |
| **ElevenLabs** | `ElevenLabsService` | TTS: `text_to_speech(text, voice_id, language, expressive=False, sentence_pause_seconds=0.0)`. For UGC: `expressive=True`, `sentence_pause_seconds=0.5`. Pauses implemented via `_insert_sentence_pauses` (ElevenLabs `<break time="Xs" />` between sentences). |
| **ZapCap** | `ZapCapService` | Add subtitles to video; language from **Language** column. |
| **Suno** | `SunoMusicService` | Generate background music from description (from Gemini). |
| **GCS** | `GCSStorageService` | Upload images/audio/video; return public or signed URLs. Bucket: `config.GCS_UPLOAD_BUCKET_NAME` (e.g. `automatiq`), folder `GCS_UPLOAD_FOLDER`. |
| **OpenAI** | `OpenAIService` | Used by ElevenLabs (e.g. speech detection) and optionally for some legacy/backup paths. |

---

## 7. Rendi concatenation and dissolve

- **`concatenate_videos(video_data, use_transitions=False, video_only=False, dissolve_seconds=0.0)`**
  - If `video_only` and `dissolve_seconds > 0` and more than one clip: **`_concatenate_video_only_with_dissolve(video_urls, durations, dissolve_seconds)`**.
  - Else if `video_only`: **`_concatenate_video_only(video_urls, durations)`** (trim + concat, no xfade).
  - Else if `use_transitions`: **`_concatenate_with_transitions`** (xfade).
  - Else: **`_concatenate_simple`** (concat demuxer).

- **UGC** explicitly calls:  
  `concatenate_videos(video_data, video_only=True, dissolve_seconds=0.45)`  
  so every transition between shots has a **0.45 s gentle dissolve** (FFmpeg xfade).

- **`_concatenate_video_only_with_dissolve`**: trim each input to its target duration, scale/crop to 1080x1920, then chain `xfade=transition=fade:duration=X:offset=...` between consecutive clips. Output is video-only (no audio).

---

## 8. VO: expressiveness and sentence pauses

- **UGC VO:**  
  - Script from `generate_influencer_vo_script` (Gemini): first-person, passionate, smooth flow, no abrupt jumps.  
  - TTS: `text_to_speech(vo_script, voice_id, language, expressive=True, sentence_pause_seconds=0.5)`.

- **`expressive=True`** (UGC): ElevenLabs `voice_settings`: `stability=0.4`, `style=0.55` (more expressive).

- **`sentence_pause_seconds=0.5`**: Before sending text to ElevenLabs, **`_insert_sentence_pauses(text, 0.5)`** inserts ` <break time="0.5s" /> ` after each sentence-ending punctuation (`.!?`) so there is a **0.5 s pause between sentences** without changing the script stored in the sheet.

---

## 9. Slogan and logo (CTA)

- **Slogan column:** Resolved in order: column named **"Slogan"**, then **"Slogen"**, then any header that matches (case-insensitive) `"slogan"` or `"slogen"`. Value is read and passed as `slogan_text` into both product and UGC pipelines.
- **Usage:** In the **last (CTA) scene**, the image is generated with **logo URL** and **slogan text** combined in one frame (Gemini Image). Logo is prominent; slogan appears as text (e.g. below or near logo). If no slogan is provided, a default (e.g. "Try it now!") is used.

---

## 10. Config (`Config` dataclass)

- **Sheets:** `GOOGLE_SHEET_ID`, `GOOGLE_SHEET_TAB`, `SERVICE_ACCOUNT_FILE`.
- **API keys:** `OPENAI_API_KEY`, `KIE_API_KEY`, `RENDI_API_KEY`, `ELEVENLABS_API_KEY`, `ZAPCAP_API_KEY`, `VERTEX_AI_API_KEY`, etc.
- **Gemini:** `VERTEX_AI_MODEL`, `GEMINI_IMAGE_PROJECT_ID`, `GEMINI_PRODUCT_IMAGE_MODEL`, `GEMINI_SCENE_IMAGE_MODEL`, rate limits and retries.
- **Veo 3:** `VEO3_MODEL`, `VEO3_PROJECT_ID`, endpoints, resolution, poll interval.
- **GCS:** `GCS_UPLOAD_BUCKET_NAME`, `GCS_UPLOAD_FOLDER`, `GCS_UPLOAD_CREDENTIALS_FILE`.
- **Voices:** `DEFAULT_VOICE_ID` (male), `DEFAULT_FEMALE_VOICE_ID` (female for UGC).
- **UGC defaults:** `DEFAULT_INFLUENCER_SCENES`, `INFLUENCER_SCENE_DURATION`.
- **Video types:** `VIDEO_TYPES = ("product video", "UGC-style video", "service video", "explainer video")`.
- **Column names:** All `*_COLUMN` constants (see §2).

---

## 11. File and run layout

- **Single main file:** `Comp_Videos/video_scene_processor.py` (very long; contains Config, all services, and `VideoSceneProcessor`).
- **Run:** From `Comp_Videos` directory (so `service_account.json` is found):  
  `python video_scene_processor.py`
- **Environment:** `.env` / `env_example.txt` for API keys and optional overrides. `service_account.json` and optionally `gcs_service_account.json` in `Comp_Videos`.

---

## 12. Quick reference: what to change where

| Need | Where |
|------|--------|
| Slogan / Slogen column | Column resolution in `VideoSceneProcessor` (slogan_col lookup); usage in CTA scene in UGC and product flows. |
| VO pauses between sentences | `ElevenLabsService._insert_sentence_pauses` and `text_to_speech(..., sentence_pause_seconds=0.5)` in UGC. |
| VO more expressive | `text_to_speech(..., expressive=True)` and `voice_settings` in `ElevenLabsService.text_to_speech`. |
| Gentle dissolve between shots | `RendiService.concatenate_videos(..., video_only=True, dissolve_seconds=0.45)` in UGC; `_concatenate_video_only_with_dissolve`. |
| Scene count / duration | UGC: scene count and duration budget from `target_duration`, assets count; product: similar from config and sheet. |
| Logo + slogan in CTA | CTA scene image generation (Gemini Image) with `logo_url` and `slogan_text`; last scene is always CTA. |
| Gender (m/f) | Voice ID selection and influencer generation use **Gender** column; `_generate_influencer_image` and VO call. |
| Reference images (Image 1–5) | Analyzed in UGC; each scene has `reference_image_index`; passed to scene image generation. |
| Assets (Asset 1–3) | Inserted as-is, max 3 s each, in the middle of the sequence; order: body scenes → assets → CTA. |
| Video reference (product video) | Column read in `process_all_videos`; URL passed to `process_product_video`; download → `GeminiService.analyze_reference_video_structure` → structure passed to `generate_product_video_scenes`. |

This file is the single place to look for the full, up-to-date structure of the system.

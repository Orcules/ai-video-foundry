# Model Reference — what each tool is best at

This is your toolbelt. When you build a storyboard, stamp the right model on each scene (`preview_image_model`) and each clip (`video_model_override`). Match by intent, not by surface keyword. When two could apply, the higher entry in each picking-flow wins.

If a brief is ambiguous, default to: `nano-banana-pro` for the image preview and `veo-3.1-fast` for the clip animation. These are the "good for everything" picks.

---

## IMAGE MODELS (for storyboard previews)

Pick ONE per scene. The chosen model renders the first frame, which the user reviews and which then becomes the I2V start image for video generation. Quality of this frame anchors the entire scene.

| Model | What it wins at | Use when | Avoid when | Refs | Cost |
|---|---|---|---|---|---|
| **`nano-banana-pro`** | Photoreal hero shots, complex lighting, on-image text, multi-ref (up to 8 images) | Hero/CTA frames, brand product shots, anything the user will scrutinize, scenes with text legibility | Quick drafts (too slow at 10-20s), high-volume iteration | Up to 8 | $0.06 |
| **`nano-banana-2`** | Fast iteration (4-6s), **Image Search Grounding** (correct landmarks, public figures, brand logos via Google Search at gen-time), character/cartoon | Drafts, B-roll, scenes where the brief mentions a real place / brand / public figure, social-feed lifestyle shots | High-stakes hero frames where Pro's micro-detail matters | 1 (optional) | $0.04 |
| **`gemini-3-pro-image-preview`** | Same engine as Nano Banana Pro, called via Vertex directly | When you need direct Vertex pipeline (no Kie), or for tighter structure on response payloads | The 2× cost vs Nano Banana 2 isn't justified for simple scenes | Up to 3 (base64 inline) | direct |
| **`gemini-3.1-flash-image-preview`** | Vertex-native fast tier, low cost, large batch | Lots of similar previews (a 9-scene grid all in same style) | Hero frames | Up to 3 | direct |
| **`gemini-2.5-flash-image`** | Legacy fast fallback | Backward compatibility only — don't pick this for new work | — | Up to 3 | direct |

### Image model picking flow

1. **Brief mentions a real place / brand / public figure / logo that must be accurate** → `nano-banana-2` (only model with Image Search Grounding).
2. **Hero frame / CTA card / on-image text / complex lighting** → `nano-banana-pro`.
3. **Storyboard has 5+ scenes and you want consistent style across all** → `nano-banana-pro` for hero + CTA, `nano-banana-2` for B-roll / connectors.
4. **Cinematic still / film-look brief** → `nano-banana-pro` (better at lighting + grain).
5. **Drafting / fast iteration / "show me ideas"** → `nano-banana-2` everywhere.
6. **Brief mentions "screenshot", "UI", "dashboard"** → `nano-banana-pro` (text legibility wins).
7. **Default when unsure** → `nano-banana-pro`.

---

## VIDEO MODELS (for animating each scene/clip)

Pick ONE per clip via `video_model_override`. If you don't set it, the animation router picks based on heuristics — that works fine but you can do better by setting it explicitly when the clip has special needs.

### Quick chart

| Model | Modality | Best at | Duration | Cost/sec |
|---|---|---|---|---|
| `veo-3.1-fast` | I2V + native audio | **Default for everything**. Spatial audio (48kHz stereo), strongest lip-sync. | 4 / 6 / 8s | $0.10 |
| `veo-3.1` | I2V + native audio | Hero quality — same Veo 3.1 advances at higher fidelity. | 4 / 6 / 8s | $0.20 |
| `veo-3.1-ref-fast` | **Ref-to-Video (Ingredients)** | **Character + venue + style consistency across shots**. Up to 3 reference images locked. | 8s only | $0.10 |
| `veo-3.1-ref` | Ref-to-Video | Same as Ref Fast, hero fidelity. | 8s only | $0.20 |
| `veo-3.1-ref-fal` | Ref-to-Video (FAL routing) | Fallback when Vertex is throttled. | 8s only | $0.18 |
| `veo-3.0-fast` | I2V | Cheaper baseline. Older but solid. | 4 / 6 / 8s | $0.08 |
| `veo-3.0` | I2V | Older hero tier. | 4 / 6 / 8s | $0.16 |
| `seedance-2` | **Multi-shot I2V/T2V** | "Cut to / then" beats in ONE call. Up to 9 image refs + 3 video refs + 3 audio refs. | 4–15s | $0.09 |
| `kling-2.6` | I2V | **Heavy camera motion** — dolly, orbit, whip pan, crash zoom. Strong motion physics. | 5 / 10s | $0.055 |
| `kling-2.5` | I2V | Older Kling. Cheaper. | 5 / 10s | $0.042 |
| `kling/ai-avatar-pro` | **Lipsync** (still + audio → talking head) | Talking-head clips with pre-recorded VO. The only dedicated lipsync model. | from audio | $0.08 / clip |
| `runway-gen4.5` | I2V | **Top photorealism** (#1 Elo 1247). Cinematic photography polish, brand-quality. | 2–10s | $0.12 |
| `runway-gen4-turbo` | I2V | Cheaper Runway. Budget cinematic. | 2–10s | $0.05 |
| `kenburns` | FFmpeg pan/zoom | Free. Sub-3s fillers, CTA cards, end fillers. | any | ~$0 |

### Video model picking flow

1. **Talking head with dialog / lipsync** → `veo-3.1-ref-fast` if the character must look identical to a reference photo (ingredients lock); otherwise `veo-3.1-fast`.
2. **Multi-shot beat in one cut** (motion_prompt contains "cut to", "then", "intercut", "montage") → `seedance-2`. Pack character_sheet + venue_sheet + style_sheet via ingredients.
3. **Character consistency across 3+ scenes** → every character-bearing clip should be `veo-3.1-ref-fast` with `ingredients.use_character_sheet = true`. The Ingredients lock is what holds the face geometry.
4. **Heavy camera motion** (dolly in, orbit, whip pan, crash zoom, fast tracking) → `kling-2.6`.
5. **Cinematic photography polish / brand-quality / Elo-top** → `runway-gen4.5`.
6. **Budget cinematic** → `runway-gen4-turbo`.
7. **Motion graphics / kinetic typography / animated text** → `kling-2.6` (Kling is best at preserving text legibility through motion).
8. **Short filler (< 3s) with subtle motion** → `kenburns` (free).
9. **CTA card / logo reveal / static overlay** → `kenburns` or `veo-3.1-fast` with `static` primary_move.
10. **Default when unsure** → `veo-3.1-fast`.

### Critical rules

- **Veo 3.1 Ref Fast / Ref are 8 seconds only.** If the scene needs 4 or 6 seconds, either pad with Ken Burns at the end or pick `veo-3.1-fast` (which supports 4/6/8) and rely on character_sheet ingredients via Composer rather than the Ref endpoint.
- **Seedance accepts 4–15s.** For a beat that needs ~10s of "wide → close-up → reaction" with one locked character, Seedance is far cheaper and more consistent than chaining 3 separate Veo clips.
- **Kling AvatarPro requires both an image AND an audio file.** Only use when the user has explicitly recorded VO or when we pre-synthesized it before video gen.
- **Runway 4.5 is the photoreal champion but costs more.** Reserve for hero shots where the brief mentions "cinematic", "commercial-quality", "brand polish", "Apple-grade".

---

## WORKED EXAMPLES

### Example 1 — "30s promo for my Tel Aviv ramen bar, here are 4 venue photos"

| Scene | Role | Preview image | Clip type | Video model | Why |
|---|---|---|---|---|---|
| 1 | hook | `nano-banana-pro` | generate | `veo-3.1-fast` | Hero opening, food close-up needs Pro's lighting. Veo 3.1 Fast for animation with native audio (steam, sizzle). |
| 2 | problem | `nano-banana-2` | asset_image_animate | `kling-2.6` | "Most ramen is mediocre" — quick venue cutaway from user upload. Kling for the dolly-out reveal. |
| 3 | solution | `nano-banana-pro` | seedance_multishot | `seedance-2` | "Wide shot of chef → close-up of broth → orbit around bowl" in one beat with character + venue ingredients locked. |
| 4 | proof | `nano-banana-pro` | generate | `veo-3.1-ref-fast` | Customer reaction shot with character_sheet ingredient (chef's face must match scene 3). |
| 5 | cta | `nano-banana-pro` | composite (logo+slogan) | `kenburns` | Final logo card with slogan — free Ken Burns push-in is the polite finish. |

### Example 2 — "Saas dashboard demo 45s with founder voiceover"

| Scene | Role | Preview image | Clip type | Video model | Why |
|---|---|---|---|---|---|
| 1 | hook | `nano-banana-pro` | generate | `veo-3.1-fast` | Founder portrait (sharp text on slide behind). |
| 2 | problem | `nano-banana-pro` | motion_graphic | `kling-2.6` | Animated stat callout ("87% of teams waste 4hrs/week"). Kling for text legibility. |
| 3 | solution | `nano-banana-pro` | asset_image_animate | `veo-3.1-fast` | User-uploaded dashboard screenshot, gentle pan to highlight feature. |
| 4 | proof | `nano-banana-pro` | motion_graphic | `kling-2.6` | Testimonial quote with kinetic typography. |
| 5 | cta | `nano-banana-pro` | composite (logo) | `kenburns` | Logo + URL card. |

### Example 3 — "Influencer founder story 60s, locked character across all shots"

For this one, set `character_sheet.reference_image_urls` from the founder's uploaded photo. Every scene gets `ingredients.use_character_sheet = true`. Every video model that supports ingredients uses `veo-3.1-ref-fast` (or `seedance-2` for multi-shot beats). The character's face stays identical across every scene.

| Scene | Role | Preview image | Clip type | Video model | Ingredients |
|---|---|---|---|---|---|
| 1 | hook | `nano-banana-pro` | generate | `veo-3.1-ref-fast` | character ✓ |
| 2 | story_setup | `nano-banana-2` | seedance_multishot | `seedance-2` | character ✓ + venue ✓ |
| 3 | challenge | `nano-banana-pro` | generate | `veo-3.1-ref-fast` | character ✓ + style ✓ |
| 4 | breakthrough | `nano-banana-pro` | seedance_multishot | `seedance-2` | character ✓ + venue ✓ + style ✓ |
| 5 | cta | `nano-banana-pro` | composite (logo) | `veo-3.1-fast` | (no ingredients — logo card) |

---

## ANTI-PATTERNS — common mistakes

- **Don't put `nano-banana-pro` on every scene by default.** B-roll connectors and asset-clip animations don't need hero-grade image gen. Mix in `nano-banana-2` for cost.
- **Don't pick `runway-gen4.5` unless the brief explicitly demands cinema-grade polish.** It's expensive and slower than Veo. The default is `veo-3.1-fast`.
- **Don't pick `veo-3.1-ref-fast` for clips that don't need character consistency.** It's 8s-only and locked-in — use `veo-3.1-fast` for general I2V.
- **Don't pick Seedance for single shots.** Its strength is multi-shot consistency. A single 5-second shot uses Veo or Kling more cheaply.
- **Don't pick `kling/ai-avatar-pro` for non-lipsync clips.** It's specialized for "still photo + audio → talking head". Wrong tool for any other use.
- **Don't override `tool_hint` AND `video_model_override` on the same clip.** Pick one. `video_model_override` is more specific and wins.

---

## How to stamp this in the storyboard JSON

Per scene, add the new optional field:

```json
{
  "scene_number": 1,
  "narrative_role": "hook",
  "duration": 4.0,
  "preview_image_model": "nano-banana-pro",
  "clips": [...]
}
```

Per clip, add the new optional field next to `tool_hint`:

```json
{
  "type": "generate",
  "duration": 4.0,
  "video_model_override": "veo-3.1-ref-fast",
  "video_provider_override": "direct",
  "ingredients": { "use_character_sheet": true }
}
```

Both fields are optional. When you omit them, the system falls back to the tier default (for `preview_image_model`) or `animation_router.pick_tool()` (for `video_model_override`).

When you DO stamp them, the storyboard becomes self-describing: anyone reading the JSON can see why each scene/clip uses the model it does. That's what we want.

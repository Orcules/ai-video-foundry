"""Storyboard schema, validator, and converter for the `custom` pipeline.

A storyboard is a chat-built JSON describing every scene, every clip, the VO,
and the music. The custom pipeline converts it into the intermediates shape
that `process_ugc_video` already understands (`existing_intermediates=...`),
so no changes to `ugc.py` are required.

Storyboard JSON shape (canonical):

```jsonc
{
  "meta": {
    "title": "string",
    "video_type": "custom",
    "preset_hint": "location-faithful" | "product" | "ugc" | "personal_brand" | null,
    "target_duration_seconds": 30,
    "language": "he",
    "country": "israel",
    "style": "Cinematic photography",
    "fidelity_to_assets": 0.75,        // 0 = freely creative, 1 = stay faithful
    "aspect_ratio": "9:16"
  },
  "voiceover": {
    "script": "text with ||| segment markers",
    "voice_id": "elevenlabs_voice_id",
    "language": "he"
  },
  "music": {
    "description": "Upbeat lo-fi with Japanese motif",
    "mood": "energetic",
    "url": null                          // optional: skip Suno if provided
  },
  "assets": {
    "character_urls": [],
    "product_image_urls": [],
    "reference_image_urls": [],
    "asset_video_urls": [],
    "logo_url": null,
    "slogan_text": null
  },
  "scenes": [
    {
      "scene_number": 1,
      "narrative_role": "hook",
      "vo_text": "...",
      "duration": 5.0,
      "clips": [
        {
          "type": "asset_video|asset_image_animate|generate|composite|ken_burns",
          "duration": 3.0,
          "tool_hint": "auto|veo|kling|runway|kenburns|trim",
          "shows_influencer": false,
          "source": {
            "asset_video_index": 0,
            "reference_image_index": 1,
            "start_seconds": 2.0,
            "end_seconds": 5.0
          },
          "first_prompt": "...",         // for type=generate / composite
          "motion_prompt": "...",        // for type=asset_image_animate / generate
          "overlay": { "logo": true, "slogan": true }  // for type=composite
        }
      ]
    }
  ]
}
```
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


CLIP_TYPES = {
    "asset_video",
    "asset_image_animate",
    "generate",
    "composite",
    "ken_burns",
    # NEW types for D-series (Director engine):
    "seedance_multishot",   # multi-shot consistency via Seedance 2.0
    "motion_graphic",       # kinetic typography / animated text+UI
    # NEW type for programmatic framework rendering (Manim / Remotion / HyperFrames / Lottie).
    # Until each framework's executor is wired, these clips fall back to a styled
    # Ken Burns placeholder so the storyboard still produces a valid video.
    "framework_render",
}

FRAMEWORK_TYPES = {"manim", "remotion", "hyperframes", "lottie"}

TOOL_HINTS = {"auto", "veo", "seedance", "kling", "runway", "kenburns", "trim"}

# E1/E2 — explicit per-scene image model + per-clip video model overrides.
# The Director reads `director_models_reference.md` and stamps one of these on
# each scene/clip. Both fields are optional — defaults come from the tier config
# (image_model) and animation_router.pick_tool() (video_model).
IMAGE_MODEL_NAMES = {
    "nano-banana-2",
    "nano-banana-pro",
    "gemini-3-pro-image-preview",
    "gemini-3.1-flash-image-preview",
    "gemini-2.5-flash-image",
}
VIDEO_MODEL_NAMES = {
    "veo-3.0", "veo-3.0-fast", "veo-3.1", "veo-3.1-fast",
    "veo-3.1-ref", "veo-3.1-ref-fast", "veo-3.1-ref-fal",
    "kling-2.5", "kling-2.6",
    "seedance-2",
    "runway-gen4-turbo", "runway-gen4.5",
    "kenburns",
}
VIDEO_PROVIDERS = {"direct", "kie", "fal", "runway_direct", "ffmpeg", "rendi"}

# Camera intent enums (Director-emitted, validated softly — unknown values are warnings, not errors).
SHOT_TYPES = {
    "extreme_close_up", "close_up", "medium", "medium_wide", "wide",
    "establishing", "over_shoulder", "insert", "pov",
}
PRIMARY_MOVES = {
    "static", "slow_dolly_in", "slow_dolly_out", "fast_dolly_in",
    "orbit", "tracking", "whip_pan", "crash_zoom", "ken_burns",
    "pan_left", "pan_right", "tilt_up", "tilt_down", "handheld",
}
LENS_FEELS = {"anamorphic", "35mm", "85mm_portrait", "telephoto", "wide_lens", "natural"}
CAMERA_SPEEDS = {"slow", "moderate", "fast"}

# Director preset hints (internal — never shown to user)
PRESET_HINTS = {
    "product", "influencer", "personal_brand", "ugc_real_grid",
    "motion_graphics", "narrative",
}
VIRAL_STRUCTURES = {
    "hook_problem_solution_cta", "hook_proof_cta", "story", "tutorial",
    "testimonial", "narrative_arc", "ugc_punchline",
}


def validate_storyboard(sb: Dict[str, Any]) -> List[str]:
    """Return a list of human-readable errors. Empty list = valid."""
    errors: List[str] = []
    if not isinstance(sb, dict):
        return ["storyboard must be an object"]

    meta = sb.get("meta") or {}
    if not isinstance(meta, dict):
        errors.append("meta must be an object")
    else:
        if not meta.get("title"):
            errors.append("meta.title is required")
        td = meta.get("target_duration_seconds")
        if not isinstance(td, (int, float)) or td <= 0:
            errors.append("meta.target_duration_seconds must be a positive number")
        fid = meta.get("fidelity_to_assets", 0.5)
        if not isinstance(fid, (int, float)) or not 0.0 <= float(fid) <= 1.0:
            errors.append("meta.fidelity_to_assets must be between 0 and 1")

    vo = sb.get("voiceover") or {}
    if not isinstance(vo, dict):
        errors.append("voiceover must be an object")
    elif not vo.get("script"):
        errors.append("voiceover.script is required")

    assets = sb.get("assets") or {}
    if not isinstance(assets, dict):
        errors.append("assets must be an object")

    # NEW (D2): optional Director sheets. Validate shape if present.
    for sheet_name in ("character_sheet", "venue_sheet", "style_sheet"):
        sheet = sb.get(sheet_name)
        if sheet is None:
            continue
        if not isinstance(sheet, dict):
            errors.append(f"{sheet_name} must be an object if provided")
            continue
        refs = sheet.get("reference_image_urls", [])
        if refs is not None and not isinstance(refs, list):
            errors.append(f"{sheet_name}.reference_image_urls must be a list of URLs")
        elif isinstance(refs, list):
            # Veo Ingredients accepts up to 3 references; Seedance up to 9.
            # We cap per-sheet at 5 — Composer trims for the target tool anyway.
            if len(refs) > 5:
                errors.append(
                    f"{sheet_name}.reference_image_urls has {len(refs)} items; max 5 (Composer will trim per-tool)"
                )

    # NEW (D2): optional grid output (ugc_real-style 3x3 layout)
    grid = sb.get("grid")
    if grid is not None:
        if not isinstance(grid, dict):
            errors.append("grid must be an object if provided")
        else:
            rows = grid.get("rows", 3)
            cols = grid.get("cols", 3)
            if not isinstance(rows, int) or rows < 1:
                errors.append("grid.rows must be a positive integer")
            if not isinstance(cols, int) or cols < 1:
                errors.append("grid.cols must be a positive integer")

    scenes = sb.get("scenes") or []
    if not isinstance(scenes, list) or not scenes:
        errors.append("scenes must be a non-empty list")
        return errors

    for s_idx, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            errors.append(f"scenes[{s_idx}] must be an object")
            continue
        if not isinstance(scene.get("duration"), (int, float)) or scene["duration"] <= 0:
            errors.append(f"scenes[{s_idx}].duration must be a positive number")
        clips = scene.get("clips") or []
        if not isinstance(clips, list) or not clips:
            errors.append(f"scenes[{s_idx}].clips must be a non-empty list")
            continue
        clip_total = 0.0
        for c_idx, clip in enumerate(clips):
            if not isinstance(clip, dict):
                errors.append(f"scenes[{s_idx}].clips[{c_idx}] must be an object")
                continue
            ctype = clip.get("type")
            if ctype not in CLIP_TYPES:
                errors.append(
                    f"scenes[{s_idx}].clips[{c_idx}].type must be one of {sorted(CLIP_TYPES)}"
                )
            cdur = clip.get("duration")
            if not isinstance(cdur, (int, float)) or cdur <= 0:
                errors.append(f"scenes[{s_idx}].clips[{c_idx}].duration must be positive")
            else:
                clip_total += float(cdur)
            hint = clip.get("tool_hint", "auto")
            if hint not in TOOL_HINTS:
                errors.append(
                    f"scenes[{s_idx}].clips[{c_idx}].tool_hint must be one of {sorted(TOOL_HINTS)}"
                )
            src = clip.get("source") or {}
            if ctype == "asset_video":
                if not isinstance(src.get("asset_video_index"), int):
                    errors.append(
                        f"scenes[{s_idx}].clips[{c_idx}] type=asset_video requires source.asset_video_index"
                    )
            elif ctype == "asset_image_animate":
                if not isinstance(src.get("reference_image_index"), int):
                    errors.append(
                        f"scenes[{s_idx}].clips[{c_idx}] type=asset_image_animate requires source.reference_image_index"
                    )
            elif ctype == "generate":
                if not clip.get("first_prompt"):
                    errors.append(
                        f"scenes[{s_idx}].clips[{c_idx}] type=generate requires first_prompt"
                    )
            elif ctype == "motion_graphic":
                # Motion graphics need a visual prompt (the text/graphic content)
                if not clip.get("first_prompt"):
                    errors.append(
                        f"scenes[{s_idx}].clips[{c_idx}] type=motion_graphic requires first_prompt"
                    )
            elif ctype == "framework_render":
                fw = clip.get("framework")
                if fw not in FRAMEWORK_TYPES:
                    errors.append(
                        f"scenes[{s_idx}].clips[{c_idx}] type=framework_render requires framework in {sorted(FRAMEWORK_TYPES)} (got {fw!r})"
                    )
                if not clip.get("first_prompt"):
                    errors.append(
                        f"scenes[{s_idx}].clips[{c_idx}] type=framework_render requires first_prompt describing the rendered content"
                    )
            # seedance_multishot accepts either ingredients (Composer packages refs)
            # or just a motion_prompt — no hard requirement on first_prompt here.

            # NEW (D2): validate optional ingredients block per clip
            ing = clip.get("ingredients")
            if ing is not None and not isinstance(ing, dict):
                errors.append(
                    f"scenes[{s_idx}].clips[{c_idx}].ingredients must be an object if provided"
                )

            # E1: per-clip video_model_override + video_provider_override
            vmo = clip.get("video_model_override")
            if vmo is not None and vmo not in VIDEO_MODEL_NAMES:
                errors.append(
                    f"scenes[{s_idx}].clips[{c_idx}].video_model_override must be one of {sorted(VIDEO_MODEL_NAMES)} (got {vmo!r})"
                )
            vpo = clip.get("video_provider_override")
            if vpo is not None and vpo not in VIDEO_PROVIDERS:
                errors.append(
                    f"scenes[{s_idx}].clips[{c_idx}].video_provider_override must be one of {sorted(VIDEO_PROVIDERS)} (got {vpo!r})"
                )

        # NEW (D2): validate optional per-scene camera intent (soft — unknown values warn but don't fail)
        cam = scene.get("camera")
        if cam is not None and not isinstance(cam, dict):
            errors.append(f"scenes[{s_idx}].camera must be an object if provided")

        # E1: per-scene preview_image_model
        pim = scene.get("preview_image_model")
        if pim is not None and pim not in IMAGE_MODEL_NAMES:
            errors.append(
                f"scenes[{s_idx}].preview_image_model must be one of {sorted(IMAGE_MODEL_NAMES)} (got {pim!r})"
            )
        # E3: preview_image_url is populated by the storyboard_previews step. We don't validate its shape strictly.

        # Soft check: clip durations should roughly match scene duration
        s_dur = scene.get("duration", 0)
        if s_dur and abs(clip_total - s_dur) > 0.5:
            errors.append(
                f"scenes[{s_idx}] clip durations sum to {clip_total:.1f}s "
                f"but scene.duration is {s_dur:.1f}s (off by >0.5s)"
            )

    return errors


def storyboard_to_ugc_scenes(sb: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Convert storyboard scenes to the ugc.py `scene_prompts` shape.

    The output matches what `process_ugc_video` reads from
    `intermediates["scene_prompts"]` (smart-mode shape with `beat_clips`).
    """
    out: List[Dict[str, Any]] = []
    for s_idx, scene in enumerate(sb.get("scenes") or []):
        scene_num = scene.get("scene_number", s_idx + 1)
        clips_in = scene.get("clips") or []
        beat_clips: List[Dict[str, Any]] = []
        primary_video_asset_idx: Optional[int] = None
        primary_ref_image_idx: Optional[int] = None
        primary_first_prompt = ""
        primary_motion_prompt = ""
        shows_influencer = False

        for clip in clips_in:
            ctype = clip.get("type")
            cdur = float(clip.get("duration", 0.0))
            src = clip.get("source") or {}
            bc: Dict[str, Any] = {"duration": cdur}

            if ctype == "asset_video":
                bc["type"] = "video"
                bc["video_asset_index"] = src.get("asset_video_index")
                if isinstance(src.get("start_seconds"), (int, float)):
                    bc["_start_seconds"] = float(src["start_seconds"])
                if isinstance(src.get("end_seconds"), (int, float)):
                    bc["_end_seconds"] = float(src["end_seconds"])
                if src.get("best_moment_index") is not None:
                    bc["best_moment_index"] = src["best_moment_index"]
                if primary_video_asset_idx is None:
                    primary_video_asset_idx = bc["video_asset_index"]

            elif ctype == "asset_image_animate":
                bc["type"] = "image"
                bc["reference_image_index"] = src.get("reference_image_index")
                bc["motion_prompt"] = clip.get("motion_prompt") or "Subtle slow zoom in, very slight movement"
                bc["variant"] = "regular"
                if primary_ref_image_idx is None:
                    primary_ref_image_idx = bc["reference_image_index"]

            elif ctype == "generate":
                bc["type"] = "generate"
                bc["first_prompt"] = clip.get("first_prompt") or ""
                bc["second_prompt"] = clip.get("motion_prompt") or ""
                bc["motion_prompt"] = clip.get("motion_prompt") or ""
                bc["description"] = clip.get("first_prompt") or ""
                if not primary_first_prompt:
                    primary_first_prompt = bc["first_prompt"]
                    primary_motion_prompt = bc["motion_prompt"]

            elif ctype == "composite":
                # Composite: NB2 influencer-in-venue OR logo overlay.
                # If influencer urls + reference image -> influencer_in_venue variant.
                # Otherwise treat as generate with overlay metadata for end-card path.
                if clip.get("shows_influencer") and isinstance(src.get("reference_image_index"), int):
                    bc["type"] = "image"
                    bc["reference_image_index"] = src["reference_image_index"]
                    bc["motion_prompt"] = clip.get("motion_prompt") or "Influencer interacts naturally with the venue"
                    bc["variant"] = "influencer_in_venue"
                    if primary_ref_image_idx is None:
                        primary_ref_image_idx = bc["reference_image_index"]
                else:
                    bc["type"] = "generate"
                    bc["first_prompt"] = clip.get("first_prompt") or ""
                    bc["second_prompt"] = clip.get("motion_prompt") or ""
                    bc["motion_prompt"] = clip.get("motion_prompt") or ""
                    bc["overlay"] = clip.get("overlay") or {}
                    if not primary_first_prompt:
                        primary_first_prompt = bc["first_prompt"]
                        primary_motion_prompt = bc["motion_prompt"]

            elif ctype == "ken_burns":
                # No animation API call — pure FFmpeg zoom-pan on a still image.
                bc["type"] = "image"
                bc["reference_image_index"] = src.get("reference_image_index")
                bc["motion_prompt"] = clip.get("motion_prompt") or "Slow Ken Burns push-in"
                bc["variant"] = "regular"
                bc["_ken_burns_only"] = True   # hint for the executor / router
                if primary_ref_image_idx is None and isinstance(src.get("reference_image_index"), int):
                    primary_ref_image_idx = src["reference_image_index"]

            elif ctype == "seedance_multishot":
                # Multi-shot consistency via Seedance 2.0. ugc.py's beat-clips path
                # doesn't know about Seedance; the Composer will side-channel this
                # clip into intermediates.scene_videos BEFORE ugc.py runs (see
                # _composer.py:execute_seedance_clips). The bc.type="generate"
                # representation is the fallback if Composer skips this clip.
                bc["type"] = "generate"
                bc["first_prompt"] = clip.get("first_prompt") or ""
                bc["second_prompt"] = clip.get("motion_prompt") or ""
                bc["motion_prompt"] = clip.get("motion_prompt") or ""
                bc["description"] = clip.get("first_prompt") or clip.get("motion_prompt", "")
                bc["_tool_hint"] = "seedance"
                bc["_storyboard_clip_type"] = "seedance_multishot"
                if not primary_first_prompt:
                    primary_first_prompt = bc["first_prompt"]
                    primary_motion_prompt = bc["motion_prompt"]

            elif ctype == "motion_graphic":
                # Kinetic typography / animated text+UI. T2I (Nano Banana) +
                # I2V (Kling, best with text). Same side-channel pattern as Seedance
                # if Composer pre-executes; otherwise falls through to ugc.py generate.
                bc["type"] = "generate"
                bc["first_prompt"] = clip.get("first_prompt") or ""
                bc["second_prompt"] = clip.get("motion_prompt") or "Smooth kinetic reveal"
                bc["motion_prompt"] = clip.get("motion_prompt") or "Smooth kinetic reveal"
                bc["description"] = clip.get("first_prompt") or ""
                bc["_tool_hint"] = clip.get("tool_hint") if clip.get("tool_hint") not in (None, "auto") else "kling"
                bc["_storyboard_clip_type"] = "motion_graphic"
                if not primary_first_prompt:
                    primary_first_prompt = bc["first_prompt"]
                    primary_motion_prompt = bc["motion_prompt"]

            # Carry user's tool hint forward (if not already stamped above by new types)
            tool_hint = clip.get("tool_hint", "auto")
            if tool_hint and tool_hint != "auto" and "_tool_hint" not in bc:
                bc["_tool_hint"] = tool_hint

            # E1: per-clip explicit video model override wins over _tool_hint.
            vmo = clip.get("video_model_override")
            if vmo:
                bc["_video_model_override"] = vmo
                vpo = clip.get("video_provider_override")
                if vpo:
                    bc["_video_provider_override"] = vpo

            # Carry ingredients flags so the Composer/executor can package refs
            ing = clip.get("ingredients") or {}
            if ing:
                bc["_ingredients"] = {
                    "use_character_sheet": bool(ing.get("use_character_sheet")),
                    "use_venue_sheet": bool(ing.get("use_venue_sheet")),
                    "use_style_sheet": bool(ing.get("use_style_sheet")),
                }

            # Both shows_character (new) and shows_influencer (legacy) are honored
            if clip.get("shows_character") or clip.get("shows_influencer"):
                bc["shows_influencer"] = True
                bc["shows_character"] = True
                shows_influencer = True

            beat_clips.append(bc)

        ugc_scene: Dict[str, Any] = {
            "scene_number": scene_num,
            "narrative_role": scene.get("narrative_role", ""),
            "shows_influencer": shows_influencer,
            "video_asset_index": primary_video_asset_idx,
            "reference_image_index": primary_ref_image_idx,
            "first_prompt": primary_first_prompt,
            "second_prompt": primary_motion_prompt,
            "motion_prompt": primary_motion_prompt,
            "duration": float(scene.get("duration", 4.0)),
            "vo_text": scene.get("vo_text", ""),
            "beat_clips": beat_clips,
        }
        # NEW (D2): pass per-scene camera intent through as metadata so the
        # executor / Composer can fold cinematic words into the motion_prompt.
        cam = scene.get("camera")
        if isinstance(cam, dict) and cam:
            ugc_scene["_camera"] = dict(cam)

        # E1: per-scene preview_image_model + (post-render) preview_image_url
        pim = scene.get("preview_image_model")
        if pim:
            ugc_scene["_preview_image_model"] = pim
        pi_url = scene.get("preview_image_url")
        if pi_url:
            ugc_scene["_preview_image_url"] = pi_url

        out.append(ugc_scene)

    return out


def package_ingredient_refs(
    sb: Dict[str, Any],
    use_character: bool = False,
    use_venue: bool = False,
    use_style: bool = False,
    max_total: int = 9,
) -> List[str]:
    """Build the prioritized reference-image URL list for a single Veo/Seedance
    call, given the clip's ingredient flags and the storyboard's sheets.

    Order matters: Veo 3.1 Ingredients-to-Video weights the FIRST reference
    image most heavily (character > environment > style). Seedance 2.0 follows
    the same convention. We respect that ordering and cap at ``max_total``
    (Veo: 3-4, Seedance: 9).
    """
    refs: List[str] = []

    def _extend_unique(urls: List[str]) -> None:
        for u in urls or []:
            if u and u not in refs:
                refs.append(u)
                if len(refs) >= max_total:
                    return

    if use_character:
        cs = sb.get("character_sheet") or {}
        _extend_unique(cs.get("reference_image_urls") or [])
        if len(refs) >= max_total:
            return refs
    if use_venue:
        vs = sb.get("venue_sheet") or {}
        _extend_unique(vs.get("reference_image_urls") or [])
        if len(refs) >= max_total:
            return refs
    if use_style:
        ss = sb.get("style_sheet") or {}
        _extend_unique(ss.get("reference_image_urls") or [])

    return refs[:max_total]


def cinematic_motion_words(camera: Optional[Dict[str, Any]]) -> str:
    """Render a per-scene camera dict into a short prefix for motion prompts.

    Director's `camera` field uses enum-like values; we translate them into
    plain English the video model understands without prompt mush.
    """
    if not camera or not isinstance(camera, dict):
        return ""
    parts: List[str] = []
    shot = camera.get("shot_type")
    move = camera.get("primary_move")
    speed = camera.get("speed") or "moderate"
    lens = camera.get("lens_feel")

    if shot:
        parts.append(f"{shot.replace('_', ' ')} framing")
    if move and move != "static":
        parts.append(f"{speed} {move.replace('_', ' ')}")
    elif move == "static":
        parts.append("static camera")
    if lens:
        parts.append(f"{lens.replace('_', ' ')} lens")
    return ", ".join(parts)


def storyboard_to_ugc_kwargs(sb: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Split a storyboard into `(ugc_kwargs, existing_intermediates)`.

    `ugc_kwargs` are the keyword args to pass to `process_ugc_video()`.
    `existing_intermediates` is the dict to pass as `existing_intermediates=` so
    ugc.py skips its own scene planning / VO / music generation steps.
    """
    meta = sb.get("meta") or {}
    vo = sb.get("voiceover") or {}
    music = sb.get("music") or {}
    assets = sb.get("assets") or {}

    # Map fidelity dial to influencer clip ratios. Higher fidelity = more asset clips,
    # fewer generated scenes. Lower fidelity = inverse. The ratios already exist in
    # pipeline_defaults.json (min/max_influencer_clip_ratio). The dial only matters
    # when the storyboard is built by the LLM — here we just plumb it through as a
    # hint; the storyboard's actual clip distribution is the source of truth.
    fidelity = float(meta.get("fidelity_to_assets", 0.5))

    ugc_kwargs: Dict[str, Any] = {
        "prompt": meta.get("title", ""),
        "target_duration": int(round(float(meta.get("target_duration_seconds", 20)))),
        "language": meta.get("language", "en"),
        "subtitle_language": meta.get("language", "en"),
        "country": meta.get("country", ""),
        "visual_style": meta.get("style", "Auto"),
        "voice_id": vo.get("voice_id"),
        "character_urls": list(assets.get("character_urls") or []),
        "reference_image_urls": list(assets.get("reference_image_urls") or []),
        "asset_urls": [
            {"url": u, "type": "video"} for u in (assets.get("asset_video_urls") or [])
        ],
        "product_image_urls": list(assets.get("product_image_urls") or []),
        "logo_url": assets.get("logo_url"),
        "slogan_text": assets.get("slogan_text"),
        # Use influencer subtype — it's the only subtype that activates ugc.py's
        # smart-mode `beat_clips` execution (see _smart_mode at ugc.py:1395/1578).
        # Influencer-specific behaviors like end-card / lipsync are gated on the
        # presence of business_name / character_urls, so they no-op when the
        # storyboard doesn't supply them.
        "video_subtype": "influencer",
        # Standard sync, beat_clips from the storyboard drive precision.
        "sync_method": "standard",
        "asset_mode": "smart",
        "generate_vo": True,
        # Defaults that work for all video types — the storyboard's preset_hint
        # could later steer these.
        "business_category": "general",
    }

    # Intermediates injection: ugc.py treats these as pre-generated and skips
    # the corresponding steps. The conversions:
    #   scene_prompts -> skips scene planning + Director+Writer LLM call
    #   vo_script + vo_audio_url + vo_word_segments -> skips TTS round-trip if all three present
    #   music_url + music_description -> skips Suno call
    intermediates: Dict[str, Any] = {
        "scene_prompts": storyboard_to_ugc_scenes(sb),
    }
    if music.get("url"):
        intermediates["music_url"] = music["url"]
        intermediates["music_description"] = music.get("description", "")

    # VO injection: ugc.py only takes the VO-from-intermediates path when ALL THREE
    # of vo_script, vo_audio_url, vo_word_segments are present. The custom pipeline
    # synthesizes the audio BEFORE calling process_ugc_video so it can plumb all
    # three. The storyboard typically carries only the script — `custom.py` fills
    # in the audio_url + word_segments after a single ElevenLabs round-trip.
    if vo.get("audio_url") and vo.get("word_segments"):
        intermediates["vo_script"] = vo.get("script", "")
        intermediates["vo_audio_url"] = vo["audio_url"]
        intermediates["vo_word_segments"] = vo["word_segments"]

    # Fidelity dial: stash on intermediates for downstream readers (animation_router etc.)
    intermediates["_storyboard_fidelity"] = fidelity
    intermediates["_storyboard_meta"] = dict(meta)

    return ugc_kwargs, intermediates

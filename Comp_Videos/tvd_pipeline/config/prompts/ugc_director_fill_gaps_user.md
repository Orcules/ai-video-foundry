Some scenes in your video layout have TIMING GAPS — the assigned clip is shorter than the voice-over segment. You need to fill these gaps with additional clips.

YOUR SCENE LAYOUT (from Step 1 — scenes marked with NEEDS FILLER have a timing gap):
{scene_layout_text}

UNUSED CLIPS available to fill gaps:
{unused_clips_text}

*** YOUR TASK ***
For each scene marked NEEDS FILLER, pick ONE unused clip to fill the gap. The filler clip will play right after the primary clip in that scene.

Rules:
1. Each unused clip may be used in AT MOST one scene. Do not reuse.
2. Pick the clip whose content/vibe best complements the scene's primary clip and narrative role.
3. If no unused clip fits, set filler_clip_index to null AND provide a filler_idea — a short creative description of what to generate (1-2 sentences).

*** WHEN CHOOSING "GENERATE" (filler_clip_index = null) — RULES ***
You MUST provide a filler_idea describing the visual. Generated fillers may ONLY be neutral/generic visuals:
- City/location establishing shot (e.g., "Calm Prague street with cobblestones and warm evening light")
- Influencer reaction shot (e.g., "Young woman smiling with excitement, looking to the side")
- Text/typography overlay (e.g., "Clean text card with the restaurant address")
- Atmospheric detail (e.g., "Warm bokeh lights in a cozy evening setting")
NEVER generate product-specific visuals (food, interiors, merchandise) — only real clips show the real product.

RESPONSE FORMAT (JSON only, no other text):
{{
  "fillers": [
    {{
      "scene_number": 1,
      "filler_clip_index": 6
    }},
    {{
      "scene_number": 2,
      "filler_clip_index": null,
      "filler_idea": "Young woman walking down a charming Prague side street, golden hour light"
    }}
  ]
}}

Only include scenes that NEED FILLER. Do not include scenes that are fine.

Now fill the gaps:

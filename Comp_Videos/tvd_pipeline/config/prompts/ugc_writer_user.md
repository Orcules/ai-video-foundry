Write visual prompts for a UGC influencer video. You receive the story plan and media assignments — your job is to write first_prompt (image) and second_prompt (motion) for scenes that need AI generation.

SCENE PLAN (story structure + VO):
{scene_plan_json}

MEDIA ASSIGNMENTS (which media goes where):
{media_assignments_json}

REFERENCE IMAGE DESCRIPTIONS:
{ref_images_text}

INFLUENCER DESCRIPTION:
{influencer_description}

{location_context}
{venue_dna}

*** WHAT TO WRITE — AND WHAT TO SKIP ***
- ONLY write prompts for scenes where media_source is "generate".
- SKIP scenes where media_source is "video_asset" — those scenes use real footage and need no prompts. Do NOT include them in your output.
- ALSO write prompts for FILLER IDEAS: some video_asset scenes have a "filler_ideas" array — these are short clips that play AFTER the real footage to fill timing gaps. For each filler_idea, write a first_prompt and second_prompt. Use the filler_idea as creative direction. Include them in your output with a special key: "filler_prompts" (see response format below).
- For each scene you write, read its narrative_role and scene_intent from the plan, and its shows_influencer from the assignments.

*** HOW TO WRITE EACH SCENE ***
For each scene (that is NOT video_asset):

1. Read the scene_intent — understand WHAT the viewer should feel.
2. Read the vo_text — understand WHAT is being said during this scene.
3. Read shows_influencer — decide whether the influencer appears.
4. Read reference_image_index — if set, use that reference image as environmental inspiration.
5. Write first_prompt:
   - Start with "{style_prompt_prefix}"
   - Depict a vivid scene that serves the scene_intent and complements the vo_text
   - The visual does NOT need to literally mirror the VO — it should complement it emotionally
   - If shows_influencer is true: describe the influencer's ACTION and EMOTION only — do NOT describe their physical appearance (hair, clothing, body). The appearance comes from a reference image. Just describe what the influencer is DOING in the scene — never passive, never talking, never holding a phone
   - If shows_influencer is false: focus on environment, objects, atmosphere, or other people
   - If a reference image is assigned: use its colors, textures, and atmosphere as inspiration but create a fresh composition
   - Use the lens/composition that fits the narrative_role (see your system instructions)
   - Minimum 4 sentences. Be specific and sensory.
   - Body scenes: NO visible text, NO logo. Last scene only: you MAY add one short phrase in {language_name}.
   - NEVER use brand names — describe visual appearance instead.
6. Write second_prompt:
   - Match the narrative_role: hook = dynamic energy, problem = subtle tension, discovery = building curiosity, solution = smooth confidence, outcome = warm calm, cta = slow minimal
   - Describe camera movement + environmental motion
   - For influencer scenes: describe what they DO (gesture, react, interact) — NEVER talking
   - NEVER use brand or character names

*** STYLE RULES ***
- Style: {style_key}
- Forbidden words (NEVER use in first_prompt): {style_forbidden_csv}
- Language context: {language_name} (write prompts in English, but for {language_name} cultural context)

RESPONSE FORMAT (JSON only, no other text):
{{
  "scene_prompts": [
    {{
      "scene_number": 1,
      "first_prompt": "{style_prompt_prefix} A vivid, detailed scene description...",
      "second_prompt": "Camera slowly pushes in... environmental motion..."
    }},
    {{
      "scene_number": 3,
      "first_prompt": "{style_prompt_prefix} ...",
      "second_prompt": "..."
    }}
  ],
  "filler_prompts": [
    {{
      "scene_number": 2,
      "filler_index": 0,
      "first_prompt": "{style_prompt_prefix} A vivid scene based on the filler_idea...",
      "second_prompt": "Gentle camera movement..."
    }}
  ]
}}

NOTE: scene_numbers may not be consecutive — that is correct. Skipped numbers are video_asset scenes that need no prompts.
NOTE: filler_prompts are for timing-gap fillers within video_asset scenes. scene_number matches the parent scene, filler_index is 0-based (matching the order in filler_ideas array). Include filler_prompts only if there are filler_ideas in the media assignments. If there are none, omit the filler_prompts key entirely.

Now write prompts for the scenes that need them:

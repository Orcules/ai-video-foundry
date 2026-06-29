You are producing a UGC influencer video. In ONE response, assign all clips to beats AND write visual prompts for any generated clips.

BEATS (from VO, with durations):
{beats_text}

AVAILABLE CLIPS:
{clip_list_text}

DISSOLVE: {dissolve_seconds}s between each clip pair.

INFLUENCER: {influencer_description}

REFERENCE IMAGES:
{ref_images_text}

{highlights_section}
{location_context}
{venue_dna}
{cta_branding}

*** VISUAL STYLE: {style_key} ***
Style prefix for all first_prompt values: "{style_prompt_prefix}"
Forbidden words (NEVER use in first_prompt): {style_forbidden_csv}
Language context: {language_name}

═══════════════════════════════════════════════════════
INSTRUCTIONS
═══════════════════════════════════════════════════════

1. Assign clips to each beat. Each beat's total clip time must EXACTLY equal its duration.
2. You may use multiple clips per beat. You may split a beat across existing + generated clips.
3. Some clips have FIXED durations — use at their real length or trim shorter.
4. Some clips are FLEXIBLE — can be animated to any duration {min_clip_dur}-{max_clip_dur}s.
5. Generated clips: {min_clip_dur}-{max_non_influencer_dur}s each (influencer clips: {max_influencer_dur}s max).
6. For existing clips: set type="existing", clip_index=<number>, description=null, motion_prompt=null, first_prompt=null, second_prompt=null (EXCEPT INFLUENCER_IN_VENUE: fill description + motion_prompt, set first_prompt=null, second_prompt=null).
7. For generate clips: set type="generate", clip_index=null, description=null, motion_prompt=null — and WRITE first_prompt and second_prompt directly in that clip. These must be detailed, sensory, and visually distinct from every other generate clip.

═══════════════════════════════════════════════════════
OUTPUT FORMAT (JSON only, no other text)
═══════════════════════════════════════════════════════

{{
  "beats": [
    {{
      "beat_number": 1,
      "total_duration": 3.8,
      "clips": [
        {{
          "clip_index": 5,
          "type": "existing",
          "duration": 2.0,
          "shows_influencer": true,
          "description": "Close-up shot. She is sitting at the nearest table in the foreground, facing the camera. She fills the lower half of the frame, visible from the waist up. Use the exact same venue, don't change venue.",
          "motion_prompt": "She leans in slightly with eager energy and gives a small open-handed gesture toward the table. The camera makes a gentle push-in from a medium shot to a slightly tighter framing.",
          "first_prompt": null,
          "second_prompt": null,
          "reason": "Opens on influencer in real venue for authentic hook"
        }},
        {{
          "clip_index": null,
          "type": "generate",
          "duration": 1.8,
          "shows_influencer": false,
          "description": null,
          "motion_prompt": null,
          "first_prompt": "{style_prompt_prefix} A vivid, detailed scene description with lens, lighting, texture, and atmosphere. Minimum 4 sentences. Never generic.",
          "second_prompt": "A slow, graceful pull-back as colorful light reflections dance across the scene.",
          "reason": "Atmospheric transition fills the beat"
        }}
      ],
      "backup_clips": [
        {{
          "clip_index": 7,
          "type": "existing",
          "duration": 2.0,
          "shows_influencer": true,
          "description": "Close-up shot. She is standing near the entrance of the venue, looking around. Use the exact same venue, don't change venue.",
          "motion_prompt": "She steps forward and gestures warmly toward the interior. The camera follows with a slow dolly push-in.",
          "first_prompt": null,
          "second_prompt": null,
          "reason": "Alternative venue shot if primary fails"
        }}
      ]
    }}
  ]
}}

NOTES:
- backup_clips: required ONLY when the beat has an INFLUENCER_IN_VENUE existing clip; omit entirely for all other beats.
- first_prompt / second_prompt: MUST be filled for every generate clip; MUST be null for every existing clip (including INFLUENCER_IN_VENUE).
- Total beat durations must sum to VO length + dissolve overhead as described in your instructions.
- Make every generate clip visually distinct — different lens, composition, lighting, and emotion.

Now produce the complete clip assignment + visual prompts:

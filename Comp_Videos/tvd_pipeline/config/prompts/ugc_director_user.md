BEATS (from VO, with durations):
{beats_text}

AVAILABLE CLIPS:
{clip_list_text}

DISSOLVE: {dissolve_seconds}s between each clip pair.

INFLUENCER: {influencer_description}

{highlights_section}
{location_context}

Assign clips to each beat. Each beat's total clip time must EXACTLY equal its duration.
You may use multiple clips per beat. You may split a beat across an existing clip + a generated clip.
Some clips have FIXED durations — use at their real length or trim shorter.
Some clips are FLEXIBLE — can be animated to any duration {min_clip_dur}-{max_clip_dur}s.
Generated clips: {min_clip_dur}-{max_non_influencer_dur}s each (influencer clips: {max_influencer_dur}s max).

For each clip, set type to "existing" (from the clip list, with clip_index) or "generate" (new content, clip_index=null).

Return JSON matching the enforced schema. Each beat has beat_number, total_duration, a clips array, and an optional backup_clips array. Each clip has: clip_index (int for existing, null for generate), type ("existing"/"generate"), duration (seconds), shows_influencer (bool), description (string for generate or INFLUENCER_IN_VENUE, null for other existing), motion_prompt (string for INFLUENCER_IN_VENUE clips, null for all others), reason (why this clip fits here). For beats with INFLUENCER_IN_VENUE clips, include a backup_clips array with one alternative clip per venue clip.

Example beat:
{{
  "beat_number": 1, "total_duration": 3.8,
  "clips": [
    {{"clip_index": 5, "type": "existing", "duration": 2.0, "shows_influencer": true, "description": "She is sitting at the table, looking at the camera. Use the exact same venue, don't change venue.", "motion_prompt": "She leans in slightly with eager energy and gives a small open-handed gesture toward the table. The camera makes a gentle push-in from a medium shot to a slightly tighter framing.", "reason": "Opens on influencer in real venue for authentic hook"}},
    {{"clip_index": 2, "type": "existing", "duration": 1.8, "shows_influencer": false, "description": null, "motion_prompt": null, "reason": "Dining area pan fits the arrival beat"}}
  ],
  "backup_clips": [
    {{"clip_index": 7, "type": "existing", "duration": 2.0, "shows_influencer": true, "description": "She is standing near the entrance of the venue, looking around with excitement. Use the exact same venue, don't change venue.", "motion_prompt": "She steps forward and gestures warmly toward the interior. The camera follows with a slow dolly push-in.", "reason": "Alternative venue shot if primary fails"}}
  ]
}}
Watch this video. Extract its narrative structure AND the content/voiceover of each scene so a new video can take inspiration from it.

For each distinct scene or segment, provide:
1. narrative_role: one of hook, problem, solution, benefit, demo, result, cta, transition
2. duration_seconds: approximate length of that scene in seconds
3. content_summary: 1-2 sentences describing what we SEE in this scene and the key visual message (what is shown, mood, action)
4. vo_snippet: the voiceover or key line spoken in this scene (transcribe or paraphrase what is said). If no speech, write "[no speech]" or a short description of the mood (e.g. "upbeat music only").

Return a JSON object with this exact format (no other fields):
{{
  "scene_count": <number of scenes>,
  "scenes": [
    {{ "narrative_role": "<role>", "duration_seconds": <number>, "content_summary": "<what we see and key message>", "vo_snippet": "<what is said or [no speech]>" }},
    ...
  ]
}}

Preserve the order of scenes as they appear in the video. The sum of duration_seconds should approximate the total video length.
Output ONLY valid JSON, no markdown or explanation.
You are given a DETAILED SCRIPT / BRIEF written by the user for a product video.
Your job is to convert it into a scene-structured voice-over script that fits a {total_duration}-second video (EXACTLY {target_words} spoken words).

=== USER'S ORIGINAL SCRIPT / BRIEF ===
{raw_prompt}

=== ADDITIONAL CONTEXT ===
VIDEO TOPIC: {text_1}
VIDEO GOAL: {text_2}
{scene_structure_block}
=== INSTRUCTIONS ===
- Use the user's script as the PRIMARY SOURCE. Keep the original content, flow, tone, and messaging.
- IMPORTANT: Structure the output as {scene_count} scene segments separated by '|||'.
  Each segment is the VO text for one visual scene. The visuals will be generated to match each segment.
- HARD REQUIREMENT — the script MUST contain EXACTLY {target_words} spoken words. Do NOT write fewer or more.
- If the script is longer than ~{target_words} words, condense it while keeping the most impactful parts – the hook, key arguments, and call to action.
- If the script is shorter than ~{target_words} words, you may slightly expand but stay true to the original.
- Audio Tags ([excited], [happily], [awe], [sorrowful], [nervously], [whispers], [shouts], [softly], [dramatically], [laughs], [sighs], [gasps], [clears throat], [light chuckle], [pause]) do NOT count as spoken words. The {target_words}-word target counts ONLY actual spoken text — skip all [bracketed tags] when counting.
- Keep the SAME language as the original script. Do NOT translate.
- The output must sound natural when read aloud (voice-over style).
- Remove any section headers like "הוק 1:", "Hook 1:", stage directions, or formatting – output only the spoken text with ||| separators.{lang_instruction}{country_instruction}

ELEVENLABS v3 AUDIO TAGS (MANDATORY):
Embed Audio Tags in square brackets to control emotion and delivery. Use 4-8 tags total.
- Emotions: [excited], [happily], [awe], [sorrowful], [nervously]
- Delivery: [whispers], [shouts], [softly], [dramatically]
- Reactions: [laughs], [sighs], [gasps], [clears throat], [light chuckle]
- Pacing: [pause]
NOTE: These tags are TTS engine instructions, NOT spoken words. They do not count toward your {target_words}-word target.
Place tags BEFORE or WITHIN sentences where emotion shifts. Do NOT overuse.
Example: "[excited] You won't believe this! ||| [pause] It actually works. ||| [whispers] And the best part?"

Output ONLY the voice over text with Audio Tags and ||| scene separators. No headers, no notes, no stage directions.
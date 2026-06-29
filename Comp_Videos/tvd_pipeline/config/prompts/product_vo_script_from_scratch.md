Create a short, engaging, scene-structured voice over script for a product video.

VIDEO TOPIC:
{text_1}

VIDEO GOAL:
{text_2}

STYLE REQUIREMENTS:
{text_3}
{scene_structure_block}
REQUIREMENTS:
- Target duration: {total_duration} seconds
- HARD REQUIREMENT — the script MUST contain EXACTLY {target_words} spoken words. Do NOT write fewer or more.
- Audio Tags ([excited], [happily], [awe], [sorrowful], [nervously], [whispers], [shouts], [softly], [dramatically], [laughs], [sighs], [gasps], [clears throat], [light chuckle], [pause]) do NOT count as spoken words. The {target_words}-word target counts ONLY actual spoken text — skip all [bracketed tags] when counting.
- IMPORTANT: Structure the output as {scene_count} scene segments separated by '|||'.
  Each segment is the VO text for one visual scene. The visuals will be generated to match each segment.
  What is SAID in each segment must clearly describe or relate to what the viewer will SEE in that scene.
- Natural, conversational tone
- Clear call to action at the end
- NO pricing or specific claims
- Focus on benefits and emotional connection{lang_instruction}{country_instruction}

ELEVENLABS v3 AUDIO TAGS (MANDATORY):
Embed Audio Tags in square brackets to control emotion and delivery. Use 4-6 tags total.
- Emotions: [excited], [happily], [awe], [sorrowful], [nervously]
- Delivery: [whispers], [shouts], [softly], [dramatically]
- Reactions: [laughs], [sighs], [gasps], [clears throat], [light chuckle]
- Pacing: [pause]
NOTE: These tags are TTS engine instructions, NOT spoken words. They do not count toward your {target_words}-word target.
Place tags BEFORE or WITHIN sentences where emotion shifts. Do NOT overuse.
Example: "[excited] You won't believe this! ||| [pause] It actually works. ||| [whispers] And the best part?"

Output ONLY the voice over text with Audio Tags and ||| scene separators, no stage directions or notes.
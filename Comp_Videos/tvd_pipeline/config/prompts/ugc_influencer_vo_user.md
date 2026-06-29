LENGTH: This is a {target_duration:.0f}-SECOND video. Write {target_words}–{max_words} spoken words. Both floor and ceiling are hard — do not go under {target_words} or over {max_words}.

--- CONTENT (the VO must be 100% about THIS) ---
{raw_prompt_section}
{free_text}
{asset_context}
{highlights_section}
{text4_section}
{style_guidance}
{special_instructions}
{voice_style}

--- BEAT MAP ---
{arc_beats}

--- FORMAT ---
- Language: {language_name}
- First person only (I / me / my)
- Separate narrative beats with '|||'. Each beat = one complete narrative moment.
- The LAST segment must be a complete closing sentence (CTA or conclusion). Do NOT end mid-sentence.
- NO stage directions like (pauses). Use ElevenLabs Audio Tags throughout the script. Full list: [excited], [happily], [awe], [sorrowful], [nervously], [whispers], [shouts], [softly], [dramatically], [laughs], [sighs], [gasps], [clears throat], [light chuckle], [pause]
- Embed 6–10 Audio Tags naturally: use at least one tag per segment where it fits, and place tags at emotional or emphatic moments (e.g. [dramatically] before a key line, [happily] at payoff, [softly] for intimacy, [excited] for discovery). Vary the tags; do not repeat the same tag in every segment.
- Output ONLY the spoken script + Audio Tags + ||| separators, nothing else

--- STRUCTURE ---
Follow the beat map above. Each beat separated by '|||'. Last segment = satisfying, complete ending. Passionate, genuine, emotional.
Do NOT put the professional's name or service provider's name in every segment. Tell the STORY in each segment; mention the name only once at the end (CTA) if at all. Emphasis = storytelling, not name-dropping.

HARD RANGE: {target_duration:.0f}-second video = {target_words}–{max_words} spoken words. Under {target_words}? Expand. Over {max_words}? Cut. Ending must be complete. Write the full script now.
{hebrew_nikud_reminder}
Plan a 9-cell UGC ad storyboard.

**UGC creator (identity — same person in every talking-head / `character_talking` cell; match reference images if provided):**
- Character description: {character_description}
- Gender (for voice-of-creator consistency): {gender}

**Structured offer (every cell must stay aligned with this — same facts, tone, and promise):**
- Target audience: {target_audience}
- Main problem solved: {main_problem}
- Key benefits & offer details: {key_benefits}
- CTA line: {cta_text}

**Original creator brief (full main prompt — use for product facts, context, and voice):**
{original_prompt}

**Offer profile (LLM-structured analysis, JSON):**
{offer_profile_json}

**Creative strategy (JSON):**
{creative_strategy_json}

**Target duration:** {duration_sec} seconds
**Language:** {language} (speech rate: ~{wps} words/second)
**Offer type:** {offer_type}
**Ad format:** {ad_format}

**Voice line word count targets (CRITICAL — do not write shorter lines):**
- Total spoken words across all 9 cells: ~{total_target_words} words
- Each `lipsync: true` cell (cells 1, 5, 9): ~{lipsync_words} words per voice_line
- Each `lipsync: false` cell: ~{nonlipsync_words} words per voice_line
- These targets are calculated from the language speech rate so that the assembled VO audio matches the requested duration.
- Write COMPLETE SENTENCES that fill the allotted word budget. Do not write single-sentence stubs.

Produce exactly 9 cells. Return JSON only.

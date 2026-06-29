Build a complete storyboard JSON for the project below. Return ONLY JSON matching the response schema — no prose, no markdown, no commentary.

# User's brief (latest chat turn)
{user_brief}

# Full chat context (most recent first)
{chat_history}

# Slots the Concierge gathered so far
```json
{slots_json}
```

# Uploaded media
```json
{assets_json}
```

# Constraints (must be honored verbatim)
- `meta.target_duration_seconds` = {target_duration}
- `meta.language` = "{language}"
- `meta.style` = "{style}"
- `meta.fidelity_to_assets` = {fidelity}
- `meta.aspect_ratio` = "{aspect_ratio}"

# Reminders
- Hook in the first 1.5–2s.
- One primary camera move per clip.
- Lock character/venue via sheets + ingredients.
- Pick the right clip type per beat (see cheat sheet in the system prompt).
- Use the chat history to infer tone, urgency, target audience.
- If the brief is mostly clear, COMMIT — don't ask follow-ups.

Output: one JSON object. Nothing else.

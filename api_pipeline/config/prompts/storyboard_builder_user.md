Build a full storyboard JSON for the following project. Return ONLY JSON matching the response schema — no prose, no markdown.

# Conversation context (chat agent gathered these from the user)
{chat_context}

# Approved plan slots
```json
{slots_json}
```

# Uploaded assets the user provided
```json
{assets_json}
```

# Hard constraints
- `meta.target_duration_seconds` MUST equal {target_duration}
- `meta.language` MUST equal "{language}"
- `meta.style` MUST equal "{style}"
- `meta.fidelity_to_assets` MUST equal {fidelity}
- All asset indices in `clips[].source` must be in range for the uploaded lists above. Do not reference indices that don't exist.
- Sum of scene durations MUST equal {target_duration} (±0.5s tolerance).

Plan the visual mix using the fidelity dial. Write a tight VO script in {language}. Pick the right clip type per beat. Set tool_hint to "auto" unless you have a strong reason to override.

Output: one JSON object. Nothing else.

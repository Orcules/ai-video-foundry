The influencer is {gender_word} ({gender_pronoun}/{gender_possessive}).

Write motion prompts for the {count} images above.

For each image:
- Always write a regular variant (description + the exact hardcoded motion prompt).
- Images at indices {candidate_indices} have been identified as containing whimsical/animatable elements.
  For ONLY those images, also write a surprise variant with a creative motion prompt.
- For all other images, set the surprise fields to null.
- Images at indices {venue_candidate_indices} show venues where the influencer can be placed.
  For ONLY those images, also write venue variant fields.
- For all other images, set venue fields to null.

Use 0-based indexing (first image = index 0).

Return JSON with an "images" array.

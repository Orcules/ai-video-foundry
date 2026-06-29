Build a single image generation prompt for a 3x3 grid of 9 UGC ad frames.

The image model will receive a **first reference image**: blank 9:16 canvas with nine equal cells in a 3×3 grid (structural template). In your prompt, state clearly that the output must **mirror that grid geometry** (9:16, equal rectangles, straight dividers, reading order left-to-right top-to-bottom) and fill each cell with the appropriate scene — never a freeform collage.

**Creator gender (must match API / Studio selection — never flip):**
{gender}

**UGC creator (lock this identity across all cells that show the face):**
{character_description}

**Product / offer context (the grid must match this — not a generic product):**
{product_description}

**Style DNA:**
{style_dna_json}

**9-cell plan:**
{nine_cell_plan_json}

**Offer type:** {offer_type}
**Visual style:** {visual_style}

Write the complete image generation prompt. Plain text only, no JSON.

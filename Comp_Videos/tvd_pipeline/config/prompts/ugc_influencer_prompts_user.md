Generate prompts for a {scene_count}-scene UGC influencer video that will GO VIRAL.{influencer_section}

PRODUCT/EXPERIENCE TO PROMOTE:
{free_text}
{vo_timing_block}
*** CRITICAL: IMAGE = VISUAL OF WHAT THE VO SAYS ***
For each scene you MUST read the VO text for that scene (in the timing block below). The first_prompt must describe an image that shows EXACTLY what the viewer hears. If the VO says "I thought it wasn't for me" → show that doubt (person hesitating, skeptical). If the VO says "then I tried it" → show the moment of trying. No decorative or generic images. The story is interesting only when picture and words tell the same moment.

SCENE PLAN (build a CAPTIVATING, SPECIFIC STORY — not bland or generic):
- STORY = ONE CONCRETE JOURNEY: The story must be a SPECIFIC situation (what was going wrong? where? for whom?) → turn (what changed? what did they see or do?) → payoff. NOT generic "many people struggle" or "I tried it and it was great." Think: "one real person's specific moment, told like a short film."
- NARRATIVE ARC: Early scenes = the CONCRETE PROBLEM or SITUATION (one specific moment, not "person stressed at desk"). Middle = the TURN or discovery (the "aha" moment—what exactly happens?). Late = the PAYOFF (the result, the feeling). Last scene = CTA. Every scene must feel like a beat from THIS story only, not a stock scene.
- ONE CLEAR MESSAGE PER SCENE, TIED TO THE VO: For each scene, the first_prompt must depict exactly what the VO says in that scene's time window. No generic shots. Match scene N to VO scene N. Same order, same meaning. Before writing each first_prompt, quote the VO for that scene in your head; the image must be a direct visual translation of that quote.
- SMART INFLUENCER PLACEMENT (based on VO): set shows_influencer TRUE only when the VO talks about the influencer themselves (first person: "I...", "my..."). When the VO talks about other people → shows_influencer = FALSE, show those people/situation instead.
- Scene 1 = THE HOOK: A stunning visual that matches what the VO says in scene 1 — showing the relatable situation or an attention-grabbing moment. No product UI/logo in the hook, but the scene IS about the product's world.
- Middle scenes: Each first_prompt must illustrate that scene's VO text. Show the PEOPLE or SITUATION described in the VO. When the VO reveals the product/solution, you CAN show the product. The influencer appears only when the VO is about them.
- Last scene (Scene {scene_count}) — CTA, MUST connect to the video and offer: Prefer a CLEAN ending — no character (shows_influencer = false), professional card with background that fits the product/offer. Set shows_influencer = true ONLY when the VO is explicitly personal ("contact me", "message me", "call me"). first_prompt must describe a visual that clearly ties to THIS video's offer (e.g. product category, benefit, brand world). When the influencer appears, use a WIDE or MEDIUM shot. No service or person name as text in the image.{manual_section}{cta_section}

IMAGE AND CHARACTER PLACEMENT LOGIC (CRITICAL — think before placing):
You have {ref_image_count} reference image(s) (Image 1, Image 2, Image 3, Image 4{extra_image_label} as provided) with descriptions below, AND a VO script split into scenes. For EACH scene you must decide:

A) REFERENCE IMAGE PLACEMENT ("reference_image_index"):
- Use the reference images — do not leave them unused when they can support a scene. Prefer to assign each of Image 1 to Image {ref_image_count} to at least one scene where it fits the VO and the story.
- Read the VO text for this scene AND each reference image description below.
- If a reference image MATCHES what the VO describes (e.g. VO says "we sat at this restaurant" and Image 2 shows a restaurant interior) → set reference_image_index to that image (0-based).
- If NO reference image fits what the VO says in this scene → set reference_image_index to null. The system will generate a fresh image purely from your prompt. Do NOT force a reference image into a scene where it does not fit the story.
- Can reuse images across scenes when relevant. A reference image can appear in multiple scenes if the story calls for it.

*** CRITICAL — IMAGE TYPE PLACEMENT RULES ***
Each reference image below has a [TYPE] tag. You MUST follow these placement rules:

[PRODUCT_SCREENSHOT] or [PRODUCT_PHOTO] images:
  → Use ONLY in scenes that present the SOLUTION, DISCOVERY, or DEMONSTRATION of the product.
  → This is typically the scene where the VO reveals the answer/product — the "aha moment" (usually middle-to-late scenes).
  → NEVER use product images in: the HOOK (scene 1), PROBLEM scenes, or scenes describing a negative situation.
  → The product image should appear at the moment of DISCOVERY or REVEAL — when the story shifts from problem to solution.

[LOCATION], [LIFESTYLE], [FOOD], [PERSON], [OTHER] images:
  → These show environments, experiences, and people. Use them in ANY scene where they match the VO content.
  → They CAN appear in early scenes, problem scenes, hook scenes — wherever the setting is relevant.
  → Match the image to the MOOD and CONTENT of the VO for that specific scene.

STORY ARC → IMAGE TYPE MAPPING:
  Hook/Problem scenes (early) → LOCATION/LIFESTYLE/FOOD images or generate fresh. NEVER product images here.
  Discovery/Solution scenes (middle) → This is where PRODUCT images belong — the moment the viewer sees the answer.
  Result/Payoff scenes (late) → LOCATION/LIFESTYLE showing the positive outcome. PRODUCT images can also work here.
  CTA scene (last) → Logo/brand moment (handled separately).

REFERENCE IMAGE DESCRIPTIONS:
{ref_images_text}

{venue_dna}
VIDEO ASSET MATCHING:
{asset_matching_context}

MEDIA ASSIGNMENT RULES:
- Each reference_image_index may be used in AT MOST ONE scene. Do not show the same photo twice.
- Each video_asset_index + best_moment_index COMBINATION must be unique. Do not use the same moment twice.
- HOWEVER: you MAY assign the same video_asset_index to MULTIPLE consecutive scenes if you use DIFFERENT best_moment_index values for each. This is called "sequential spanning" — a long asset plays across multiple scenes using different moments, creating a flowing continuous shot while the VO narrates on top. Prefer sequential (ascending) moment indices for natural flow.
- A scene can have a reference_image_index, a video_asset_index, or neither — but NOT both. If a video asset fits the scene, prefer the video (real footage > AI-generated).
- If no asset fits a scene, set video_asset_index to null (the scene will be AI-generated).
- For each scene matched to a video asset, set best_moment_index:
  - Set to a moment index (0, 1, 2...) to focus on that specific moment.
  - Set to -1 to use the entire clip as-is — ONLY when the asset duration is close to the scene duration (within ~2s). Do NOT use -1 for a long asset matched to a short scene.

B) CHARACTER PLACEMENT ("shows_influencer"):
- The character/influencer should appear ONLY when it makes STORY SENSE — not forced into every scene.
- When the VO talks about the character themselves (first person: "I...", "my...", "we...") → shows_influencer = TRUE.
- When the VO talks about OTHER people, abstract concepts, or situations → shows_influencer = FALSE. Show THOSE people/situations.
- Flashback scenes about the past or about other people's stories → shows_influencer = FALSE.
- LAST SCENE (CTA): Must be connected to the video and the offer. Prefer CLEAN CTA: no character (shows_influencer = false), ending card with background that fits the offer. Only set shows_influencer = true when the VO is explicitly a personal close ("contact me", "message me"). Otherwise clean CTA — no characters.
- If the story is about multiple characters, include ALL of them when they are part of the narrative.

C) COMBINING IMAGES AND CHARACTERS:
- When shows_influencer = TRUE AND a reference image fits → the image generator will combine the character reference with the environment reference. Your prompt should describe the character IN that environment.
- When shows_influencer = FALSE AND a reference image fits → just the environment/situation, no character.
- When shows_influencer = TRUE AND reference_image_index = null → the character in a generated scene matching the VO.
- When shows_influencer = FALSE AND reference_image_index = null → a generated scene matching the VO, no character.

IMPORTANT - DESCRIBE VISUALS, NOT BRANDS:
When inspired by reference images, describe what you SEE (colors, shapes, textures, atmosphere) not what it IS by name.
Example: Instead of "the iconic Mickey Mouse parade float", write "a dazzling golden parade float covered in shimmering lights, vibrant flowers, and whimsical sculptures, with a festive crowd cheering in the background"

TEXT RULES: Body scenes (1 to {last_body_scene}): no on-screen text, no logo, no brand name. Last scene only: you MAY add one short phrase in {language_name} if it fits. If you include text in any scene (e.g. on a screen the character views), it must be DIEGETIC — part of an object in the scene (monitor, hologram, sign, document), never floating labels, infographic titles, or split-panel captions like "STRUGGLING SECTOR" / "RESILIENT SECTOR". One narrative frame per scene, not a slide or comparison card.

*** ANTI-BLAND — NO STOCK SCENES ***
Every scene must feel like a still from a SPECIFIC story moment, not a stock photo.
- FORBID: Generic "person at desk", "person with laptop", "person smiling at camera", "modern office", "team celebrating". These are bland and forgettable.
- REQUIRE: One SPECIFIC moment—the exact gesture, the object in frame that matters to the story, what just happened or is about to happen. So the frame could only belong to THIS video. Use unexpected angle or detail (over-shoulder, close-up on hands, reflection, the second before the reaction) so it feels like a film, not an ad.

*** PROMPT QUALITY — BEAUTIFUL, AESTHETIC, MINIMAL (Nano Banana / image gen) ***
Every first_prompt must produce an image that is BEAUTIFUL, REFINED, and ELEGANT—like a premium magazine or film still. The frame must feel pleasing and uncluttered.
- AESTHETIC FIRST: Describe 2–4 elements max. Generous negative space, soft or simple background (bokeh, gradient, clean wall). No busy scenes, no clutter. Premium look.
- Use VIVID, SENSORY language: specific light (soft window, golden hour), colors, one striking detail. Be CONCRETE and evocative.
- Include MOOD: curiosity, anticipation, relief, wonder. ONE striking visual hook per scene.
- NO filler objects or "and also" elements. Every sentence must add one concrete visual or mood. Simple, clear, beautiful—not crowded.

*** PER-SCENE TYPE — WRITE SMARTER PROMPTS BY ROLE ***
For each scene, set "narrative_role" to exactly one of: "hook", "problem", "discovery", "solution", "outcome", "cta".
Then tailor first_prompt and second_prompt to that role:

- **hook** (Scene 1): first_prompt = attention-grabbing, visceral, one clear striking image. second_prompt = dynamic: push-in or quick reveal, energy. If a person: describe their expression (e.g. intent gaze, curiosity) — human and tied to the story.
- **problem**: first_prompt = relatable struggle, tension. second_prompt = subtle tension: slow push, slight unease. If a person: describe expression (e.g. furrowed brow, concern, concentration).
- **discovery**: first_prompt = the turn, the "aha" visible in light or expression. second_prompt = building energy, curiosity. If a person: describe expression (e.g. eyes widening, slight smile of realization).
- **solution**: first_prompt = the product/answer in use, satisfying. second_prompt = clear, confident motion. If a person: describe expression (e.g. confident smile, focused satisfaction).
- **outcome**: first_prompt = positive result, relief, joy. second_prompt = warm, relaxed motion. If a person: describe expression (e.g. relaxed smile, relief, contentment).
- **cta** (last scene): first_prompt = clean closing card (no character unless VO is personal "contact me"). second_prompt = slow, minimal motion; if a person: inviting expression (e.g. warm eye contact, slight smile).

FOR EACH SCENE (every image must match what the VO says in that scene's time window—precise timing):
1. **narrative_role**: One of hook, problem, discovery, solution, outcome, cta — based on this scene's place in the story and the VO content.
2. **first_prompt**: Read this scene's VO text AND its narrative_role. Depict EXACTLY what is being said. Start with "{style_prompt_prefix}". Describe a BEAUTIFUL, REFINED, minimal scene—only 2–4 key elements, generous negative space, soft or simple background (e.g. bokeh, gradient, clean wall). One clear focal point, specific light and color, ONE striking detail. AESTHETIC: like a premium magazine or film still—elegant, uncluttered, pleasing to the eye. Never describe busy backgrounds, many props, or crowded compositions. Minimum 4 sentences; every sentence adds one concrete visual or mood. Use ONLY words consistent with the {style_key} style — NEVER use these forbidden words: {style_forbidden_csv}. Set shows_influencer based on WHO the VO is about. Follow the IMAGE TYPE PLACEMENT RULES above. Body scenes: no text, no logo. Only in the LAST scene you may include one short phrase in {language_name} if it fits. CRITICAL: One narrative frame per scene — no infographic layout, no split panels, no floating titles or labels. If text appears (e.g. on a screen), it must be part of an object in the scene (diegetic), not a caption or banner.
3. **second_prompt**: Match the narrative_role (camera movement + motion). When a person appears in the scene, you MUST describe their facial expression — specific, human, and connected to what the story is saying in that moment (e.g. "slow push-in, person with focused expression of concentration"; "slight zoom, eyes widening with discovery, subtle smile"). The expression should reflect the emotional beat of the VO. Different camera movement per scene. For influencer scenes: what they DO (never talk) and their expression. NEVER use brand or character names.

RESPONSE FORMAT (JSON):
{{
    "influencer_description": "DETAILED physical description - face, hair, body type, skin tone, style",
    "scene_prompts": [
        {{
            "scene_number": 1,
            "narrative_role": "hook",
            "shows_influencer": true/false,
            "reference_image_index": 0,
            "video_asset_index": null,
            "best_moment_index": null,
            "first_prompt": "...",
            "second_prompt": "..."
        }}
    ]
}}

*** CRITICAL ASSET USAGE REMINDER ***
You MUST use the provided reference images and video assets. Do NOT set all reference_image_index and video_asset_index to null — that wastes the real media the user uploaded. Assign EVERY reference image to exactly one scene where it fits the VO. Assign EVERY video asset to at least one scene (use the best matching moment). Real footage is ALWAYS better than AI-generated — prefer video assets over generating new content. Only set null when an asset genuinely does not fit ANY scene.

Now create {scene_count} BREATHTAKING scenes. Scene 1 must be the most VISUALLY STUNNING hook imaginable:

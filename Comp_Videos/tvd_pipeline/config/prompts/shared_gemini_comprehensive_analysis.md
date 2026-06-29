{goal_statement}

{language_context}
{article_context}
{instructions_context}
{transcript_context}

{workflow_steps}

You will output:
1. **VIDEO STORY UNDERSTANDING** - Complete narrative, story type, subject journey, scene connections
2. **IMAGE PROMPTS** - PRECISE prompts for Nano Banana that match the ORIGINAL video's visuals exactly
3. **MOTION PROMPTS** - PRECISE animation prompts for Kling/Runway that match the ORIGINAL video's movement
4. **NEW VOICEOVER SCRIPT** - Complete VO script in the target language that matches the story
5. **CTA BUTTON** - If the video needs a call-to-action button

🔑 CRITICAL RULES FOR PROMPTS:

⚠️⚠️⚠️ PRODUCT VISIBILITY - MOST IMPORTANT ⚠️⚠️⚠️
THE PRODUCT DOES NOT APPEAR IN EVERY SCENE!
- Watch the ORIGINAL video carefully: In which scenes is the product actually VISIBLE?
- If the product is NOT visible in a scene → Set product_visible=false and DO NOT mention the product in the image_prompt
- If the product IS visible in a scene → Set product_visible=true and include it with POSITIVE, SPECIFIC description
- Example: Scene 1 shows product application (product_visible=true), Scene 2 shows person walking away (product_visible=false), Scene 3 shows result with product visible (product_visible=true)

**IMAGE PROMPTS (for Nano Banana) - MUST MATCH ORIGINAL VIDEO EXACTLY:**
- Watch the ORIGINAL video at this scene's timestamp - what do you ACTUALLY see?
- Recreate the EXACT visual: subject appearance, clothing, setting, lighting, camera angle
- ⚠️ ONLY include product if product_visible=true for this scene!
- If product_visible=true: describe product EXACTLY as it appears (color, shape, size, materials, EXACT location on body/object)
- If product_visible=false: DO NOT mention the product at all in the prompt
- ⚠️ CRITICAL: If subject changes between scenes (weight, appearance, mood, clothing) - describe the EXACT state in THIS scene
- Match the ORIGINAL video's visual style: camera angle, framing, lighting, mood
- Be SPECIFIC and DETAILED - the prompt should recreate the original scene visually
- Focus on describing what IS visible: people, objects, environments, lighting, mood, camera angles
- Format: "Photorealistic [exact shot type from original], [exact subject appearance from original], [exact action from original], [exact setting from original], [exact lighting from original], [exact mood from original]"

**MOTION PROMPTS (for video generation) - MUST MATCH ORIGINAL VIDEO EXACTLY:**
- Watch the ORIGINAL video at this scene's timestamp - what movement do you ACTUALLY see?
- Describe the EXACT movement: camera movement, subject motion, speed, direction
- Match the ORIGINAL video's pacing and style
- Keep under 200 characters
- Be SPECIFIC about the movement type (zoom, pan, static, tracking, etc.)
- Format: "[Exact camera movement from original], [exact subject action from original], [exact speed/style from original]"

**PRODUCT LOGIC - CRITICAL FOR ACCURACY:**
⚠️⚠️⚠️ FIRST: Determine if product is visible in EACH scene by watching the original video!

For EACH scene, you must:
1. Watch the original video at that scene's timestamp
2. Check: Is the product VISIBLE in this scene?
3. Set product_visible=true ONLY if product is actually visible
4. Set product_visible=false if product is NOT visible (even if product exists in the video overall)

**PRODUCT VISUAL DESCRIPTION - EXTREMELY DETAILED:**
When describing the product in "visual_description", you MUST provide an EXTREMELY DETAILED 400+ word description that includes:

1. **EXACT SHAPE AND DIMENSIONS:**
   - Precise shape (circular, rectangular, oval, irregular, etc.)
   - Exact dimensions (e.g., "2cm diameter", "3cm x 4cm rectangle", "covers palm of hand")
   - Relative size to body parts or objects (e.g., "half the size of a credit card", "slightly larger than a quarter")

2. **EXACT COLORS (with specific hex codes or precise color names):**
   - Primary color with specific shade (e.g., "bright orange #FF6600", "pale beige #F5F5DC", "deep navy blue #000080")
   - Secondary colors if any (e.g., "white outer ring #FFFFFF", "transparent center")
   - Gradients or color transitions if present
   - NOT just "orange" or "white" - be SPECIFIC!

3. **EXACT MATERIALS AND TEXTURES:**
   - Material type (e.g., "smooth adhesive gel center", "matte white outer ring", "transparent film backing")
   - Texture description (e.g., "glossy surface", "matte finish", "smooth", "textured", "opaque", "semi-transparent")
   - How light interacts (e.g., "reflective surface", "absorbs light", "translucent")

4. **PRODUCT BRANDING (CRITICAL - MUST REMOVE!):**
   ⚠️ DO NOT include any text, logos, or branding on the product surface!
   - If the original product has text/logos → REMOVE them in your prompts
   - The product surface must be CLEAN and PLAIN
   - Describe the product's physical features ONLY (shape, color, material, texture)
   - Example: Instead of "patch with 'SlimFast' logo" → "plain circular patch with orange center"

5. **EXACT PACKAGING (if visible):**
   - Package color, shape, material
   - How product appears in packaging
   - NOTE: Package branding should also be removed/ignored

6. **EXACT PLACEMENT AND ORIENTATION:**
   - Precise location on body/object (e.g., "centered on lower abdomen 5cm below navel", "on right cheekbone")
   - Orientation (e.g., "horizontal", "vertical", "diagonal at 45 degrees")
   - How it sits on surface (e.g., "flat against skin", "slightly raised", "curved to match body contour")

7. **EXACT LIGHTING AND SHADOWS:**
   - How light hits the product (e.g., "soft natural light from above creates subtle highlight on center")
   - Shadow details (e.g., "slight shadow cast on skin below", "no shadow, flush with skin")

8. **EXACT PERSPECTIVE AND CAMERA ANGLE:**
   - Camera angle relative to product (e.g., "top-down view", "45-degree angle from side", "eye-level")
   - How product appears from this angle (e.g., "circular shape appears slightly oval from this angle")

9. **UNIQUE FEATURES:**
   - Any patterns, designs, or distinguishing marks
   - Any special characteristics that make this product identifiable

When product_visible=true, describe it POSITIVELY and SPECIFICALLY:
⚠️ CRITICAL: Use POSITIVE descriptions, NOT negative ones!
❌ BAD: "patch not on forehead, not on clothes"
✅ GOOD: "small circular patch (2cm diameter) with bright orange gel center (#FF6600) and matte white outer ring (#FFFFFF), adhered to the bare skin of the lower abdomen 5cm below navel, visible on clean exposed stomach area, soft natural lighting creates subtle highlight on glossy gel surface"

1. **SKIN PRODUCTS (patches, stickers, creams, serums):**
   - **Patches/stickers for weight loss/slimming:**
     * MUST say: "adhered to the bare skin of the [specific body part: stomach/abdomen, arm, thigh, etc.]"
     * MUST say: "on clean exposed skin" or "on bare skin visible under/above clothing"
     * Example: "small circular patch adhered to the bare skin of the lower abdomen, visible on clean exposed stomach area"

   - **Face patches/creams:**
     * MUST say: "applied to the [specific face area: forehead, cheek, under-eye, etc.]"
     * Example: "patch adhered to the forehead" or "cream being massaged into the cheek"

   - **Body creams/lotions:**
     * MUST say: "being rubbed/massaged into the [specific body part: arm, leg, stomach, etc.]"
     * Example: "cream being massaged into the bare skin of the arm"

2. **PET PRODUCTS (toys, treats, accessories):**
   - MUST say: "[pet type] actively [action: playing with, chewing, interacting with] the [product]"
   - Example: "Border Collie actively chewing and playing with the colorful ball toy"

3. **FOOD/DRINKS:**
   - MUST say: "[person/pet] [action: eating, drinking, preparing] the [product]"
   - Example: "person drinking from the bottle" or "hands preparing the food"

4. **SUPPLEMENTS/PILLS:**
   - MUST say: "hand holding [product] near mouth" or "[product] being taken from package"

**CRITICAL - PRODUCT VISIBILITY RULES:**
⚠️ THE PRODUCT DOES NOT APPEAR IN EVERY SCENE!
- Analyze the ORIGINAL video carefully: In which scenes is the product VISIBLE?
- In scenes where the product IS visible → Include it with POSITIVE, SPECIFIC description with EXACT colors (hex codes), dimensions, materials, and placement
- In scenes where the product is not visible → Focus on describing what IS visible: the subject, clothing, setting, lighting, mood, camera angle
- Example: If scene 1 shows product application (include product), scene 2 shows person walking (focus on person and setting), scene 3 shows result with product (include product) → Scenes 1 and 3 include product, scene 2 focuses on other visual elements

🚫🚫🚫 CRITICAL - BRANDING AND TEXT RULES 🚫🚫🚫

**PRODUCT SURFACE = NO TEXT/BRANDING (MANDATORY!)**
- Products must be shown COMPLETELY CLEAN - no text, no logos, no branding
- If original product has brand name/logo → REMOVE IT in your prompts
- The product surface must be plain and clean
- Example: Original shows "SlimPatch™" on product → Describe as "plain circular patch" (NO text on product)

**TEXT OVERLAYS - STRICT RULES!**

🚨 RULE 1: CHECK IF ORIGINAL VIDEO HAS TEXT OVERLAY
- Watch the original video carefully
- Does it have text/branding overlays on the screen (not on product)?
- If YES → You may add text overlay (extracted from article)
- If NO → DO NOT add any text overlay! Keep image CLEAN!

🚨 RULE 2: CHECK MANUAL INSTRUCTIONS
- If Manual Instructions say "remove text" or "no text" → NO text overlay at all!
- If Manual Instructions say "add text" → Add text even if original didn't have
- Manual Instructions OVERRIDE the original video analysis

🚨 RULE 3: TEXT MUST BE FROM THE ARTICLE CONTENT
- The text MUST be extracted from the article/Free text content
- Find the main offer, discount, benefit, or call-to-action IN THE ARTICLE
- Write it in the target language

✅ CORRECT EXAMPLES (text extracted from article):
- Article says "50% discount on all models" → Text: "50% OFF ALL MODELS"
- Article says "we're hiring drivers" → Text: "WE'RE HIRING!"
- Article says "free shipping this week" → Text: "FREE SHIPPING"
- Article says "משלוח חינם לכל הארץ" → Text: "משלוח חינם"

❌ WRONG - DON'T DO THIS:
- "promotional text" ← Technical description, not real text!
- "SALE" when article doesn't mention a sale ← Not from article!
- "BUY NOW" when article is about job hiring ← Unrelated!
- Adding text when original video had no text ← Violates Rule 1!

📋 DECISION FLOWCHART:
1. Does Manual Instructions say "remove text"? → NO TEXT
2. Does Manual Instructions say "add text"? → ADD TEXT (from article)
3. Does original video have text overlays? → If NO → NO TEXT
4. If original has text → Extract message from article → ADD THAT TEXT

| Condition | Action |
|-----------|--------|
| Manual says "remove text" | NO text overlay |
| Manual says "add text" | ADD text from article |
| Original has text + no manual override | ADD text from article |
| Original has NO text + no manual override | NO text overlay |

**WHEN DESCRIBING PRODUCTS IN IMAGE PROMPTS - POSITIVE LANGUAGE ONLY:**
⚠️ REMEMBER: Only describe product if product_visible=true for THIS scene!

If product_visible=true:
- ✅ DO: "patch adhered to the bare skin of the stomach area, visible on clean exposed abdomen"
- ❌ DON'T: "patch not on forehead, not on clothes"
- ✅ DO: "small circular patch on the lower abdomen, skin visible around it"
- ❌ DON'T: "patch not floating, not on fabric"
- Be SPECIFIC about EXACT location (stomach, arm, face area, etc.)
- Be SPECIFIC about EXACT action (adhered, being applied, being massaged, etc.)
- Include POSITIVE context (bare skin visible, clean exposed area, etc.)

If product_visible=false:
- ✅ DO: Describe the scene focusing on what IS visible - the subject, clothing, setting, lighting, mood, camera angle
- ✅ DO: "Photorealistic medium shot, young woman with slim athletic build wearing black workout clothes, confidently walking through bright modern bedroom, natural window lighting, energetic happy mood"
- Focus on describing the visual elements that ARE present in the scene

**VOICEOVER SCRIPT:**
- Match the STYLE and TONE of the original
- Use content from the article provided
- Match the video duration
- If original is energetic → new VO should be energetic
- If original is calm → new VO should be calm

Return a JSON object with these sections:

{{
  "scenes": [
    {{
      "scene_number": 1,
      "start_time": "0:00",
      "end_time": "0:03",
      "duration_seconds": 3,

      "understanding": {{
        "what_happens": "<describe EXACTLY what happens in this scene - watch the original video at this timestamp and describe what you see>",
        "narrative_role": "<hook/problem/solution/benefit/demo/result/cta/transition>",
        "story_connection": "<How does this scene connect to the previous scene? What changed? What's the progression?>",
        "subject_appearance": "<CRITICAL: How does the subject look in THIS SPECIFIC scene? Be EXACT: body type, clothing, expression, state, position. Match the ORIGINAL video exactly>",
        "visual_details": "<EXACT visual details from original: camera angle, framing, lighting, setting, colors, mood>",
        "text_on_screen": "<ONLY if original video has text overlay on screen: Extract the main offer/message from the article and write it here (e.g., '50% OFF', 'משלוח חינם'). If Manual Instructions say 'remove text' → leave EMPTY. If original has NO text overlay → leave EMPTY. The text MUST come from the article content!>",
        "has_branding_overlay": true | false,
        "product_visible": true | false,
        "product_action": "<what's being done with product, if visible - be specific about the action>",
        "changes_from_previous": "<What changed from the previous scene? Subject appearance? Setting? Mood? Product visibility?>"
      }},

      "prompts": {{
        "image_prompt": "<COMPLETE, READY-TO-USE prompt for Nano Banana. Must be photorealistic, detailed, include all visual elements.
        ⚠️ CRITICAL: Only include the product in the prompt if 'product_visible' is true for this scene!
        🚫 PRODUCT MUST BE CLEAN - NO text, logos, or branding on the product surface!

        PRODUCT RULES:
        - If product_visible=true: Include product with POSITIVE, SPECIFIC description with EXACT colors (hex codes), dimensions, materials, and placement. Product must be PLAIN with no text/branding on it.
        - If product_visible=false: Describe the scene focusing on what IS visible - the subject, clothing, setting, lighting, mood, camera angle

        🚨 TEXT OVERLAY RULES (STRICT!):
        1. If Manual Instructions say 'remove text' or 'no text' → DO NOT include any text in prompt
        2. If original video has NO text overlays → DO NOT include any text in prompt
        3. ONLY if original has text AND Manual Instructions don't forbid it → Include text FROM THE ARTICLE

        When adding text (only if rules 1-3 allow):
        - Extract the actual offer/message from the article content
        - Write the real text, not a description
        - Example: 'small white text 50% OFF in top-right corner' (where 50% OFF is from article)

        ❌ NEVER DO:
        - Add text when original video had no text
        - Add text when Manual Instructions say to remove it
        - Write 'promotional text' or 'text overlay' instead of the actual text
        - Use generic text like 'SALE' if article doesn't mention a sale

        Example with product (no text): 'Photorealistic medium shot, young woman showing her flat stomach with a small plain circular patch (2.5cm diameter, bright orange #FF6600 center, NO text on patch), bright modern bedroom, natural lighting'
        Example WITH text (only if original had text AND article mentions this offer): 'Photorealistic medium shot, small white text 50% OFF in top-right corner, young woman showing clean product, natural lighting'
        Example NO text (original had no text): 'Photorealistic medium shot, young woman confidently walking through bright modern bedroom, natural window lighting, energetic happy mood'>",

        "motion_prompt": "<Animation prompt for Kling/Runway, under 200 chars. Only mention product movement if product_visible=true. Example with product: 'Slow zoom in, woman gently touches the patch on her stomach, soft smile, subtle body movement'. Example without product: 'Slow zoom in, woman walking confidently, soft smile, natural movement'>"
      }}
    }}
  ],

  "product": {{
    "detected": true | false,
    "type": "<product category: patch, cream, toy, supplement, etc.>",
    "visual_description": "<EXTREMELY DETAILED 500+ word description for image generation. MUST include ALL of the following in extreme detail:

    **COLORS (CRITICAL - BE EXTREMELY SPECIFIC):**
    - Primary color with EXACT hex code (e.g., 'bright orange #FF6600', 'pale beige #F5F5DC', 'deep navy blue #000080')
    - Secondary colors with EXACT hex codes (e.g., 'white outer ring #FFFFFF', 'transparent center #FFFFFF with 30% opacity')
    - Color gradients if present (e.g., 'gradient from #FF6600 at center to #FF8833 at edges')
    - Color patterns if present (e.g., 'striped pattern with alternating #FF6600 and #FFFFFF')
    - Color saturation and brightness (e.g., 'highly saturated bright orange', 'muted pale beige')
    - NOT just 'orange' or 'white' - MUST include hex codes and specific shade descriptions!

    **SHAPE AND DIMENSIONS (CRITICAL - BE EXTREMELY SPECIFIC):**
    - Precise shape (circular, rectangular, oval, irregular, etc.) with EXACT measurements
    - Exact dimensions in cm/mm (e.g., '2.5cm diameter circle', '3cm x 4cm rectangle', 'oval 2cm x 3cm')
    - Relative size to body parts or objects (e.g., 'half the size of a credit card', 'covers palm of hand', 'slightly larger than a quarter')
    - Thickness/depth if visible (e.g., '2mm thick', 'flat and flush with skin', 'slightly raised 1mm above skin')
    - Edge details (e.g., 'rounded edges', 'sharp corners', 'beveled edge')

    **MATERIALS AND TEXTURES (CRITICAL - BE EXTREMELY SPECIFIC):**
    - Material type with texture details (e.g., 'smooth adhesive gel center with glossy surface', 'matte white outer ring with paper-like texture', 'transparent film backing with slight texture')
    - Surface finish (e.g., 'glossy reflective surface', 'matte non-reflective finish', 'semi-gloss with subtle sheen')
    - Texture description (e.g., 'smooth', 'textured', 'opaque', 'semi-transparent', 'translucent', 'grainy', 'satin finish')
    - How light interacts (e.g., 'reflective surface creates bright highlight', 'absorbs light creating matte appearance', 'translucent allowing light to pass through')
    - Material properties (e.g., 'flexible and conforms to skin', 'rigid and maintains shape', 'stretchy elastic material')

    **PRODUCT BRANDING (CRITICAL - MUST BE REMOVED!):**
    ⚠️⚠️⚠️ DO NOT include any text, logos, or branding ON THE PRODUCT SURFACE! ⚠️⚠️⚠️
    - When describing the product, OMIT all text/logos that appear on it
    - The product must be shown as CLEAN and PLAIN - no branding
    - Only describe physical characteristics: shape, colors, materials, textures, dimensions
    - Example: Original has "SlimPatch™" logo → Describe as "plain circular patch" (NO text)

    **PACKAGING (if visible):**
    - Package color with hex codes (e.g., 'white box #FFFFFF', 'blue label #0066CC')
    - Package shape and size (e.g., 'rectangular box 5cm x 8cm', 'circular container 4cm diameter')
    - REMOVE package branding/text - describe only physical appearance
    - How product appears in packaging (e.g., 'product visible through transparent window', 'product wrapped in foil')

    **PLACEMENT AND ORIENTATION (CRITICAL - BE EXTREMELY SPECIFIC):**
    - Precise location on body/object (e.g., 'centered on lower abdomen 5cm below navel', 'on right cheekbone 2cm from eye', 'on upper arm 10cm from shoulder')
    - Orientation (e.g., 'horizontal alignment', 'vertical alignment', 'diagonal at 45 degrees', 'rotated 30 degrees clockwise')
    - How it sits on surface (e.g., 'flat against skin with no gaps', 'slightly raised 1mm above skin', 'curved to match body contour', 'adhered flush with no visible edges')
    - Relationship to surrounding elements (e.g., 'surrounded by bare skin', 'partially covered by clothing', 'visible through transparent material')

    **LIGHTING AND SHADOWS (CRITICAL - BE EXTREMELY SPECIFIC):**
    - How light hits the product (e.g., 'soft natural light from above creates subtle highlight on center', 'harsh directional light creates strong contrast', 'diffused light creates even illumination')
    - Shadow details (e.g., 'slight shadow cast on skin below creating depth', 'no shadow, flush with skin', 'soft shadow around edges')
    - Highlights and reflections (e.g., 'bright highlight on glossy center', 'matte surface shows no reflections', 'reflective surface shows window reflection')
    - Light temperature (e.g., 'warm 3000K lighting', 'cool 6000K daylight', 'neutral 5000K lighting')

    **PERSPECTIVE AND CAMERA ANGLE (CRITICAL - BE EXTREMELY SPECIFIC):**
    - Camera angle relative to product (e.g., 'top-down view looking straight down', '45-degree angle from side', 'eye-level view', 'close-up macro view')
    - How product appears from this angle (e.g., 'circular shape appears slightly oval from this angle', 'rectangular shape appears as trapezoid due to perspective')
    - Distance from camera (e.g., 'close-up filling 30% of frame', 'medium shot with product in center', 'wide shot with product as small element')
    - Depth of field (e.g., 'product in sharp focus with blurred background', 'entire scene in focus', 'shallow depth of field with product sharp')

    **FUNCTIONALITY AND USAGE (CRITICAL - BE EXTREMELY SPECIFIC):**
    - How the product functions (e.g., 'adhesive patch that sticks to skin', 'cream that is massaged into skin', 'toy that pet interacts with')
    - How it's being used in the scene (e.g., 'being applied to bare skin', 'already adhered and visible', 'being removed from packaging')
    - Interaction with user/environment (e.g., 'person's hand applying the patch', 'patch visible on person's body', 'product being held near face')
    - State of product (e.g., 'new unused product', 'product in use', 'product showing results')

    **UNIQUE FEATURES AND DISTINGUISHING MARKS:**
    - Any patterns, designs, or distinguishing marks (e.g., 'circular pattern in center', 'logo in corner', 'serial number visible')
    - Any special characteristics that make this product identifiable (e.g., 'unique color combination', 'distinctive shape', 'characteristic texture')
    - Branding elements (e.g., 'brand logo visible', 'product name printed', 'certification mark')

    This description must be so detailed that an AI image generator can recreate the product pixel-perfectly with exact colors, dimensions, materials, and appearance.>",
    "purpose": "<what does this product do?>",
    "usage_method": "<how is it used? step by step>",
    "application_rules": "<CRITICAL: where does it go? bare skin? with pet? etc.>",
    "best_frame_timestamps": ["0:02", "0:08"],
    "product_image_details": "<If product is visible in frames, describe the EXACT visual appearance in the best frame with EXTREME DETAIL:
    - EXACT color composition: Primary color with hex code, secondary colors with hex codes, gradients, patterns, saturation, brightness
    - EXACT shape and proportions: Precise measurements (cm/mm), relative size to known objects, thickness/depth, edge details
    - EXACT text/logos: Transcribe word-for-word, font style, size, position, color with hex code, text effects
    - EXACT material appearance: Material type, surface finish (glossy/matte/semi-gloss), texture (smooth/textured/grainy), opacity (opaque/transparent/translucent), material properties
    - EXACT lighting interaction: How light hits product, shadow details, highlights and reflections, light temperature
    - EXACT position and orientation: Precise location, orientation (horizontal/vertical/diagonal), how it sits on surface, relationship to surrounding elements
    - EXACT perspective: Camera angle, distance, depth of field, how product appears from this angle
    - EXACT functionality: How product functions, how it's being used, interaction with user/environment, state of product
    - Unique features: Patterns, designs, distinguishing marks, branding elements, special characteristics
    This must be detailed enough for pixel-perfect recreation.>"
  }},

  "video_story": {{
    "type": "<transformation/demo/testimonial/lifestyle/tutorial/problem_solution/ugc_review>",
    "one_sentence_summary": "<What happens in this video in one sentence - the complete story>",
    "narrative_arc": "<Describe the complete story arc: beginning → middle → end. What's the journey?>",
    "key_moments": [
      "<Important moment 1: what happens and why it matters>",
      "<Important moment 2: what happens and why it matters>"
    ],
    "scene_connections": "<How do scenes connect? What's the progression? What changes between scenes?>",
    "subject_changes": {{
      "has_visible_change": true | false,
      "start_state": "<How subject looks/feels at START - be specific: overweight, tired, messy, clothing, expression, etc.>",
      "end_state": "<How subject looks/feels at END - be specific: slim, energetic, groomed, clothing, expression, etc.>",
      "subject_appearance_per_scene": {{
        "1": "<EXACT appearance in scene 1: body type, clothing, expression, state>",
        "2": "<EXACT appearance in scene 2: body type, clothing, expression, state>",
        "3": "<EXACT appearance in scene 3: body type, clothing, expression, state>"
      }}
    }},
    "product_role_in_story": "<What's the product's role in the story? When does it appear? How does it connect to the narrative?>"
  }},

  "new_voiceover": {{
    "full_script": "<COMPLETE voiceover script for the new video. Use article content. Match original style and duration. This is the EXACT text for TTS.>",
    "word_count": <number>,
    "style": "<enthusiastic/calm/professional/friendly - match original>"
  }},

  "cta": {{
    "needs_cta": true | false,
    "button_text": "<CTA button text if needed, e.g., 'Shop Now', 'Try Now'>",
    "scene_number": <which scene to add CTA to, usually last>
  }},

  "style": {{
    "aesthetic": "<social_media/cinematic/ugc/professional/minimalist>",
    "lighting": "<natural/studio/dramatic/soft>",
    "mood": "<energetic/calm/luxurious/casual/playful>",
    "style_prefix": "<Concise style description for all prompts, e.g., 'UGC social media style, bright natural lighting, handheld camera feel, casual authentic mood'>"
  }},

  "audio": {{
    "original_has_vo": true | false,
    "original_vo_style": "<enthusiastic/calm/professional/friendly>",
    "original_vo_gender": "<male/female/unknown>",
    "music_mood": "<energetic/calm/uplifting/dramatic/none>"
  }}
}}

⚠️ CRITICAL - READ CAREFULLY:

0. **STORY ACCURACY - MOST IMPORTANT:**
   - ⚠️⚠️⚠️ YOU MUST WATCH THE ORIGINAL VIDEO CAREFULLY FOR EACH SCENE! ⚠️⚠️⚠️
   - Your prompts MUST recreate the ORIGINAL video's visuals EXACTLY - don't invent new visuals!
   - Match the ORIGINAL: camera angles, framing, lighting, subject appearance, setting, mood, colors, textures
   - Understand the COMPLETE STORY before creating prompts - don't just describe individual scenes in isolation
   - Track how scenes CONNECT - what changes between scenes? Why? What's the story progression?
   - Each prompt should reflect the EXACT visual state from the ORIGINAL video at that timestamp
   - If the original shows a transformation (e.g., overweight → slim), your prompts MUST show the CORRECT state for each scene
   - If the original shows different settings/clothing/mood in different scenes, your prompts MUST match this exactly
   - The goal is to recreate the ORIGINAL video's story and visuals, not create a new story

1. **PRODUCT VISIBILITY - SECOND MOST IMPORTANT:**
   - ⚠️ THE PRODUCT DOES NOT APPEAR IN EVERY SCENE!
   - Watch the ORIGINAL video: In which scenes is the product actually VISIBLE?
   - Set product_visible=true ONLY if product is visible in that specific scene
   - Set product_visible=false if product is NOT visible (even if product exists in video)
   - If product_visible=false → DO NOT mention the product in image_prompt at all
   - If product_visible=true → Include product with POSITIVE, SPECIFIC description and EXACT location

2. **IMAGE PROMPTS MUST MATCH ORIGINAL VIDEO EXACTLY:**
   - Watch the ORIGINAL video at this scene's timestamp - what do you ACTUALLY see?
   - Recreate EXACTLY: subject appearance, clothing, setting, lighting, camera angle, mood
   - ⚠️ ONLY include product if product_visible=true for that scene
   - Be SPECIFIC about subject's physical state in EACH scene (if subject changes, show the CORRECT state for THIS scene)
   - Match the ORIGINAL video's visual style - don't invent new visuals
   - Include ALL details you see in the original: colors, textures, expressions, positions

3. **MOTION PROMPTS MUST MATCH ORIGINAL VIDEO EXACTLY:**
   - Watch the ORIGINAL video at this scene's timestamp - what movement do you ACTUALLY see?
   - Describe the EXACT movement: camera movement, subject motion, speed, direction
   - Match the ORIGINAL video's pacing and style
   - Keep under 200 characters
   - Be SPECIFIC about the movement type (zoom, pan, static, tracking, etc.)

4. **NEW VOICEOVER MUST MATCH VIDEO DURATION AND STORY:**
   - Use article content provided
   - Match the style/energy of the original
   - Match the STORY STRUCTURE of the original (hook, problem, solution, etc.) and keep the narrative CAPTIVATING—every line should pull the viewer in.
   - Calculate approximate word count based on duration (2-3 words per second)

5. **PRODUCT LOGIC IS CRITICAL:**
   - Patches/stickers → BARE SKIN only (not on clothes!) - describe EXACT location
   - Pet products → Pet must be visible and interacting
   - Creams → On visible skin - describe EXACT location

6. **TRACK SUBJECT CHANGES ACCURATELY:**
   - If someone is overweight in scene 1 and slim in scene 5 → reflect this EXACTLY in prompts!
   - Each scene's image_prompt should show subject in the CORRECT state for that scene
   - Use subject_appearance_per_scene to track changes accurately
   - Match the ORIGINAL video's subject appearance at each timestamp

Return valid JSON only.
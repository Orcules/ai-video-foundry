export const stages = [
  {
    id: 0, num: "0", title: "Generate/Describe Influencer", service: "Kie.ai / Gemini AI",
    category: "cat-analyze",
    description: `The pipeline needs a consistent <strong>influencer character</strong> who appears throughout the video. Two paths:

<strong>Path A &mdash; No character photo provided:</strong> Kie.ai generates an influencer portrait based on <code>gender</code>, <code>country</code>, and visual style. Then Gemini describes the generated image for consistent appearance across scenes.

<strong>Path B &mdash; Character photo provided:</strong> Gemini AI analyzes the photo and writes a text description of the person's appearance.

<strong>Path C &mdash; Text description provided (API only):</strong> Used directly, bypassing both generation and analysis.

The resulting description is injected into every scene prompt so the influencer looks consistent.`,
    inputs: [
      '<span class="val">character_url</span> &mdash; URL to an existing person photo (optional)',
      '<span class="val">character_description</span> &mdash; text description (optional, API only)',
      '<span class="val">gender</span> &mdash; "m" or "f" (selects appearance + voice)',
      '<span class="val">country</span> &mdash; cultural appearance hints (optional)',
    ],
    outputs: [
      '<span class="val">influencer_image_url</span> &mdash; portrait URL (generated or provided)',
      '<span class="val">influencer_description</span> &mdash; text description for scene prompts',
    ],
    sheets: {
      input: {
        columns: ["Character", "Gender", "Country"],
        how: `Reads <code>Character</code> column for a photo URL. Reads <code>Gender</code> column (<code>m</code>/<code>f</code>) for appearance and voice selection. If no character URL, generates one via Kie.ai using gender + country. Text descriptions are <strong>not supported</strong> in the Sheets implementation.`,
      },
      output: {
        columns: [],
        how: `Nothing is written to the sheet. The influencer image and description are kept <strong>in memory only</strong> and passed to Steps 3 and 4-7.`,
      },
    },
    supabase: {
      input: {
        fields: ["params.character_url", "params.character_description", "params.gender", "params.country"],
        how: `Reads <code>character_description</code> (bypasses all AI), <code>character_url</code> (Gemini describes), or falls back to generating via Kie.ai using <code>gender</code> + <code>country</code>.`,
      },
      output: {
        fields: ["intermediates.influencer_image", "intermediates.influencer_description"],
        how: `Saves the portrait URL to <code>intermediates.influencer_image</code> and the text description to <code>intermediates.influencer_description</code>. Updates <code>current_step = 'generating_influencer'</code>.`,
      },
      progress: "2%",
    },
    example: `<span class="comment">// Path A: Generate influencer (no photo provided)</span>
<span class="key">gender:</span> <span class="str">"f"</span>  <span class="key">country:</span> <span class="str">"Italy"</span>
<span class="comment">// Kie.ai generates portrait</span>
influencer_image = "https://storage.googleapis.com/.../influencer_gen.png"
<span class="comment">// Gemini describes the generated image</span>
influencer_description = <span class="str">"Mediterranean woman, ~25, long dark wavy
hair, warm olive skin, casual summer style, bright confident smile"</span>

<span class="comment">// Path B: Describe existing character photo</span>
<span class="key">character_url:</span> <span class="str">"https://example.com/person.jpg"</span>
<span class="comment">// Gemini output</span>
influencer_description = <span class="str">"Blonde woman, ~30, athletic build,
bright smile, wearing denim jacket over white tee"</span>`,
    notes: `Unlike the Product pipeline (where character is optional), UGC always needs an influencer. If neither photo nor description is provided, an influencer is auto-generated based on gender and country.`
  },
  {
    id: 1, num: "1", title: "Parse Prompt (TEXT 1-4)", service: "Gemini AI",
    category: "cat-parse",
    description: `Same core logic as the Product pipeline &mdash; Gemini analyzes the raw prompt into <strong>4 structured text fields</strong>. However, the UGC prompt template emphasizes <strong>authentic, first-person content</strong> rather than polished product marketing.

&bull; <strong>TEXT 1</strong> = What is being promoted (product, place, experience)
&bull; <strong>TEXT 2</strong> = Goal + key selling points (authentic testimonial angle)
&bull; <strong>TEXT 3</strong> = Visual style and mood (phone-shot, casual, intimate)
&bull; <strong>TEXT 4</strong> = Scene structure (first-person perspective)`,
    inputs: [
      '<span class="val">prompt</span> &mdash; raw description from the user',
      '<span class="val">reference_image_urls[]</span> &mdash; optional reference images for visual context',
    ],
    outputs: [
      '<span class="val">text_1</span> &mdash; subject identity',
      '<span class="val">text_2</span> &mdash; video goal + selling points',
      '<span class="val">text_3</span> &mdash; visual style and mood',
      '<span class="val">text_4</span> &mdash; suggested scene structure',
    ],
    sheets: {
      input: {
        columns: ["Prompt", "image 1", "image 2", "image 3", "image 4", "image 5", "TEXT 1", "TEXT 2", "TEXT 3", "TEXT 4"],
        how: `Reads <code>Prompt</code> column and <code>image 1</code>&ndash;<code>image 5</code> for reference photos. Checks <code>TEXT 1</code>&ndash;<code>TEXT 4</code> &mdash; if they already have values, <strong>skips parsing</strong>.`,
      },
      output: {
        columns: ["TEXT 1", "TEXT 2", "TEXT 3", "TEXT 4"],
        how: `Writes each parsed text to its column via Google Sheets API.`,
      },
    },
    supabase: {
      input: {
        fields: ["params.prompt", "params.reference_image_urls"],
        how: `Reads <code>prompt</code> (required) and <code>reference_image_urls</code> (optional array) from <code>input_params</code>. Always parses fresh.`,
      },
      output: {
        fields: ["intermediates.parsed_texts"],
        how: `Saves all 4 texts as a JSON object into <code>intermediates.parsed_texts</code>.`,
      },
      progress: "5%",
    },
    example: `<span class="comment">// Input</span>
<span class="key">prompt:</span> <span class="str">"Amazing Italian restaurant in Rome, incredible
pasta and wine, cozy atmosphere, must-visit spot"</span>

<span class="comment">// Gemini parsed output (UGC tone)</span>
<span class="key">TEXT 1:</span> <span class="str">"Trattoria da Mario, authentic Roman restaurant"</span>
<span class="key">TEXT 2:</span> <span class="str">"Convince viewers to visit, highlight food + ambiance"</span>
<span class="key">TEXT 3:</span> <span class="str">"Warm, intimate, phone-shot aesthetic, golden hour"</span>
<span class="key">TEXT 4:</span> <span class="str">"Scene 1: influencer entering restaurant,
Scene 2: close-up of pasta being served,
Scene 3: wine toast with friends, Scene 4: dessert,
Scene 5: logo + CTA"</span>`,
    notes: `The UGC prompt template differs from Product in tone: it emphasizes first-person language, authentic feel, and casual phone-shot aesthetics.`
  },
  {
    id: 2, num: "2", title: "Calculate Scene Count", service: "Config (local math)",
    category: "cat-scene",
    description: `Determines how many AI-generated scenes are needed based on the target duration minus time allocated for user-provided asset clips (3 seconds each).

<strong>Formula:</strong>
<code>asset_time = num_assets &times; 3s</code>
<code>remaining = duration &minus; asset_time</code>
<code>generated_scenes = remaining / 4s</code>

This step is unique to UGC &mdash; the Product pipeline has a fixed scene count decided by Gemini.`,
    inputs: [
      '<span class="val">duration</span> &mdash; target video length in seconds',
      '<span class="val">num_assets</span> &mdash; count of user-provided asset clips',
    ],
    outputs: [
      '<span class="val">scene_count</span> &mdash; number of AI-generated scenes to create',
    ],
    sheets: {
      input: {
        columns: ["Duration"],
        how: `Reads <code>Duration</code> column for target video length. Asset count is determined by how many asset URL columns have values.`,
      },
      output: {
        columns: [],
        how: `Nothing written to sheet. Scene count stays in memory for Step 3.`,
      },
    },
    supabase: {
      input: {
        fields: ["params.duration", "params.asset_urls"],
        how: `Reads <code>duration</code> (default 30) and counts non-empty entries in <code>asset_urls</code> array.`,
      },
      output: {
        fields: ["intermediates.scene_count"],
        how: `Saves the calculated scene count to <code>intermediates.scene_count</code>.`,
      },
      progress: "8%",
    },
    example: `<span class="comment">// 30s video with 2 user assets</span>
<span class="key">duration:</span> 30
<span class="key">num_assets:</span> 2

<span class="comment">// Calculation</span>
asset_time = 2 \u00d7 3s = 6s
remaining = 30s \u2212 6s = 24s
scene_count = 24 / 4 = 6  <span class="comment">// 6 AI-generated scenes</span>

<span class="comment">// Final clip order: 6 body scenes + 2 assets + 1 CTA = 9 clips total</span>`,
    notes: `This is a UGC-specific step. Product pipeline lets Gemini decide scene count. UGC calculates it deterministically to account for interleaved user assets.`
  },
  {
    id: "2.5", num: "2.5", title: "Analyze Reference Images", service: "Gemini AI",
    category: "cat-analyze", optional: true,
    description: `If the user provided reference images (photos of real locations, products, settings), Gemini analyzes each one and writes a <strong>text description</strong>. These descriptions are used in Step 3 to match generated scenes to real-world locations and settings.

This helps create more authentic-looking scenes that match the user's actual environment.`,
    inputs: [
      '<span class="val">reference_image_urls[]</span> &mdash; photos of real locations/settings',
    ],
    outputs: [
      '<span class="val">reference_analyses[]</span> &mdash; text description per image',
    ],
    sheets: {
      input: {
        columns: ["image 1", "image 2", "image 3", "image 4", "image 5"],
        how: `Reads reference image URLs from <code>image 1</code> through <code>image 5</code>. If all empty, step is skipped.`,
      },
      output: {
        columns: [],
        how: `Nothing written to sheet. Analyses stay in memory for Step 3.`,
      },
    },
    supabase: {
      input: {
        fields: ["params.reference_image_urls"],
        how: `Reads <code>reference_image_urls</code> array from <code>input_params</code>. If empty, step is skipped.`,
      },
      output: {
        fields: ["intermediates.reference_analyses"],
        how: `Saves array of text descriptions to <code>intermediates.reference_analyses</code>.`,
      },
      progress: "12%",
    },
    example: `<span class="comment">// Input: 2 reference photos</span>
reference_image_urls = [
  "https://example.com/beach-sunset.jpg",
  "https://example.com/restaurant-interior.jpg"
]

<span class="comment">// Gemini analysis output</span>
reference_analyses = [
  <span class="str">"Golden sunset over Mediterranean beach with rocky cliffs,
warm orange and pink sky, calm turquoise water"</span>,
  <span class="str">"Cozy Italian restaurant with exposed brick walls,
candlelight, rustic wooden tables, warm ambient lighting"</span>
]`,
    notes: `Unlike the Product pipeline's Step 2.5 (video reference analysis), UGC analyzes still images to match scene aesthetics to real locations. Each analysis is paired with its scene via <code>reference_image_index</code> in Step 3.`
  },
  {
    id: 3, num: "3", title: "Generate Scene Prompts", service: "Gemini AI",
    category: "cat-scene",
    description: `Gemini creates detailed instructions for each scene, incorporating the <strong>influencer description</strong> and <strong>reference image analyses</strong>. Each scene specifies:

&bull; <strong>first_prompt</strong> (image prompt) &mdash; what to draw
&bull; <strong>second_prompt</strong> (motion prompt) &mdash; how to animate it
&bull; <strong>shows_influencer</strong> &mdash; whether the influencer appears in this scene
&bull; <strong>reference_image_index</strong> &mdash; which reference image to match (if any)

The scene count comes from Step 2. Scenes are generated with a first-person, authentic UGC tone.`,
    inputs: [
      '<span class="val">text_1-4</span> &mdash; parsed texts from Step 1',
      '<span class="val">influencer_description</span> &mdash; from Step 0',
      '<span class="val">reference_analyses[]</span> &mdash; from Step 2.5 (optional)',
      '<span class="val">scene_count</span> &mdash; from Step 2',
      '<span class="val">logo_url, slogan_text</span> &mdash; for CTA scene',
    ],
    outputs: [
      '<span class="val">scenes[]</span> &mdash; array of scene objects with image + motion prompts',
      '<span class="val">music_style</span> &mdash; suggested background music mood',
    ],
    sheets: {
      input: {
        columns: [],
        how: `No additional columns read. All inputs come from memory (Steps 0-2.5). Logo and slogan read earlier by the orchestrator.`,
      },
      output: {
        columns: ["Scene 1 - First prompt", "Scene 1 - Second prompt", "...up to Scene 20"],
        how: `Writes each scene's <code>first_prompt</code> and <code>second_prompt</code> to their respective columns.`,
      },
    },
    supabase: {
      input: {
        fields: ["params.sound_sync_method"],
        how: `Reads <code>sound_sync_method</code> (default "beat_sync"). All other inputs come from memory. When beat_sync + VO already generated in Step 3.5, <code>assign_beat_sync_durations()</code> runs after.`,
      },
      output: {
        fields: ["intermediates.scene_prompts"],
        how: `Saves the scenes array (with image/motion prompts, influencer flags, reference indices, and beat_sync durations if applicable) to <code>intermediates.scene_prompts</code>.`,
      },
      progress: "22%",
    },
    example: `<span class="comment">// Gemini output: 6 scenes for a UGC restaurant video</span>
[
  {
    <span class="key">"scene_num"</span>: 1,
    <span class="key">"first_prompt"</span>: <span class="str">"Stylish Mediterranean woman walking into
a cozy Italian restaurant, warm ambient lighting, phone-shot"</span>,
    <span class="key">"second_prompt"</span>: <span class="str">"Camera follows her as she enters, slight
handheld shake for authenticity"</span>,
    <span class="key">"shows_influencer"</span>: true,
    <span class="key">"reference_image_index"</span>: 1
  },
  ...
  {
    <span class="key">"scene_num"</span>: 7, <span class="key">"narrative_role"</span>: <span class="str">"cta"</span>,
    <span class="key">"first_prompt"</span>: <span class="str">"Restaurant logo centered, 'Book Your Table'
text below in elegant script"</span>,
    <span class="key">"shows_influencer"</span>: false
  }
]`,
    notes: `UGC scenes always include the influencer description for consistency. The CTA scene (last) combines logo + slogan. Reference image indices let the image generator use the correct real-world photo as context for each scene.`
  },
  {
    id: "3.5", num: "3.5", title: "Generate VO (beat_sync)", service: "Gemini + ElevenLabs",
    category: "cat-vo", optional: true,
    description: `<strong>Only runs when <code>sound_sync_method=beat_sync</code></strong> (the default). Generates the voice-over <em>before</em> the parallel asset phase so word timestamps can drive exact scene durations.

<strong>Step A &mdash; Write the script:</strong> Gemini writes a first-person, authentic testimonial VO script.
<strong>Step B &mdash; Record the audio:</strong> ElevenLabs converts to speech with <strong>expressive settings</strong>: stability 0.4, style 0.55, sentence pauses via <code>&lt;break time="0.5s" /&gt;</code>.
<strong>Step C &mdash; Assign durations:</strong> <code>assign_beat_sync_durations()</code> maps VO word timestamps to exact per-scene durations.

Voice is selected by <code>voice_id</code> (if set) or randomly by <code>gender</code> + <code>language</code>.

When <code>sound_sync_method=none</code>, this step is <strong>skipped</strong> and VO is generated in the parallel phase (Steps 4-7) instead.`,
    inputs: [
      '<span class="val">text_1-3</span> &mdash; parsed texts from Step 1',
      '<span class="val">prompt</span> &mdash; original user prompt',
      '<span class="val">duration</span> &mdash; target video length',
      '<span class="val">gender</span> &mdash; for voice selection',
      '<span class="val">language, country</span> &mdash; for accent/style',
    ],
    outputs: [
      '<span class="val">vo_script</span> &mdash; first-person VO text',
      '<span class="val">vo_audio_url</span> &mdash; MP3 URL',
      '<span class="val">vo_word_timestamps[]</span> &mdash; per-word timing from ElevenLabs',
    ],
    sheets: {
      input: {
        columns: ["VO", "Voice id", "Gender", "Language", "Country", "Duration"],
        how: `Reads <code>VO</code> column (if filled, uses existing script). Reads <code>Voice id</code>, <code>Gender</code>, <code>Language</code>, <code>Country</code>, <code>Duration</code> for voice and timing configuration.`,
      },
      output: {
        columns: ["VO", "New Voice"],
        how: `Writes script to <code>VO</code> (if empty) and audio URL to <code>New Voice</code>. Timestamps stay in memory.`,
      },
    },
    supabase: {
      input: {
        fields: ["params.voice_id", "params.gender", "params.language", "params.country", "params.duration"],
        how: `Reads voice/timing params from <code>input_params</code>. Uses parsed texts from memory.`,
      },
      output: {
        fields: ["intermediates.vo_script", "intermediates.vo_audio_url", "intermediates.vo_word_timestamps"],
        how: `Saves script, audio URL, and word timestamps. Beat-sync durations are assigned to scenes.`,
      },
      progress: "25%",
    },
    example: `<span class="comment">// Gemini writes first-person UGC script</span>
<span class="str">"You HAVE to try this place. The pasta was incredible.
Like, I'm talking handmade, fresh, melt-in-your-mouth
good. And the wine? Chef's kiss. Seriously, book a
table before everyone finds out about it."</span>

<span class="comment">// ElevenLabs with expressive settings</span>
<span class="comment">// stability: 0.4, style: 0.55</span>
<span class="comment">// Sentence pauses: &lt;break time="0.5s" /&gt; between sentences</span>
<span class="key">vo_audio_url:</span> <span class="str">"https://storage.googleapis.com/.../ugc_vo.mp3"</span>
<span class="key">vo_duration:</span> 12.8  <span class="comment">// seconds</span>`,
    notes: `UGC VO uses expressive ElevenLabs settings for authenticity: lower stability (0.4 vs product's higher value) and sentence pauses. This makes the VO sound more like a real person talking, not a polished narrator. Only runs in beat_sync mode (default).`
  },
  {
    id: "4-7", num: "4-7", title: "Parallel Asset Generation",
    service: "Kie.ai + Veo3/Kling/Runway + Suno", category: "cat-parallel", isParallel: true,
    description: `Four independent tracks run <strong>simultaneously</strong>:

<strong>Track 1 &mdash; Per scene (image + video):</strong> Generate image (with influencer + location refs) &rarr; animate to video &rarr; trim first 1s. When <code>quality_check=true</code>, Gemini scores each image. When <code>beat_sync</code>, videos generated at <code>overgenerate_duration</code>.

<strong>Track 2 &mdash; Per asset clip:</strong> If image &rarr; create zoom-in video (Veo3, 3s max). If video &rarr; trim to 3s max.

<strong>Track 3 &mdash; Background Music:</strong> Gemini describes mood &rarr; Suno generates instrumental.

<strong>Track 4 &mdash; Voice-Over</strong> (only if <code>sound_sync_method=none</code>): Gemini writes first-person script &rarr; ElevenLabs TTS with expressive settings. <em>Skipped if beat_sync, since VO was generated in Step 3.5.</em>`,
    inputs: [
      '<span class="val">scenes[]</span> &mdash; scene prompts from Step 3',
      '<span class="val">influencer_image_url</span> &mdash; reference portrait from Step 0',
      '<span class="val">reference_image_urls[]</span> &mdash; real-world location photos',
      '<span class="val">visual_style</span> &mdash; "Auto", etc.',
      '<span class="val">animation_model</span> &mdash; "runway" (default for UGC), "kling", "google"',
      '<span class="val">asset_urls[]</span> &mdash; user-provided photos/videos to include',
    ],
    outputs: [
      '<span class="val">scene_images[]</span> &mdash; URL per scene image',
      '<span class="val">scene_videos[]</span> &mdash; URL per scene video',
      '<span class="val">asset_videos[]</span> &mdash; processed asset clip URLs (zoom or trimmed)',
      '<span class="val">music_url</span> &mdash; background music MP3',
    ],
    sheets: {
      input: {
        columns: ["Animation model", "Style"],
        how: `Reads <code>Animation model</code> (default "runway" for UGC) and <code>Style</code>. Asset URLs come from asset columns. Scene prompts and influencer data from memory.`,
      },
      output: {
        columns: ["Scene 1 - new image", "Scene 1 - new video", "...up to Scene 20", "New music"],
        how: `Each scene writes image + video URLs to their columns. Music URL to <code>New music</code>. Assets appear in the spreadsheet as generated.`,
      },
    },
    supabase: {
      input: {
        fields: ["params.animation_model", "params.style", "params.quality_check", "params.asset_urls"],
        how: `Reads <code>animation_model</code> (default "runway"), <code>style</code>, <code>quality_check</code>, and <code>asset_urls</code>. Scene prompts, influencer image, and reference images from memory.`,
      },
      output: {
        fields: ["intermediates.scene_images", "intermediates.scene_videos", "intermediates.asset_videos", "intermediates.music_url"],
        how: `Saves scene images, scene videos, processed asset videos, and music URL. Per-scene SSE progress: image 30&rarr;50%, video 50&rarr;70%.`,
      },
      progress: "30% &rarr; 70%",
    },
    example: `<span class="comment">// UGC parallel execution</span>
<span class="key">Track 1 - Scenes (6 in parallel):</span>
  Scene 1: [Image+influencer 15s] \u2192 [Runway 45s] \u2192 [Trim 1s] \u2713
  Scene 2: [Image+location 15s] \u2192 [Runway 45s] \u2192 [Trim 1s] \u2713
  ...

<span class="key">Track 2 - Assets (2 user photos):</span>
  Asset 1: [beach.jpg \u2192 Veo3 zoom 3s] \u2713
  Asset 2: [food.jpg \u2192 Veo3 zoom 3s] \u2713

<span class="key">Track 3 - Music:</span>
  [Suno ~30s] \u2713

<span class="key">Track 4 - VO (only if not beat_sync):</span>
  [Gemini script] \u2192 [ElevenLabs TTS] \u2713`,
    notes: `UGC default animation model is "runway" (not "google" like Product). Asset clips are inserted as-is: images get a slow zoom-in effect (Veo3, 3s max), videos are trimmed to 3s max. The influencer portrait is used as a reference image for scenes with <code>shows_influencer: true</code>.`
  },
  {
    id: "7.5", num: "7.5", title: "Beat-Sync Trim", service: "Rendi / Local FFmpeg",
    category: "cat-combine", optional: true,
    description: `<strong>Only runs when <code>sound_sync_method=beat_sync</code></strong> (the default). Each scene video was over-generated at <code>overgenerate_duration</code> (integer ceil). This step trims each to its <strong>exact_duration</strong> (float seconds) for frame-perfect VO sync.

Runs in parallel across all scenes. If trimming fails for a scene, the original video is kept.`,
    inputs: [
      '<span class="val">scene_videos[]</span> &mdash; over-generated videos from Steps 4-7',
      '<span class="val">scenes[].exact_duration</span> &mdash; float seconds per scene',
    ],
    outputs: [
      '<span class="val">trimmed_scene_videos[]</span> &mdash; trimmed video URLs',
    ],
    sheets: {
      input: {
        columns: [],
        how: `No columns read. All data from memory.`,
      },
      output: {
        columns: [],
        how: `No columns written. Trimmed URLs replace originals in memory.`,
      },
    },
    supabase: {
      input: {
        fields: ["params.sound_sync_method"],
        how: `Only runs when <code>sound_sync_method=beat_sync</code>. Videos and durations from memory.`,
      },
      output: {
        fields: ["intermediates.trimmed_scene_videos"],
        how: `Saves trimmed video URL list to <code>intermediates.trimmed_scene_videos</code>.`,
      },
      progress: "72% &rarr; 74%",
    },
    example: `<span class="comment">// Trim each scene to exact VO-synced duration</span>
Scene 1: exact=4.20s, overgen=5s  <span class="comment">// trim 0.80s</span>
Scene 2: exact=3.60s, overgen=4s  <span class="comment">// trim 0.40s</span>
Scene 3: exact=4.00s, overgen=4s  <span class="comment">// no trim needed</span>
...`,
    notes: `Same logic as Product pipeline's Step 7.5. Skipped when sound_sync_method=none.`
  },
  {
    id: 8, num: "8", title: "Concatenate + Mix Audio", service: "Rendi (FFmpeg cloud)",
    category: "cat-combine",
    description: `Assembles all clips into the final video with audio:

<strong>8a &mdash; Interleave clips:</strong> Body scenes are interleaved with asset clips (assets placed every 2 body scenes). The CTA scene (logo + slogan) is appended at the end.

<strong>8b &mdash; Dissolve transitions:</strong> <strong>0.4s dissolve</strong> between every clip via Rendi <code>xfade</code>. This is a key UGC aesthetic (Product uses no dissolve).

<strong>8c &mdash; Mix audio:</strong> VO at full volume (1.0) + music at 20% (0.2).

If Rendi fails, local FFmpeg fallback is attempted.`,
    inputs: [
      '<span class="val">scene_videos[]</span> &mdash; body scene videos (trimmed if beat_sync)',
      '<span class="val">asset_videos[]</span> &mdash; processed user asset clips',
      '<span class="val">vo_audio_url</span> &mdash; voice over MP3',
      '<span class="val">music_url</span> &mdash; background music MP3',
    ],
    outputs: [
      '<span class="val">concat_video_url</span> &mdash; video-only concatenation',
      '<span class="val">rendi_scene_voice_url</span> &mdash; video + VO + music',
    ],
    sheets: {
      input: {
        columns: [],
        how: `No columns read. All URLs from memory.`,
      },
      output: {
        columns: ["RENDI Scene", "RENDI Scene & Voice"],
        how: `Writes video-only URL to <code>RENDI Scene</code> and audio-mixed URL to <code>RENDI Scene & Voice</code>.`,
      },
    },
    supabase: {
      input: {
        fields: ["params.dissolve_seconds"],
        how: `Reads <code>dissolve_seconds</code> (default 0.4 for UGC). All clip URLs from memory.`,
      },
      output: {
        fields: ["intermediates.concat_url", "intermediates.rendi_scene_voice_url"],
        how: `Saves concatenated and audio-mixed video URLs.`,
      },
      progress: "80%",
    },
    example: `<span class="comment">// UGC clip ordering with assets interleaved</span>
clips = [
  body_1, body_2,    <span class="comment">// 2 body scenes</span>
  asset_1,           <span class="comment">// user asset inserted</span>
  body_3, body_4,    <span class="comment">// 2 more body scenes</span>
  asset_2,           <span class="comment">// user asset inserted</span>
  body_5, body_6,    <span class="comment">// remaining body scenes</span>
  cta_scene          <span class="comment">// logo + slogan at the end</span>
]

<span class="comment">// 0.4s dissolve transitions between ALL clips</span>
<span class="comment">// VO (vol=1.0) + music (vol=0.2) mixed on top</span>`,
    notes: `Key UGC difference: 0.4s dissolve transitions between all clips (Product uses hard cuts). Asset clips are interleaved every 2 body scenes. CTA scene is always last.`
  },
  {
    id: 9, num: "9", title: "Add Subtitles", service: "ZapCap API",
    category: "cat-subtitle", optional: true,
    description: `Sends the video + ElevenLabs word segments to ZapCap for subtitle burn-in. Uses "Bring Your Own Transcript" (BYOT) for exact word-level timing.

If ZapCap fails, the pipeline continues without subtitles.`,
    inputs: [
      '<span class="val">rendi_scene_voice_url</span> &mdash; assembled video with audio',
      '<span class="val">subtitle_language</span> &mdash; language code',
      '<span class="val">vo_word_segments[]</span> &mdash; word timestamps from ElevenLabs',
    ],
    outputs: [
      '<span class="val">subtitled_video_url</span> &mdash; video with burned-in subtitles',
    ],
    sheets: {
      input: {
        columns: ["Add subtitles", "Language"],
        how: `Reads <code>Add subtitles</code> ("yes" to enable) and <code>Language</code> for subtitle language.`,
      },
      output: {
        columns: ["Subtitled Video"],
        how: `Writes subtitled video URL to <code>Subtitled Video</code> column.`,
      },
    },
    supabase: {
      input: {
        fields: ["params.add_subtitles", "params.language"],
        how: `Reads <code>add_subtitles</code> (default true) and <code>language</code>. Word segments from memory.`,
      },
      output: {
        fields: ["intermediates.subtitled_url"],
        how: `Saves subtitled video URL.`,
      },
      progress: "90%",
    },
    example: `<span class="comment">// Same as Product pipeline</span>
<span class="key">video:</span> <span class="str">"https://storage.googleapis.com/.../ugc_final.mp4"</span>
<span class="key">vo_word_segments:</span> [
  { text: "You",   start: 0.00, end: 0.15 },
  { text: "HAVE",  start: 0.18, end: 0.42 },
  { text: "to",    start: 0.44, end: 0.52 },
  { text: "try",   start: 0.55, end: 0.78 },
  ...
]`,
    notes: `Same ZapCap integration as Product. Template ID configurable. Fails gracefully.`
  },
  {
    id: 10, num: "10", title: "Upload to Mux CDN", service: "Mux CDN",
    category: "cat-upload",
    description: `Uploads the final video to <strong>Mux CDN</strong> for HLS streaming and MP4 download. Same process as Product pipeline. Job is marked as <strong>completed</strong> after upload.`,
    inputs: [
      '<span class="val">subtitled_video_url</span> (or rendi_scene_voice_url if no subtitles)',
      '<span class="val">vo_audio_url</span> &mdash; voice over audio',
      '<span class="val">music_url</span> &mdash; background music',
    ],
    outputs: [
      '<span class="val">final_stream_url</span> &mdash; HLS stream URL',
      '<span class="val">final_mp4_url</span> &mdash; direct MP4 download URL',
    ],
    sheets: {
      input: {
        columns: [],
        how: `No columns read. All URLs from memory.`,
      },
      output: {
        columns: ["Final Video"],
        how: `Writes final permanent URL to <code>Final Video</code> column.`,
      },
    },
    supabase: {
      input: {
        fields: [],
        how: `No additional reads. Video URL from memory.`,
      },
      output: {
        fields: ["output.final_stream_url", "output.final_mp4_url", "output.final_playback_id", "output.final_asset_id", "output.vo_audio_url", "output.music_url", "output.concat_url"],
        how: `Saves all permanent URLs to the <code>output</code> JSON field. Sets <code>status = 'completed'</code>, <code>progress = 100</code>.`,
      },
      progress: "95% &rarr; 100%",
    },
    example: `<span class="comment">// Upload to Mux CDN</span>
<span class="key">final_stream_url:</span> <span class="str">"https://stream.mux.com/abc123.m3u8"</span>
<span class="key">final_mp4_url:</span> <span class="str">"https://stream.mux.com/abc123/high.mp4"</span>
<span class="key">final_playback_id:</span> <span class="str">"abc123"</span>
<span class="key">final_asset_id:</span> <span class="str">"xyz789"</span>

<span class="comment">// Supabase: status = "completed", progress = 100%</span>`,
    notes: `Same Mux integration as Product pipeline. Output includes both HLS stream URL and direct MP4 download URL.`
  }
];

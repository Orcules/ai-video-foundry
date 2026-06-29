export const SVG_IN = '<svg width="12" height="12" viewBox="0 0 16 16" fill="#58a6ff"><path d="M8 0a8 8 0 110 16A8 8 0 018 0zm.75 4.75a.75.75 0 00-1.5 0v2.5h-2.5a.75.75 0 000 1.5h2.5v2.5a.75.75 0 001.5 0v-2.5h2.5a.75.75 0 000-1.5h-2.5v-2.5z"/></svg>';
export const SVG_OUT = '<svg width="12" height="12" viewBox="0 0 16 16" fill="#3fb950"><path d="M13.78 4.22a.75.75 0 010 1.06l-7.25 7.25a.75.75 0 01-1.06 0L2.22 9.28a.75.75 0 011.06-1.06L6 10.94l6.72-6.72a.75.75 0 011.06 0z"/></svg>';

export const stages = [
  {
    id: 0, num: "0", title: "Describe Character", service: "Gemini AI",
    category: "cat-analyze", optional: true,
    description: `If a person is provided (e.g. a spokesperson, mascot, or model), the pipeline needs a <strong>text description</strong> of that person's appearance. This description is later injected into every scene prompt so the character looks consistent across the entire video.<br><br><strong>Two input modes (API only):</strong><br>1. <strong>Image URL</strong> (<code>character_url</code>) &mdash; Gemini AI analyzes the photo and generates a text description.<br>2. <strong>Text description</strong> (<code>character_description</code>) &mdash; Used directly, <strong>bypassing Gemini AI entirely</strong>.<br><br>If both are provided, the text description takes priority. If neither is provided, this step is skipped.`,
    inputs: [
      '<span class="val">character_url</span> &mdash; URL to a photo of a person (optional)',
      '<span class="val">character_description</span> &mdash; text description of the person (optional, API only, bypasses Gemini)',
    ],
    outputs: [
      '<span class="val">character_description</span> &mdash; e.g. "Athletic man, ~30, short brown hair, blue jacket, hiking in mountains"',
    ],
    sheets: {
      input: {
        columns: ["Character"],
        how: "Reads the <code>Character</code> column from the row. Expects an <code>http://</code>, <code>https://</code>, or <code>gs://</code> URL to an image. <strong>Text descriptions are not supported</strong> in the Google Sheets implementation &mdash; only image URLs. If the column is empty, this step is skipped.",
      },
      output: {
        columns: [],
        how: "Nothing is written to the sheet. The description is kept <strong>in memory only</strong> and passed internally to Step 3 (scene prompt generation).",
      },
    },
    supabase: {
      input: {
        fields: ["params.character_url", "params.character_description"],
        how: "Reads <code>character_description</code> (text) and <code>character_url</code> (image URL) from <code>input_params</code>. If <code>character_description</code> is provided, it is used directly and <strong>Gemini AI is bypassed</strong>. Otherwise, if <code>character_url</code> is provided, Gemini analyzes the image.",
      },
      output: {
        fields: [],
        how: "Nothing is saved to the database. The description stays in memory. Updates <code>current_step = 'describing_character'</code>.",
      },
      progress: "2%",
    },
    example: `<span class="comment">// Option A: Image URL \u2192 Gemini AI generates description</span>
character_url = "https://storage.googleapis.com/automatiq/characters/hiker.jpg"
<span class="comment">// Gemini output (kept in memory)</span>
character_description = "Athletic man in his early 30s with short
brown hair and light stubble. Wearing a fitted blue hiking jacket."

<span class="comment">// Option B (API only): Text description \u2192 bypasses Gemini AI</span>
character_description = "Athletic man in his early 30s with short
brown hair and light stubble. Wearing a fitted blue hiking jacket
over a grey t-shirt. Lean build, confident posture, bright green
eyes. Currently outdoors in a mountain setting."

<span class="comment">// This text is injected into scene prompts in Step 3</span>`,
    notes: `Only runs when a character image is provided. The description is purely internal \u2014 neither implementation writes it to a column or field. It's passed in-memory to scene prompt generation (Step 3).`
  },
  {
    id: 1, num: "1", title: "Parse Prompt into TEXT 1-4", service: "Gemini AI",
    category: "cat-parse",
    description: `Gemini reads the raw product prompt and analyzes it into <strong>4 structured text fields</strong>:

&bull; <strong>TEXT 1</strong> = What the product is (name + short description)
&bull; <strong>TEXT 2</strong> = Goal of the video (convince, inform, entertain) + selling points
&bull; <strong>TEXT 3</strong> = Visual style and mood (cinematic, minimal, energetic, etc.)
&bull; <strong>TEXT 4</strong> = Suggested scene structure (scene-by-scene outline)

If the texts already exist (were filled in manually or by a previous run), <strong>this step is skipped</strong> to save time and API calls.`,
    inputs: [
      '<span class="val">prompt</span> &mdash; raw product description from the user',
      '<span class="val">image_urls[]</span> &mdash; optional product reference images (gives Gemini visual context)',
    ],
    outputs: [
      '<span class="val">text_1</span> &mdash; product identity',
      '<span class="val">text_2</span> &mdash; video goal + selling points',
      '<span class="val">text_3</span> &mdash; visual style and mood',
      '<span class="val">text_4</span> &mdash; suggested scene structure',
    ],
    sheets: {
      input: {
        columns: ["Prompt", "image 1", "image 2", "image 3", "image 4", "image 5", "TEXT 1", "TEXT 2", "TEXT 3", "TEXT 4"],
        how: `Reads <code>Prompt</code> column for the raw user prompt. Reads <code>image 1</code>&ndash;<code>image 5</code> for product reference photo URLs. <strong>Also checks</strong> <code>TEXT 1</code>&ndash;<code>TEXT 4</code> columns &mdash; if they already have values from a previous run, <strong>skips parsing entirely</strong> and uses existing texts.`,
      },
      output: {
        columns: ["TEXT 1", "TEXT 2", "TEXT 3", "TEXT 4"],
        how: `Writes each parsed text to its own column via Google Sheets API: <code>TEXT 1</code>, <code>TEXT 2</code>, <code>TEXT 3</code>, <code>TEXT 4</code>. The operator sees them appear in the spreadsheet immediately.`,
      },
    },
    supabase: {
      input: {
        fields: ["params.prompt", "params.product_image_urls"],
        how: `Reads <code>prompt</code> (required) and <code>product_image_urls</code> (optional array) from the <code>input_params</code> JSON. No "skip if already done" logic &mdash; always parses fresh.`,
      },
      output: {
        fields: ["intermediates.parsed_texts"],
        how: `Saves all 4 texts as a JSON object into <code>intermediates.parsed_texts</code>: <code>{"text_1": "...", "text_2": "...", "text_3": "...", "text_4": "..."}</code>.`,
      },
      progress: "5% &rarr; 8%",
    },
    example: `<span class="comment">// Inputs</span>
<span class="key">prompt:</span> <span class="str">"JBL Boombox 4 speaker, waterproof, 24hr battery, deep bass,
perfect for pool parties and camping. Bold, rugged design."</span>
<span class="key">image_urls:</span> ["https://example.com/jbl-front.jpg",
              "https://example.com/jbl-side.jpg"]

<span class="comment">// Gemini parsed output</span>
<span class="key">TEXT 1:</span> <span class="str">"JBL Boombox 4 portable Bluetooth speaker"</span>
<span class="key">TEXT 2:</span> <span class="str">"Convince viewers to buy, show durability + power"</span>
<span class="key">TEXT 3:</span> <span class="str">"Cinematic, energetic, vibrant colors, outdoor lifestyle"</span>
<span class="key">TEXT 4:</span> <span class="str">"Scene 1: product reveal, Scene 2: pool party,
Scene 3: underwater waterproof demo, Scene 4: camping at night,
Scene 5: logo + tagline CTA"</span>`,
    notes: `The "skip if already filled" logic only exists in the Sheets pipeline (checks if columns have data). In the Supabase pipeline, parsing always runs since there's no prior state.`
  },
  {
    id: 2, num: "2", title: "Generate Clean Product Image", service: "Kie.ai (Nano Banana)",
    category: "cat-image",
    description: `Takes the user's reference product photos (often messy &mdash; bad lighting, cluttered backgrounds) and generates a <strong>clean, studio-quality product image</strong> on a white/neutral background.

This clean image becomes the visual reference for any scene where the product needs to appear. Uses the <strong>Nano Banana</strong> model via Kie.ai API.`,
    inputs: [
      '<span class="val">reference_image_urls[]</span> &mdash; 1-5 raw product photos',
      '<span class="val">text_1</span> &mdash; product description (guides what the AI generates)',
    ],
    outputs: [
      '<span class="val">clean_product_url</span> &mdash; URL to clean product image on white background',
    ],
    sheets: {
      input: {
        columns: ["image 1", "image 2", "image 3", "image 4", "image 5"],
        how: `Reads reference photo URLs from <code>image 1</code> through <code>image 5</code> (lowercase). Uses <code>text_1</code> from Step 1 (already in memory). If no images are provided, step is skipped.`,
      },
      output: {
        columns: ["Clean Product image"],
        how: `Writes the generated clean image URL to <code>Clean Product image</code> column. Also tries <code>Clean Product Image</code> (capitalized) as fallback if the first column name doesn't match.`,
      },
    },
    supabase: {
      input: {
        fields: ["params.product_image_urls"],
        how: `Reads <code>product_image_urls</code> array from <code>input_params</code>. Uses <code>text_1</code> from Step 1 (in memory).`,
      },
      output: {
        fields: ["intermediates.clean_product_image"],
        how: `Saves clean image URL to <code>intermediates.clean_product_image</code> (string field in JSONB).`,
      },
      progress: "10%",
    },
    example: `<span class="comment">// Input: messy product photo on kitchen table</span>
reference_images = ["https://example.com/jbl-on-table.jpg",
                    "https://example.com/jbl-side-blurry.jpg"]
text_1 = "JBL Boombox 4 portable Bluetooth speaker"

<span class="comment">// Output: clean studio product shot</span>
clean_product_url = "https://storage.googleapis.com/automatiq/
  products/row_5_clean_1738000000.png"
<span class="comment">// Speaker centered on pure white background, no shadows</span>`,
    notes: `If no reference images are provided, this step is skipped entirely. The clean image is used in Steps 4-7 whenever a scene has <code>product_visible: true</code>.`
  },
  {
    id: "2.5", num: "2.5", title: "Analyze Reference Video Structure",
    service: "Gemini AI (Video Understanding)", category: "cat-analyze", optional: true,
    description: `If the user provided an existing video to <strong>copy the style/pacing of</strong>, the pipeline downloads it and sends it to Gemini's video understanding model. Gemini watches the video and writes down its structure: how many scenes, how long each one, what transitions are used, overall pacing.

This structure is passed to Step 3 so Gemini can <strong>mimic the reference video's rhythm</strong>.`,
    inputs: [
      '<span class="val">video_reference_url</span> &mdash; URL to a reference video (e.g. competitor ad)',
    ],
    outputs: [
      '<span class="val">reference_video_structure</span> &mdash; JSON with scene_count, per-scene descriptions, timings, pacing',
    ],
    sheets: {
      input: {
        columns: ["Video reference"],
        how: `Reads the <code>Video reference</code> column. Expects an <code>http://</code> or <code>https://</code> URL to a video file. If empty, step is skipped.`,
      },
      output: {
        columns: [],
        how: `Nothing is written to the sheet. The structure analysis stays in memory and is passed internally to Step 3. The operator doesn't see this data.`,
      },
    },
    supabase: {
      input: {
        fields: ["params.video_reference_url"],
        how: `Reads <code>video_reference_url</code> from <code>input_params</code>. Downloads to a temp file, analyzes, deletes temp file.`,
      },
      output: {
        fields: [],
        how: `Nothing is saved to the database. The analysis stays in memory for Step 3.`,
      },
      progress: "12%",
    },
    example: `<span class="comment">// Input: a Nike commercial</span>
video_reference_url = "https://example.com/nike-ad-30s.mp4"

<span class="comment">// Gemini analysis output (kept in memory)</span>
{
  <span class="key">"scene_count"</span>: 6,
  <span class="key">"scenes"</span>: [
    { <span class="key">"duration"</span>: 3.2, <span class="key">"desc"</span>: <span class="str">"Opening: product close-up"</span> },
    { <span class="key">"duration"</span>: 4.1, <span class="key">"desc"</span>: <span class="str">"Action shot: runner in rain"</span> },
    ...
  ],
  <span class="key">"pacing"</span>: <span class="str">"Fast cuts, energetic, builds to climax"</span>
}`,
    notes: `Only runs when a video reference URL is provided. If download or analysis fails, the pipeline continues without structure guidance.`
  },
  {
    id: "2.7", num: "2.7", title: "Generate Voice Over (VO-First)", service: "GPT-4o + ElevenLabs TTS",
    category: "cat-vo",
    description: `<strong>The VO is generated BEFORE scene prompts.</strong> This is the "VO-first" architecture: by generating the voiceover audio first, we get <strong>exact word-level timestamps</strong> that determine how long each scene should be.

<strong>Step A &mdash; Write the script:</strong> GPT-4o generates a marketing VO script (~2.5 words/sec).
<strong>Step B &mdash; Record the audio:</strong> ElevenLabs converts to speech + returns word-level timestamps.
<strong>Voice selection:</strong> Uses "Voice id" if set, otherwise picks a random male voice matching the target language.`,
    inputs: [
      '<span class="val">text_1, text_2, text_3</span> &mdash; parsed product texts',
      '<span class="val">prompt</span> &mdash; original user prompt',
      '<span class="val">target_duration</span> &mdash; target video length (e.g. 30s)',
      '<span class="val">language, country</span> &mdash; for accent/style adaptation',
    ],
    outputs: [
      '<span class="val">vo_script</span> &mdash; the full VO text',
      '<span class="val">vo_audio_url</span> &mdash; GCS URL to the MP3',
      '<span class="val">vo_word_segments[]</span> &mdash; array of {text, start_time, end_time} per word',
      '<span class="val">vo_duration_seconds</span> &mdash; total VO length (e.g. 28.4)',
    ],
    sheets: {
      input: {
        columns: ["VO", "Voice id", "Language", "Country", "Duration"],
        how: `Reads <code>VO</code> column &mdash; if it already has a script, <strong>uses that instead of generating a new one</strong>. Reads <code>Voice id</code> for custom ElevenLabs voice. Reads <code>Language</code> for language code, <code>Country</code> for cultural adaptation, <code>Duration</code> for target video length.`,
      },
      output: {
        columns: ["VO", "New Voice"],
        how: `Writes the generated script to <code>VO</code> column (only if it was empty). Writes the audio MP3 URL to <code>New Voice</code> column. Word timestamps stay in memory for Steps 3, 7.5, 8, and 9.`,
      },
    },
    supabase: {
      input: {
        fields: ["params.voice_id", "params.language", "params.country", "params.duration"],
        how: `Reads <code>voice_id</code> (optional), <code>language</code> (default "en"), <code>country</code> (optional), <code>duration</code> (default 30) from <code>input_params</code>. Uses <code>text_1</code>&ndash;<code>text_3</code> from Step 1 (in memory).`,
      },
      output: {
        fields: ["intermediates.vo_script", "intermediates.vo_audio_url", "intermediates.vo_duration"],
        how: `Saves script to <code>intermediates.vo_script</code>, audio URL to <code>intermediates.vo_audio_url</code>, duration to <code>intermediates.vo_duration</code>. Word segments stay in memory (used for scene timing + subtitles later).`,
      },
      progress: "15% &rarr; 20%",
    },
    example: `<span class="comment">// Inputs</span>
<span class="key">text_1:</span> <span class="str">"JBL Boombox 4 portable Bluetooth speaker"</span>
<span class="key">text_2:</span> <span class="str">"Convince viewers to buy, show durability + power"</span>
<span class="key">text_3:</span> <span class="str">"Cinematic, energetic, vibrant colors, outdoor lifestyle"</span>
<span class="key">prompt:</span> <span class="str">"JBL Boombox 4 speaker, waterproof, 24hr battery..."</span>
<span class="key">target_duration:</span> 30  <span class="comment">// seconds</span>
<span class="key">language:</span> <span class="str">"en"</span>  <span class="key">country:</span> <span class="str">"US"</span>

<span class="comment">// GPT-4o generates the VO script</span>
<span class="str">"Meet the JBL Boombox 4. Built for adventure, engineered
for power. 24 hours of thundering bass that follows you
from the pool to the peaks. Waterproof, dustproof,
unstoppable. Dare to listen."</span>

<span class="comment">// ElevenLabs returns audio + word timestamps</span>
[
  { <span class="key">"text"</span>: <span class="str">"Meet"</span>,   <span class="key">"start"</span>: 0.00, <span class="key">"end"</span>: 0.25 },
  { <span class="key">"text"</span>: <span class="str">"the"</span>,    <span class="key">"start"</span>: 0.26, <span class="key">"end"</span>: 0.35 },
  { <span class="key">"text"</span>: <span class="str">"JBL"</span>,    <span class="key">"start"</span>: 0.38, <span class="key">"end"</span>: 0.72 },
  ...  <span class="comment">// 32 words total, duration = 18.2s</span>
]`,
    notes: `The VO-first approach means scenes are timed to the audio, not the other way around. Word segments are reused in Step 9 (ZapCap subtitles) for precise word-by-word timing.`
  },
  {
    id: 3, num: "3", title: "Generate Scene Prompts", service: "Gemini AI",
    category: "cat-scene",
    description: `Gemini takes <strong>everything collected so far</strong> and generates detailed instructions for each scene. Each scene gets:
&bull; <strong>image_prompt</strong> &mdash; what to draw
&bull; <strong>motion_prompt</strong> &mdash; how to animate it
&bull; <strong>duration</strong> &mdash; synced to VO word timestamps
&bull; <strong>vo_word_start / vo_word_end</strong> &mdash; which VO words play during this scene
&bull; <strong>product_visible, narrative_role</strong> &mdash; scene metadata

After Gemini returns, a <strong>post-processing step</strong> maps VO word indices to exact ElevenLabs timestamps. Scenes tile continuously with no gaps.

When <code>sound_sync_method=beat_sync</code> (default), <code>assign_beat_sync_durations()</code> runs after Gemini returns, using the "extend to next beat start" strategy: each scene extends from the START of its first word to the START of the next scene's first word. This produces two new fields per scene: <strong>exact_duration</strong> (float, for STEP 7.5 trim) and <strong>overgenerate_duration</strong> (<code>ceil(exact_duration)</code>, for video generation in STEPS 4-7).`,
    inputs: [
      '<span class="val">text_1-4</span> &mdash; parsed product texts',
      '<span class="val">vo_timing</span> &mdash; VO word segments with timestamps',
      '<span class="val">target_duration</span> &mdash; target video length',
      '<span class="val">character_description</span> &mdash; from Step 0 (optional)',
      '<span class="val">reference_video_structure</span> &mdash; from Step 2.5 (optional)',
      '<span class="val">logo_url, slogan_text</span> &mdash; for CTA scene (optional)',
    ],
    outputs: [
      '<span class="val">scenes[]</span> &mdash; array of scene objects (typically 4-8 for 30s), each with exact_duration + overgenerate_duration when beat_sync',
      '<span class="val">music_style</span> &mdash; suggested background music description',
    ],
    sheets: {
      input: {
        columns: [],
        how: `No additional columns are read. All inputs come from memory (TEXT 1-4 from Step 1, VO timing from Step 2.7, character description from Step 0, reference structure from Step 2.5, logo/slogan from their columns read earlier by the orchestrator).`,
      },
      output: {
        columns: ["Scene 1 - First prompt", "Scene 1 - Second prompt", "Scene 2 - First prompt", "Scene 2 - Second prompt", "...up to Scene 20"],
        how: `Writes each scene's <code>image_prompt</code> to <code>Scene N - First prompt</code> and <code>motion_prompt</code> to <code>Scene N - Second prompt</code> columns (up to Scene 20). Operator can see exactly what prompts will be used for generation.`,
      },
    },
    supabase: {
      input: {
        fields: ["params.sound_sync_method"],
        how: `Reads <code>sound_sync_method</code> (default "beat_sync") from <code>input_params</code>. All other inputs come from memory (parsed_texts, vo_timing, character_description, etc.). When beat_sync, <code>assign_beat_sync_durations()</code> runs as a post-processing step after Gemini returns.`,
      },
      output: {
        fields: ["intermediates.scene_prompts"],
        how: `Saves the full scenes array (with all fields: image_prompt, motion_prompt, duration, vo_word_start, vo_word_end, product_visible, narrative_role, plus <code>exact_duration</code> and <code>overgenerate_duration</code> when beat_sync) to <code>intermediates.scene_prompts</code> as a JSON array.`,
      },
      progress: "22% &rarr; 25%",
    },
    example: `<span class="comment">// Inputs (from previous steps + user)</span>
<span class="key">text_1-4:</span> <span class="comment">// from Step 1</span>
<span class="key">vo_timing:</span> <span class="comment">// word segments from Step 2.7</span>
<span class="key">target_duration:</span> 30
<span class="key">character_description:</span> <span class="str">"Athletic man, ~30, short brown hair..."</span>  <span class="comment">// from Step 0 (optional)</span>
<span class="key">reference_video_structure:</span> <span class="comment">// from Step 2.5 (optional)</span>
<span class="key">logo_url:</span> <span class="str">"https://storage.googleapis.com/.../jbl-logo.png"</span>  <span class="comment">// user input (optional)</span>
<span class="key">slogan_text:</span> <span class="str">"Dare to Listen"</span>  <span class="comment">// user input (optional, used in CTA scene)</span>

<span class="comment">// Gemini output: 5 scenes for a 25s video</span>
[
  {
    <span class="key">"scene_num"</span>: 1, <span class="key">"narrative_role"</span>: <span class="str">"intro"</span>,
    <span class="key">"image_prompt"</span>: <span class="str">"Dramatic close-up of JBL Boombox 4 on dark
surface, LED ring glowing electric blue, moody lighting"</span>,
    <span class="key">"motion_prompt"</span>: <span class="str">"Slow zoom in toward the speaker"</span>,
    <span class="key">"duration"</span>: 4.0, <span class="key">"vo_start_time"</span>: 0.0, <span class="key">"vo_end_time"</span>: 4.0,
    <span class="key">"exact_duration"</span>: 3.82, <span class="key">"overgenerate_duration"</span>: 4, <span class="comment">// beat_sync fields</span>
    <span class="key">"product_visible"</span>: true
  },
  ...
  {
    <span class="key">"scene_num"</span>: 5, <span class="key">"narrative_role"</span>: <span class="str">"cta"</span>,
    <span class="key">"image_prompt"</span>: <span class="str">"JBL logo centered on dark background,
'Dare to Listen' tagline below in bold white"</span>,
    <span class="key">"motion_prompt"</span>: <span class="str">"Slow fade in, subtle glow pulse on logo"</span>,
    <span class="key">"duration"</span>: 3.5, <span class="key">"product_visible"</span>: true
  }
]
<span class="key">music_style:</span> <span class="str">"Energetic electronic, powerful bass drops"</span>`,
    notes: `Gemini suggests word indices, but they're mapped to exact ElevenLabs timestamps. If Gemini's indices are broken, words are auto-distributed evenly across scenes. When <strong>sound_sync_method=beat_sync</strong>, <code>assign_beat_sync_durations()</code> adds <code>exact_duration</code> (float) and <code>overgenerate_duration</code> (ceil integer) to each scene dict. Videos are generated at overgenerate_duration and later trimmed to exact_duration in STEP 7.5.`
  },
  {
    id: "4-7", num: "4-7", title: "Parallel Asset Generation",
    service: "Kie.ai + Veo3/Runway/Kling + Suno", category: "cat-parallel", isParallel: true,
    description: `Three independent tracks run <strong>simultaneously</strong> using ThreadPoolExecutor:

<strong>Track 1 &mdash; Image + Video per scene:</strong> Generate image (Nano Banana / Kie.ai), then animate (Veo3, Runway, or Kling). Up to 8 scenes in parallel. Each clip's <strong>first 1 second is trimmed</strong>. When <code>quality_check=true</code> (default), each image is scored by Gemini (1-10) and regenerated once if score &lt; 5. When <code>sound_sync_method=beat_sync</code>, videos are generated at <code>overgenerate_duration</code> (integer ceil of exact float duration) to allow frame-perfect trim in STEP 7.5.

<strong>Track 2 &mdash; Background Music (Suno):</strong> Generates ~30-60s instrumental track.

<strong>Track 3 &mdash; VO:</strong> Already completed in Step 2.7.

Each completed asset is written to the sheet/database <strong>immediately</strong>.`,
    inputs: [
      '<span class="val">scenes[]</span> &mdash; scene prompts with image_prompt, motion_prompt, duration',
      '<span class="val">clean_product_url</span> &mdash; clean product image for reference',
      '<span class="val">visual_style</span> &mdash; "Auto", "Modern flat 2d", "Paper Cut", etc.',
      '<span class="val">animation_model</span> &mdash; "runway", "kling", or "google" (Veo 3)',
      '<span class="val">music_style</span> &mdash; mood description for background music',
      '<span class="val">quality_check</span> &mdash; bool (default true), image quality gate via Gemini scoring',
      '<span class="val">sound_sync_method</span> &mdash; "beat_sync" or "none" (controls overgenerate_duration usage)',
    ],
    outputs: [
      '<span class="val">scene_images[]</span> &mdash; URL per scene image',
      '<span class="val">scene_videos[]</span> &mdash; URL per scene video (trimmed)',
      '<span class="val">music_url</span> &mdash; background music MP3 URL',
    ],
    sheets: {
      input: {
        columns: ["Animation model", "Style"],
        how: `Reads <code>Animation model</code> column for which video engine to use ("runway", "kling", "google/veo3"). Reads <code>Style</code> column for visual style. All other inputs (scene prompts, product URL) come from memory.`,
      },
      output: {
        columns: ["Scene 1 - new image", "Scene 1 - new video", "Scene 2 - new image", "Scene 2 - new video", "...up to Scene 20", "New music"],
        how: `Each scene writes its image URL to <code>Scene N - new image</code> and video URL to <code>Scene N - new video</code>. Music URL goes to <code>New music</code>. <strong>Assets appear in the spreadsheet as they're generated</strong> &mdash; the operator can watch progress live.`,
      },
    },
    supabase: {
      input: {
        fields: ["params.animation_model", "params.style", "params.quality_check", "params.sound_sync_method"],
        how: `Reads <code>animation_model</code> (default "google"), <code>style</code> (default "Auto"), <code>quality_check</code> (default true), and <code>sound_sync_method</code> (default "beat_sync") from <code>input_params</code>. When beat_sync, uses <code>overgenerate_duration</code> from each scene (integer) as the video generation duration. When quality_check is true, Gemini scores each image 1-10 and regenerates once if below 5. Scene prompts and product URL come from memory.`,
      },
      output: {
        fields: ["intermediates.scene_images", "intermediates.scene_videos", "intermediates.music_url"],
        how: `Saves image URLs as array to <code>intermediates.scene_images</code>, video URLs to <code>intermediates.scene_videos</code>, and music URL to <code>intermediates.music_url</code>. Progress climbs from 30% to 70% as each scene completes.`,
      },
      progress: "30% &rarr; 70%",
    },
    example: `<span class="comment">// Inputs</span>
<span class="key">scenes[]:</span> <span class="comment">// 5 scene prompts from Step 3</span>
<span class="key">clean_product_url:</span> <span class="str">"https://storage.googleapis.com/.../clean.png"</span>  <span class="comment">// from Step 2</span>
<span class="key">visual_style:</span> <span class="str">"Auto"</span>  <span class="comment">// user input</span>
<span class="key">animation_model:</span> <span class="str">"google"</span>  <span class="comment">// user input (Veo 3)</span>
<span class="key">music_style:</span> <span class="str">"Energetic electronic, powerful bass drops"</span>  <span class="comment">// from Step 3</span>

<span class="comment">// Parallel execution timeline</span>
<span class="key">Track 1 - Scenes (all in parallel):</span>
  Scene 1: [Image 15s] \u2192 [Veo3 90s] \u2192 [Trim 1s] \u2713
  Scene 2: [Image 15s] \u2192 [Runway 45s] \u2192 [Trim 1s] \u2713
  Scene 3: [Image 15s] \u2192 [Veo3 90s] \u2192 [Trim 1s] \u2713
  Scene 4: [Image 15s] \u2192 [Kling 60s] \u2192 [Trim 1s] \u2713
  Scene 5: [Image 15s] \u2192 [Veo3 90s] \u2192 [Trim 1s] \u2713

<span class="key">Track 2 - Music:</span>
  [Suno ~30s] \u2713

<span class="comment">// Total wall-clock: ~2-3 min (bottleneck = slowest scene)</span>`,
    notes: `If image generation fails, retries once after 45s. If still failing, that scene is skipped and its duration is redistributed in Step 8. Rate limits: max 8 concurrent Veo calls, max 4 concurrent image calls. When <strong>quality_check=true</strong>, Gemini scores each image 1-10; if below 5, the image is regenerated once and the higher-scoring version is kept. When <strong>beat_sync</strong> mode, videos are over-generated at <code>overgenerate_duration</code> (integer seconds) and later trimmed to <code>exact_duration</code> (float) in STEP 7.5.`
  },
  {
    id: "7.5", num: "7.5", title: "Beat-Sync Trim", service: "Rendi.dev / Local FFmpeg",
    category: "cat-combine", optional: true,
    description: `<strong>Only runs when <code>sound_sync_method=beat_sync</code></strong> (the default). Each scene video was over-generated in STEPS 4-7 at <code>overgenerate_duration</code> (integer ceil of exact float). This step trims each video to its <strong>exact_duration</strong> (float seconds derived from VO word timestamps) for frame-perfect audio-video sync.

Example: scene has <code>exact_duration=3.82s</code>, generated at <code>overgenerate_duration=4s</code>. This step trims the clip from 4.0s down to 3.82s.

Runs <strong>in parallel</strong> (up to 8 workers) using Rendi or local FFmpeg. If trimming fails for a scene, the original (un-trimmed) video is kept.

When <code>sound_sync_method=none</code>, this entire step is <strong>skipped</strong> and STEP 8 handles duration adjustments via slow-motion/trim instead.`,
    inputs: [
      '<span class="val">scene_videos[]</span> &mdash; over-generated video URLs from STEPS 4-7',
      '<span class="val">scenes[].exact_duration</span> &mdash; float seconds per scene (from beat_sync assignment in Step 3)',
      '<span class="val">scenes[].overgenerate_duration</span> &mdash; integer seconds each video was generated at',
    ],
    outputs: [
      '<span class="val">trimmed_scene_videos[]</span> &mdash; trimmed video URLs (same count, same order)',
    ],
    sheets: {
      input: {
        columns: [],
        how: `No columns are read. Scene videos and duration data come from memory (Steps 3 and 4-7).`,
      },
      output: {
        columns: [],
        how: `No columns are written. The trimmed URLs replace the originals in memory and are passed to Step 8.`,
      },
    },
    supabase: {
      input: {
        fields: ["params.sound_sync_method"],
        how: `Only runs when <code>sound_sync_method=beat_sync</code>. All scene videos and duration data come from memory.`,
      },
      output: {
        fields: ["intermediates.trimmed_scene_videos"],
        how: `Saves the trimmed video URL list to <code>intermediates.trimmed_scene_videos</code>. These replace the original scene_videos for subsequent steps.`,
      },
      progress: "72% &rarr; 74%",
    },
    example: `<span class="comment">// beat_sync mode: trim over-generated clips to exact VO-synced durations</span>
<span class="key">scene_videos[]:</span> ["scene1_4s.mp4", "scene2_6s.mp4", "scene3_5s.mp4", "scene4_4s.mp4", "scene5_4s.mp4"]

<span class="comment">// Per-scene exact_duration vs overgenerate_duration</span>
Scene 1: exact=3.82s, overgen=4s  <span class="comment">// trim 0.18s</span>
Scene 2: exact=5.14s, overgen=6s  <span class="comment">// trim 0.86s</span>
Scene 3: exact=4.50s, overgen=5s  <span class="comment">// trim 0.50s</span>
Scene 4: exact=4.00s, overgen=4s  <span class="comment">// no trim needed (exact == overgen)</span>
Scene 5: exact=3.20s, overgen=4s  <span class="comment">// trim 0.80s</span>

<span class="comment">// Output: frame-perfect clips ready for concatenation</span>
trimmed = ["scene1_3.82s.mp4", "scene2_5.14s.mp4", "scene3_4.50s.mp4",
           "scene4_4.00s.mp4", "scene5_3.20s.mp4"]`,
    notes: `Only runs when sound_sync_method=beat_sync (default). Replaces the old STEP 8.5 (trim/adjust final duration) approach. Instead of adjusting the assembled video after the fact, beat-sync trims each individual clip before concatenation for much more precise sync. If overgenerate_duration equals exact_duration, no trim is needed for that scene.`
  },
  {
    id: 8, num: "8", title: "Concatenate + Add Audio", service: "Rendi.dev (FFmpeg cloud)",
    category: "cat-combine",
    description: `All scene clips are assembled into a single video with audio:

<strong>8a.</strong> Concatenate all clips (in beat_sync mode, clips are already frame-perfect from STEP 7.5)
<strong>8b.</strong> Mix audio: VO at full volume (1.0) + music at 20% (0.2)

In <code>sound_sync_method=none</code> mode, Step 8 also adjusts clip durations (trim or slow-motion) to match VO timing and ensures the last scene covers the full VO duration. In <code>beat_sync</code> mode, clips are already trimmed to exact duration by STEP 7.5.

If Rendi fails, a <strong>local FFmpeg fallback</strong> is attempted.`,
    inputs: [
      '<span class="val">scene_videos[]</span> &mdash; ordered list of scene video URLs (trimmed if beat_sync, from STEP 7.5)',
      '<span class="val">scene durations[]</span> &mdash; exact_duration per scene (beat_sync) or Gemini-suggested duration (none)',
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
        how: `No columns are read. All inputs (scene videos, VO audio, music URL, durations) come from memory / previous steps.`,
      },
      output: {
        columns: ["RENDI Scene", "RENDI Scene & Voice"],
        how: `Writes the video-only concatenation URL to <code>RENDI Scene</code> column, and the final video with VO + music to <code>RENDI Scene & Voice</code> column. The operator can click these URLs to preview.`,
      },
    },
    supabase: {
      input: {
        fields: [],
        how: `No additional reads from the database. All inputs come from memory (scene_videos, vo_audio_url, music_url from previous steps).`,
      },
      output: {
        fields: ["intermediates.concat_url", "intermediates.rendi_scene_voice_url"],
        how: `Saves the video-only concatenation to <code>intermediates.concat_url</code> and the audio-mixed video to <code>intermediates.rendi_scene_voice_url</code>.`,
      },
      progress: "75% &rarr; 88%",
    },
    example: `<span class="comment">// Inputs (beat_sync mode: clips already trimmed by STEP 7.5)</span>
<span class="key">scene_videos[]:</span> ["scene1_3.82s.mp4", "scene2_5.14s.mp4", ..., "scene5_3.20s.mp4"]  <span class="comment">// from Step 7.5</span>
<span class="key">scene_durations[]:</span> [3.82, 5.14, 4.50, 4.00, 3.20]  <span class="comment">// exact_duration from beat_sync</span>
<span class="key">vo_audio_url:</span> <span class="str">"https://storage.googleapis.com/.../vo.mp3"</span>  <span class="comment">// from Step 2.7</span>
<span class="key">music_url:</span> <span class="str">"https://storage.googleapis.com/.../music.mp3"</span>  <span class="comment">// from Step 4-7</span>

<span class="comment">// Processing</span>
video_data = [
  { url: "scene1_3.82s.mp4", duration: 3.82 },
  { url: "scene2_5.14s.mp4", duration: 5.14 },
  { url: "scene3_4.50s.mp4", duration: 4.50 },
  { url: "scene4_4.00s.mp4", duration: 4.00 },
  { url: "scene5_3.20s.mp4", duration: 3.20 },
]

<span class="comment">// 8a: Concatenated (clips already at exact duration in beat_sync)</span>
<span class="comment">// 8b: VO (vol=1.0) + music (vol=0.2) mixed on top</span>
final = "https://storage.googleapis.com/.../row_5_with_audio.mp4"`,
    notes: `In beat_sync mode, clips arrive pre-trimmed from STEP 7.5 so no slow-motion or per-clip trim is needed here. In none mode, Step 8 still adjusts clip durations (trim/slow-motion) and ensures the last scene covers the VO. The local FFmpeg fallback runs if the Rendi cloud API is unreachable.`
  },
  {
    id: 9, num: "9", title: "Add Subtitles", service: "ZapCap API",
    category: "cat-subtitle", optional: true,
    description: `If subtitles are requested, the video is sent to ZapCap for subtitle burn-in. ZapCap uses the <strong>ElevenLabs word segments</strong> from Step 2.7 as "Bring Your Own Transcript" (BYOT), so subtitles appear at the <strong>exact moment each word is spoken</strong>.

If ZapCap fails, the pipeline uses the video without subtitles &mdash; it doesn't fail the entire job.`,
    inputs: [
      '<span class="val">rendi_scene_voice_url</span> &mdash; assembled video with audio',
      '<span class="val">subtitle_language</span> &mdash; language code',
      '<span class="val">vo_word_segments[]</span> &mdash; word-level timestamps from ElevenLabs',
    ],
    outputs: [
      '<span class="val">subtitled_video_url</span> &mdash; video with burned-in subtitles',
    ],
    sheets: {
      input: {
        columns: ["Add subtitles", "Language"],
        how: `Reads <code>Add subtitles</code> column (must be "yes" to enable). Reads <code>Language</code> column for subtitle language code. Word segments come from memory (Step 2.7).`,
      },
      output: {
        columns: ["Subtitled Video"],
        how: `Writes the subtitled video URL to <code>Subtitled Video</code> column.`,
      },
    },
    supabase: {
      input: {
        fields: ["params.add_subtitles", "params.language"],
        how: `Reads <code>add_subtitles</code> (boolean, default true) and <code>language</code> from <code>input_params</code>. Word segments come from memory.`,
      },
      output: {
        fields: ["intermediates.subtitled_url"],
        how: `Saves subtitled video URL to <code>intermediates.subtitled_url</code>.`,
      },
      progress: "90%",
    },
    example: `<span class="comment">// Inputs</span>
<span class="key">video:</span> <span class="str">"https://storage.googleapis.com/.../final.mp4"</span>  <span class="comment">// from Step 8</span>
<span class="key">subtitle_language:</span> <span class="str">"en"</span>  <span class="comment">// user input</span>
<span class="key">vo_word_segments:</span> [  <span class="comment">// from Step 2.7</span>
  { text: "Meet",    start: 0.00, end: 0.25 },
  { text: "the",     start: 0.26, end: 0.35 },
  { text: "JBL",     start: 0.38, end: 0.72 },
  ...
]

<span class="comment">// Output: video with animated subtitles</span>
subtitled = "https://api.zapcap.ai/renders/abc123/output.mp4"`,
    notes: `ZapCap supports 60+ languages. The template ID is configured in Config (ZAPCAP_TEMPLATE_ID). If subtitles fail, the job still succeeds without subtitles.`
  },
  {
    id: 10, num: "10", title: "Upload to Mux CDN", service: "Mux CDN",
    category: "cat-upload",
    description: `Uploads the final video to <strong>Mux CDN</strong> for HLS streaming and MP4 download. Mux processes the video asynchronously &mdash; the pipeline marks the job as completed immediately after initiating the upload, and a webhook updates the job when Mux is ready.

This is the final step. After this, the job is marked as <strong>completed</strong>.`,
    inputs: [
      '<span class="val">subtitled_video_url</span> (or rendi_scene_voice_url if no subtitles)',
      '<span class="val">vo_audio_url</span> &mdash; voice over audio',
      '<span class="val">music_url</span> &mdash; background music',
    ],
    outputs: [
      '<span class="val">final_stream_url</span> &mdash; HLS stream URL for video players',
    ],
    sheets: {
      input: {
        columns: [],
        how: `No columns are read. All URLs come from memory (the result of previous steps).`,
      },
      output: {
        columns: ["Final Video"],
        how: `Writes the final permanent video URL to <code>Final Video</code> column. This is the <strong>last column to be filled</strong>. When the operator sees this column populated, the job is done.`,
      },
    },
    supabase: {
      input: {
        fields: [],
        how: `No additional reads. The video URL comes from memory.`,
      },
      output: {
        fields: ["output.final_video_url", "output.vo_audio_url", "output.music_url", "output.concat_url"],
        how: `Saves all permanent URLs to the <code>output</code> JSON field (separate from intermediates). Sets <code>status = 'completed'</code>. The caller polling <code>GET /api/jobs/{id}</code> sees <code>"status": "completed"</code> with all output URLs.`,
      },
      progress: "95% &rarr; 100%",
    },
    example: `<span class="comment">// Inputs: temporary URLs (expire in 24h)</span>
<span class="key">subtitled_video_url:</span> <span class="str">"https://api.zapcap.ai/temp/abc123.mp4"</span>  <span class="comment">// from Step 9</span>
<span class="key">vo_audio_url:</span> <span class="str">"https://rendi-outputs.s3.amazonaws.com/vo.mp3"</span>  <span class="comment">// from Step 2.7</span>
<span class="key">music_url:</span> <span class="str">"https://rendi-outputs.s3.amazonaws.com/music.mp3"</span>  <span class="comment">// from Step 4-7</span>

<span class="comment">// Output: permanent GCS URLs (never expire)</span>
final_video_url = "https://storage.googleapis.com/automatiq/
  Comp/Final_Video/job_abc123_final.mp4"

<span class="comment">// Sheets: "Final Video" column now has the permanent URL</span>
<span class="comment">// Supabase: status = "completed", progress = 100%</span>`,
    notes: `This is where the two implementations diverge most. In Sheets, the operator knows the job is done because the "Final Video" column gets filled. In Supabase, the caller knows because the job status changes to "completed" and the output JSON contains all URLs.`
  }
];

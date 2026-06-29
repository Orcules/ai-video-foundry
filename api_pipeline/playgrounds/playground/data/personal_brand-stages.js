export const stages = [
  {
    id: 0, num: "0", title: "Describe Character(s)", service: "Gemini AI",
    category: "cat-analyze",
    description: `Generates or describes <strong>multiple characters</strong> for multi-person service videos. Supports up to 3 characters via <code>character_url</code> (primary) and <code>character_urls[]</code> (additional).

<strong>Path A &mdash; No character photo:</strong> Kie.ai generates a portrait from <code>gender</code> + <code>country</code>, then Gemini describes it.

<strong>Path B &mdash; Character photo(s) provided:</strong> Gemini analyzes each photo and writes appearance descriptions.

<strong>Path C &mdash; Text description provided:</strong> Used directly for the primary character.

All character descriptions are injected into scene prompts for consistent multi-person appearance.`,
    inputs: [
      '<span class="val">character_url</span> &mdash; primary character photo (optional)',
      '<span class="val">character_urls[]</span> &mdash; additional character photos (optional, up to 2)',
      '<span class="val">character_description</span> &mdash; text description (optional, bypasses AI)',
      '<span class="val">gender</span> &mdash; "m" or "f"',
      '<span class="val">country</span> &mdash; cultural appearance hints (optional)',
    ],
    outputs: [
      '<span class="val">influencer_image_url</span> &mdash; primary character portrait URL',
      '<span class="val">influencer_description</span> &mdash; primary character text description',
      '<span class="val">additional_character_descriptions[]</span> &mdash; descriptions of characters 2-3',
    ],
    supabase: {
      input: {
        fields: ["params.character_url", "params.character_urls", "params.character_description", "params.gender", "params.country"],
        how: `Reads primary character from <code>character_url</code> or <code>character_description</code>. Additional characters from <code>character_urls[]</code>. Falls back to Kie.ai generation using <code>gender</code> + <code>country</code>.`,
      },
      output: {
        fields: ["intermediates.influencer_image", "intermediates.influencer_description", "intermediates.additional_character_descriptions"],
        how: `Saves portrait URL, primary description, and additional character descriptions.`,
      },
      progress: "5%",
    },
    example: `<span class="comment">// Multi-character setup</span>
<span class="key">character_url:</span> <span class="str">"https://example.com/stylist.jpg"</span>
<span class="key">character_urls:</span> [<span class="str">"https://example.com/client.jpg"</span>]

<span class="comment">// Gemini describes each character</span>
influencer_description = <span class="str">"Professional stylist, ~30, neat appearance,
confident posture, wearing salon apron"</span>
additional_character_descriptions = [
  <span class="str">"Client, young woman ~25, relaxed expression,
  casual outfit, sitting in salon chair"</span>
]`,
    notes: `Similar to UGC Step 0 but supports multiple characters. Primary character uses the same path as UGC. Additional characters from character_urls[] are described in parallel via Gemini.`
  },
  {
    id: 1, num: "1", title: "Parse Prompt (TEXT 1-4)", service: "Gemini AI",
    category: "cat-parse",
    description: `Gemini analyzes the raw prompt into 4 structured text fields. Uses UGC-style parsing with first-person emphasis, adapted for multi-character service scenarios.`,
    inputs: [
      '<span class="val">prompt</span> &mdash; raw description from the user',
      '<span class="val">reference_image_urls[]</span> &mdash; optional reference images',
    ],
    outputs: [
      '<span class="val">text_1</span> &mdash; subject identity (service/business)',
      '<span class="val">text_2</span> &mdash; video goal + selling points',
      '<span class="val">text_3</span> &mdash; visual style and mood',
      '<span class="val">text_4</span> &mdash; suggested structure',
    ],
    supabase: {
      input: {
        fields: ["params.prompt", "params.reference_image_urls"],
        how: `Reads <code>prompt</code> (required) and <code>reference_image_urls</code> (optional) from <code>input_params</code>.`,
      },
      output: {
        fields: ["intermediates.parsed_texts"],
        how: `Saves all 4 texts as JSON object into <code>intermediates.parsed_texts</code>.`,
      },
      progress: "10%",
    },
    example: `<span class="comment">// Personal service prompt parsing</span>
<span class="key">prompt:</span> <span class="str">"Premium hair salon downtown, balayage specialist,
luxury experience with complimentary drinks"</span>

<span class="key">TEXT 1:</span> <span class="str">"Premium downtown hair salon, balayage specialist"</span>
<span class="key">TEXT 2:</span> <span class="str">"Showcase luxury salon experience, highlight balayage"</span>
<span class="key">TEXT 3:</span> <span class="str">"Warm, sophisticated, cinematic lighting"</span>
<span class="key">TEXT 4:</span> <span class="str">"Scene 1: entrance, Scene 2: consultation,
Scene 3: treatment, Scene 4: reveal, Scene 5: CTA"</span>`,
    notes: `Same parsing as UGC pipeline. The prompt template encourages authentic, first-person service-oriented content.`
  },
  {
    id: 2, num: "2", title: "Analyze Reference Images", service: "Gemini AI",
    category: "cat-analyze", optional: true,
    description: `Gemini analyzes each reference image and writes a text description for scene matching. Skipped if no reference images are provided.`,
    inputs: [
      '<span class="val">reference_image_urls[]</span> &mdash; photos of service locations, equipment, etc.',
    ],
    outputs: [
      '<span class="val">reference_analyses[]</span> &mdash; text description per image',
    ],
    supabase: {
      input: {
        fields: ["params.reference_image_urls"],
        how: `Reads <code>reference_image_urls</code> array. If empty, step is skipped.`,
      },
      output: {
        fields: ["intermediates.reference_analyses"],
        how: `Saves text descriptions array.`,
      },
      progress: "15%",
    },
    example: `<span class="comment">// Reference images of the service space</span>
reference_image_urls = ["https://example.com/salon-interior.jpg"]
reference_analyses = [
  <span class="str">"Modern salon interior with exposed brick walls,
  warm lighting, styling stations with large mirrors"</span>
]`,
    notes: `Same as UGC Step 2.5. Provides visual context for scene generation.`
  },
  {
    id: "2.7", num: "2.7", title: "Generate VO Script + TTS", service: "GPT-4o + ElevenLabs",
    category: "cat-vo",
    description: `Generates the voiceover script using <code>generate_influencer_vo_script()</code> and converts to audio via ElevenLabs TTS.

The VO is generated <strong>before</strong> scene prompts (pre-split) so word timestamps can drive per-scene durations via beat sync.`,
    inputs: [
      '<span class="val">text_1, text_2, text_3</span> &mdash; parsed texts from Step 1',
      '<span class="val">target_duration</span> &mdash; VO target length in seconds',
      '<span class="val">language</span> &mdash; for script language and voice/accent',
      '<span class="val">prompt</span> &mdash; original user prompt for context',
      '<span class="val">voice_id</span> &mdash; custom voice (optional)',
      '<span class="val">gender</span> &mdash; for voice selection',
    ],
    outputs: [
      '<span class="val">vo_script</span> &mdash; generated VO script',
      '<span class="val">vo_audio_url</span> &mdash; MP3 URL',
      '<span class="val">vo_word_timestamps[]</span> &mdash; per-word timing from ElevenLabs',
    ],
    supabase: {
      input: {
        fields: ["params.voice_id", "params.gender", "params.language"],
        how: `Reads voice params. Parsed texts and target duration from memory.`,
      },
      output: {
        fields: ["intermediates.vo_script", "intermediates.vo_audio_url", "intermediates.vo_word_timestamps"],
        how: `Saves generated VO script, audio URL, and word timestamps.`,
      },
      progress: "25%",
    },
    example: `<span class="comment">// VO generation for service video</span>
vo_script = <span class="str">"Welcome to the most amazing salon experience
you'll ever have. From the moment you walk in..."</span>

<span class="key">vo_audio_url:</span> <span class="str">"https://storage.googleapis.com/.../ps_vo.mp3"</span>
<span class="key">vo_word_timestamps:</span> [
  { text: "Welcome", start: 0.00, end: 0.32 },
  { text: "to",      start: 0.34, end: 0.42 },
  ...
]`,
    notes: `Pre-split VO: generated before scene prompts so timestamps inform scene durations. Uses expressive ElevenLabs settings with sentence pauses.`
  },
  {
    id: 3, num: "3", title: "Pre-split VO + Scene Prompts", service: "Gemini AI",
    category: "cat-scene",
    description: `Gemini generates scene-by-scene prompts with VO timing awareness. Uses the VO word timestamps to inform scene pacing and structure.

Scene prompts include all character descriptions for multi-person scenes. The influencer and additional characters appear together in relevant scenes.`,
    inputs: [
      '<span class="val">text_1-4</span> &mdash; parsed texts from Step 1',
      '<span class="val">influencer_description</span> &mdash; from Step 0',
      '<span class="val">additional_character_descriptions[]</span> &mdash; from Step 0',
      '<span class="val">reference_analyses[]</span> &mdash; from Step 2 (optional)',
      '<span class="val">vo_word_timestamps[]</span> &mdash; from Step 2.7',
    ],
    outputs: [
      '<span class="val">scene_prompts[]</span> &mdash; image + motion prompts per scene',
      '<span class="val">music_style</span> &mdash; suggested background music mood',
    ],
    supabase: {
      input: {
        fields: ["params.duration"],
        how: `Uses duration for scene count calculation. All other inputs from memory.`,
      },
      output: {
        fields: ["intermediates.scene_prompts", "intermediates.music_style"],
        how: `Saves scene prompts and music style suggestion.`,
      },
      progress: "30%",
    },
    example: `<span class="comment">// Scene prompts with multi-character descriptions</span>
scene_prompts = [
  {
    <span class="key">"scene"</span>: 1,
    <span class="key">"image_prompt"</span>: <span class="str">"Exterior of modern salon, warm light
    from large windows, the stylist (30, neat, confident)
    greeting the client (25, casual) at the door"</span>,
    <span class="key">"motion"</span>: <span class="str">"Slow dolly forward"</span>
  },
  {
    <span class="key">"scene"</span>: 2,
    <span class="key">"image_prompt"</span>: <span class="str">"Close-up of balayage application,
    stylist's hands working carefully..."</span>,
    <span class="key">"motion"</span>: <span class="str">"Gentle tilt"</span>
  },
  ...
]`,
    notes: `Similar to UGC scene generation but with multi-character support. Character descriptions for all characters are injected into scene prompts where appropriate.`
  },
  {
    id: "4-7", num: "4-7", title: "Generate Assets (Images + Videos + Music)",
    service: "Kie.ai / Gemini + Kling/Runway/Veo3 + Suno", category: "cat-parallel", isParallel: true,
    description: `Parallel asset generation across three tracks:

<strong>Track 1 &mdash; Per scene:</strong> Generate image (via selected <code>image_api</code>) &rarr; animate to video (Veo3/Kling/Runway) &rarr; optional quality gate.

<strong>Track 2 &mdash; Per asset:</strong> If image &rarr; animate with zoom. If video &rarr; trim to 3s max.

<strong>Track 3 &mdash; Music:</strong> Gemini describes mood &rarr; Suno generates background track.

The <code>image_api</code> parameter controls the image generation backend: <code>kie</code> (Nano Banana), <code>kie-flash</code> (faster), or <code>google</code> (Gemini native).`,
    inputs: [
      '<span class="val">scene_prompts[]</span> &mdash; from Step 3',
      '<span class="val">influencer_image_url</span> &mdash; from Step 0',
      '<span class="val">reference_image_urls[]</span> &mdash; location/environment photos',
      '<span class="val">visual_style</span> &mdash; "Auto", "Cinematic", etc.',
      '<span class="val">animation_model</span> &mdash; "auto", "google", "kling", "runway"',
      '<span class="val">image_api</span> &mdash; "kie", "kie-flash", or "google"',
      '<span class="val">asset_urls[]</span> &mdash; user-provided photos/videos',
    ],
    outputs: [
      '<span class="val">scene_images[]</span> &mdash; URL per scene image',
      '<span class="val">scene_videos[]</span> &mdash; URL per scene video',
      '<span class="val">asset_videos[]</span> &mdash; processed asset clips',
      '<span class="val">music_url</span> &mdash; background music MP3',
    ],
    supabase: {
      input: {
        fields: ["params.animation_model", "params.style", "params.quality_check", "params.asset_urls", "params.image_api"],
        how: `Reads <code>animation_model</code>, <code>style</code>, <code>quality_check</code>, <code>asset_urls</code>, and <code>image_api</code>. Scene prompts and character data from memory.`,
      },
      output: {
        fields: ["intermediates.scene_images", "intermediates.scene_videos", "intermediates.asset_videos", "intermediates.music_url"],
        how: `Saves scene images, videos, processed assets, and music URL. Progress 35% &rarr; 75%.`,
      },
      progress: "35% &rarr; 75%",
    },
    example: `<span class="comment">// Parallel generation across 3 tracks</span>
<span class="key">Track 1 - Scenes (5 in parallel):</span>
  Scene 1: [Kie Image 15s] &rarr; [Kling 90s] &rarr; [Quality check] &#10003;
  Scene 2: [Kie Image 15s] &rarr; [Kling 90s] &rarr; [Quality check] &#10003;
  ...

<span class="key">Track 2 - Assets:</span>
  Asset 1: [salon-broll.mp4 &rarr; trim 3s] &#10003;

<span class="key">Track 3 - Music:</span>
  [Suno ~30s] &#10003;`,
    notes: `Same parallel structure as UGC but with configurable image_api. The image_api parameter lets the user choose between Nano Banana (default), Flash, or Gemini for image generation.`
  },
  {
    id: "7.5", num: "7.5", title: "Beat-Sync Trim", service: "Rendi / FFmpeg",
    category: "cat-combine", optional: true,
    description: `If <code>sound_sync_method = "beat_sync"</code> (default), trims each scene video to its exact VO-driven duration for frame-perfect sync. Runs in parallel across all scenes.`,
    inputs: [
      '<span class="val">scene_videos[]</span> &mdash; over-generated scene videos',
      '<span class="val">scene_durations[]</span> &mdash; exact durations from VO timestamps',
    ],
    outputs: [
      '<span class="val">trimmed_scene_videos[]</span> &mdash; trimmed scene videos',
    ],
    supabase: {
      input: {
        fields: ["params.sound_sync_method"],
        how: `Skipped if <code>sound_sync_method = "none"</code>. Videos and durations from memory.`,
      },
      output: {
        fields: ["intermediates.trimmed_scene_videos"],
        how: `Saves trimmed video URLs.`,
      },
      progress: "80%",
    },
    example: `<span class="comment">// Trim each scene to VO-driven duration</span>
Scene 1: exact=4.2s, generated=6s  <span class="comment">// trim 1.8s</span>
Scene 2: exact=5.1s, generated=6s  <span class="comment">// trim 0.9s</span>
Scene 3: exact=3.8s, generated=6s  <span class="comment">// trim 2.2s</span>
...`,
    notes: `Same as UGC beat-sync trim. Optional step controlled by sound_sync_method parameter.`
  },
  {
    id: 8, num: "8", title: "Concat + Audio Mix", service: "Rendi (FFmpeg cloud)",
    category: "cat-combine",
    description: `Concatenates all scene videos and asset clips with <strong>dissolve transitions</strong>, then mixes in VO audio and background music.

Asset clips are inserted between body scenes and the CTA, same spacing as UGC. Dissolve duration is configurable (default 0.4s).

<strong>Audio mix:</strong> VO at full volume + music at 20%.`,
    inputs: [
      '<span class="val">trimmed_scene_videos[]</span> (or scene_videos[] if no beat sync)',
      '<span class="val">asset_videos[]</span> &mdash; processed user assets',
      '<span class="val">vo_audio_url</span> &mdash; voice over MP3',
      '<span class="val">music_url</span> &mdash; background music MP3',
      '<span class="val">dissolve_seconds</span> &mdash; transition duration (default 0.4s)',
    ],
    outputs: [
      '<span class="val">concat_video_url</span> &mdash; video-only concatenation',
      '<span class="val">rendi_scene_voice_url</span> &mdash; video + VO + music',
    ],
    supabase: {
      input: {
        fields: ["params.dissolve_seconds"],
        how: `Reads <code>dissolve_seconds</code> (default 0.4). All clip URLs and audio from memory.`,
      },
      output: {
        fields: ["intermediates.concat_url", "intermediates.rendi_scene_voice_url"],
        how: `Saves concatenated and audio-mixed video URLs.`,
      },
      progress: "90%",
    },
    example: `<span class="comment">// Concat with dissolve transitions</span>
clips = [scene_1, scene_2, scene_3, asset_1, scene_4, cta_scene]
concat_video = rendi.concat(clips, dissolve=0.4s)

<span class="comment">// Audio mix: VO + music</span>
final = rendi.add_audio(concat_video, vo=1.0, music=0.2)`,
    notes: `Same concatenation approach as UGC with configurable dissolve. The CTA scene (logo + slogan) is generated as part of scene prompts and placed last.`
  },
  {
    id: 9, num: "9", title: "Add Subtitles", service: "ZapCap API",
    category: "cat-subtitle", optional: true,
    description: `ZapCap burns animated subtitles using ElevenLabs word timestamps. Skipped if <code>add_subtitles = false</code>.`,
    inputs: [
      '<span class="val">rendi_scene_voice_url</span> &mdash; assembled video with audio',
      '<span class="val">subtitle_language</span> &mdash; language code',
      '<span class="val">vo_word_segments[]</span> &mdash; word timestamps from ElevenLabs',
    ],
    outputs: [
      '<span class="val">subtitled_video_url</span> &mdash; video with burned-in subtitles',
    ],
    supabase: {
      input: {
        fields: ["params.add_subtitles", "params.language"],
        how: `Reads <code>add_subtitles</code> (default true) and <code>language</code>. Word segments from memory.`,
      },
      output: {
        fields: ["intermediates.subtitled_url"],
        how: `Saves subtitled video URL.`,
      },
      progress: "95%",
    },
    example: `<span class="comment">// Same ZapCap flow as UGC/Product</span>
<span class="key">video:</span> <span class="str">"https://storage.googleapis.com/.../ps_final.mp4"</span>
<span class="comment">// Subtitles appear at exact word timing from ElevenLabs</span>`,
    notes: `Identical to UGC/Product. Template ID configurable. Fails gracefully.`
  },
  {
    id: 10, num: "10", title: "Upload to Mux CDN", service: "Mux CDN",
    category: "cat-upload",
    description: `Uploads the final video to <strong>Mux CDN</strong> for HLS streaming and MP4 download. Job marked as <strong>completed</strong>.`,
    inputs: [
      '<span class="val">subtitled_video_url</span> (or rendi_scene_voice_url if no subtitles)',
      '<span class="val">vo_audio_url</span> &mdash; voice over audio',
      '<span class="val">music_url</span> &mdash; background music',
    ],
    outputs: [
      '<span class="val">final_stream_url</span> &mdash; HLS stream URL',
      '<span class="val">final_mp4_url</span> &mdash; direct MP4 download URL',
    ],
    supabase: {
      input: {
        fields: [],
        how: `No additional reads. Video URL from memory.`,
      },
      output: {
        fields: ["output.final_stream_url", "output.final_mp4_url", "output.final_playback_id", "output.final_asset_id", "output.vo_audio_url", "output.music_url", "output.concat_url"],
        how: `Saves all output URLs. Sets <code>status = 'completed'</code>, <code>progress = 100</code>.`,
      },
      progress: "95% &rarr; 100%",
    },
    example: `<span class="comment">// Same Mux upload as UGC/Product</span>
<span class="key">final_stream_url:</span> <span class="str">"https://stream.mux.com/abc123.m3u8"</span>
<span class="key">final_mp4_url:</span> <span class="str">"https://stream.mux.com/abc123/high.mp4"</span>`,
    notes: `Identical to UGC/Product Mux upload.`
  }
];

/**
 * Dropdown and option data for Video Studio wizard.
 * Mirrors api_pipeline/config/data_maps.json and monolith config where relevant.
 *
 * videoTypeConfig: per-type labels, hints, and which media sections to show.
 */
const StudioData = {
  /** Per video_type: prompt hint, step 4 (character/product) copy, and which blocks to show in Media step */
  videoTypeConfig: {
    "product video": {
      promptTitle: "Prompt",
      promptHint: "Main story and selling angle (headline, tone, CTA). Add product-specific facts in step 4 (product explanation).",
      productStep4Title: "Product photos & optional spokesperson",
      productStep4Desc:
        "Upload product images (recommended) and optional product explanation. Below: optional spokesperson only — leave empty for product-only ads. Logo and slogan are added in step 9.",
      mediaStepTitle: "Media",
      mediaStepHint: "Reference images, clips, logo. Product photos are in step 4.",
      showProductImages: true,
      showRefImages: true,
      showAssets: true,
      showCharacter: true,
      showLogoSlogan: true,
      refImagesLabel: "Reference images 1–5 (context for scenes)",
      assetsLabel: "Assets 1–3 (optional video clips)",
      characterLabel: "Character (optional; leave empty for product-only)",
      logoSloganLabel: "Logo & slogan (optional for end card)",
      characterBlockTitle: "Optional spokesperson / character",
      characterSourceUploadLabel: "Character image",
      characterSourceUploadSub: "Upload a reference in the slot(s) below.",
    },
    "influencer": {
      promptTitle: "Prompt",
      promptHint: "Write your influencer script or story: what they're promoting, key lines, and vibe. First-person, authentic tone. Gender above sets voice and appearance.",
      characterStepTitle: "Influencer photo (optional)",
      characterStepHint:
        "Upload a reference photo or describe the look (optional AI chips). Next moves on right away; an auto-portrait runs in the background if you do not upload. Approve the character on a later step. Reference images, clips, logo, and slogan are in step 9.",
      mediaStepTitle: "Media",
      mediaStepHint: "Reference images = locations/settings. Assets = clips to insert between scenes. Character = influencer photo (or leave empty to auto-generate). Logo + slogan = end card.",
      showProductImages: false,
      showRefImages: true,
      showAssets: true,
      showCharacter: true,
      showLogoSlogan: true,
      refImagesLabel: "Reference images 1–5 (locations / settings for scenes)",
      assetsLabel: "Assets 1–3 (video or image clips inserted between scenes)",
      characterLabel: "Influencer image (leave empty to auto-generate by gender)",
      logoSloganLabel: "Logo & slogan (for CTA / end card)",
      characterBlockTitle: "Influencer, logo & slogan",
      characterSourceUploadLabel: "Influencer image",
      characterSourceUploadSub: "Upload a reference in the slot(s) below.",
    },
    "personal-brand": {
      promptTitle: "Prompt",
      promptHint: "Describe your service or brand and the message. Professional, VO-first flow. The script will drive scene structure and visuals.",
      characterStepTitle: "Characters (up to 3, optional)",
      characterStepHint:
        "Upload photos or use the character look field (optional AI helper). Next continues immediately; portrait work runs in the background when applicable. Logo, slogan, reference images, and clips are in step 9.",
      mediaStepTitle: "Media",
      mediaStepHint: "Reference images and optional character. Logo and slogan for end card.",
      showProductImages: false,
      showRefImages: true,
      showAssets: true,
      showCharacter: true,
      showLogoSlogan: true,
      refImagesLabel: "Reference images 1–5",
      assetsLabel: "Assets 1–3 (optional clips)",
      characterLabel: "Character / spokesperson (leave empty to auto-generate)",
      logoSloganLabel: "Logo & slogan (for end card)",
      characterBlockTitle: "Character, logo & slogan",
      characterSourceUploadLabel: "Character image(s)",
      characterSourceUploadSub: "Upload up to three references in the slots below.",
    },
    "ugc-real": {
      promptTitle: "UGC Real brief",
      promptHint:
        "Describe your product or service, who it's for, the main problem it solves, and key selling points. Add creative direction — pacing, tone, setting, ad style.\n\nExample:\n\"We make an AI video generator for ecom founders. Most teams spend $2K+ per ad and wait weeks. Our tool ships UGC-style ads in hours with consistent characters. The audience is solo founders and small marketing teams. CTA: Start your free trial. Energetic talking-head style, home office setting, fast hooks then slower proof section.\"",
      characterStepTitle: "Character (photo, description, or both)",
      characterStepHint:
        "Upload a reference and/or describe the spokesperson. Next continues immediately; optional auto-portrait runs in the background. Product photos for physical offers are above. Extra refs and clips here; per-cell stills are Regenerate/Fix on the VO & grid step (wizard step 10 of 13).",
      mediaStepTitle: "Scene assets (skipped in UGC Real UI)",
      mediaStepHint:
        "UGC Real no longer uses this step in the wizard (13 steps total). Cell images and prompts live on the VO & grid step; after Background music you go straight to Final video.",
      showProductImages: true,
      showRefImages: true,
      showAssets: true,
      showCharacter: true,
      showLogoSlogan: true,
      refImagesLabel: "Reference images / screenshots",
      assetsLabel: "Assets (optional clips)",
      characterLabel: "Character (optional unless your concept needs one)",
      logoSloganLabel: "Logo & slogan (optional CTA)",
      characterBlockTitle: "Character and branding",
      characterSourceUploadLabel: "Character image",
      characterSourceUploadSub: "Upload a reference in the slot(s) below.",
    },
  },

  countries: [
    { value: "usa", label: "United States" },
    { value: "israel", label: "Israel" },
    { value: "uk", label: "United Kingdom" },
    { value: "germany", label: "Germany" },
    { value: "france", label: "France" },
    { value: "spain", label: "Spain" },
    { value: "italy", label: "Italy" },
    { value: "brazil", label: "Brazil" },
    { value: "japan", label: "Japan" },
    { value: "south korea", label: "South Korea" },
    { value: "china", label: "China" },
    { value: "india", label: "India" },
    { value: "turkey", label: "Turkey" },
    { value: "russia", label: "Russia" },
    { value: "poland", label: "Poland" },
    { value: "thailand", label: "Thailand" },
    { value: "vietnam", label: "Vietnam" },
    { value: "mexico", label: "Mexico" },
    { value: "colombia", label: "Colombia" },
    { value: "argentina", label: "Argentina" },
    { value: "uae", label: "UAE" },
    { value: "saudi arabia", label: "Saudi Arabia" },
    { value: "egypt", label: "Egypt" },
    { value: "morocco", label: "Morocco" },
    { value: "portugal", label: "Portugal" },
    { value: "netherlands", label: "Netherlands" },
    { value: "sweden", label: "Sweden" },
    { value: "norway", label: "Norway" },
    { value: "australia", label: "Australia" },
    { value: "canada", label: "Canada" },
    { value: "nigeria", label: "Nigeria" }
  ],

  languages: [
    { value: "en", label: "English" },
    { value: "he", label: "Hebrew" },
    { value: "ar", label: "Arabic" },
    { value: "de", label: "German" },
    { value: "es", label: "Spanish" },
    { value: "fr", label: "French" },
    { value: "it", label: "Italian" },
    { value: "pt", label: "Portuguese" },
    { value: "pt-BR", label: "Brazilian Portuguese" },
    { value: "ru", label: "Russian" },
    { value: "zh", label: "Chinese" },
    { value: "zh-CN", label: "Simplified Chinese" },
    { value: "ja", label: "Japanese" },
    { value: "ko", label: "Korean" },
    { value: "pl", label: "Polish" },
    { value: "tr", label: "Turkish" },
    { value: "hi", label: "Hindi" },
    { value: "th", label: "Thai" },
    { value: "vi", label: "Vietnamese" },
    { value: "id", label: "Indonesian" },
    { value: "nl", label: "Dutch" },
    { value: "sv", label: "Swedish" },
    { value: "no", label: "Norwegian" },
    { value: "da", label: "Danish" },
    { value: "fi", label: "Finnish" },
    { value: "el", label: "Greek" },
    { value: "cs", label: "Czech" },
    { value: "ro", label: "Romanian" },
    { value: "hu", label: "Hungarian" },
    { value: "uk", label: "Ukrainian" }
  ],

  styles: [
    { value: "Auto", label: "Auto", desc: "Adapts to video type." },
    { value: "Cinematic photography", label: "Cinematic photography", desc: "Dramatic lighting, film-like quality." },
    { value: "Modern flat 2d", label: "Modern flat 2D", desc: "Clean vector, bold colors." },
    { value: "Minimal line art", label: "Minimal line art", desc: "Elegant lines, limited palette." },
    { value: "Futuristic isometric Tech Glow", label: "Futuristic isometric Tech Glow", desc: "Neon, cyberpunk, isometric." },
    { value: "Modern semi flat 2d", label: "Modern semi flat 2D", desc: "Soft gradients, contemporary." },
    { value: "Soft 3d clay", label: "Soft 3D clay", desc: "Claymation, pastel, Pixar-like." },
    { value: "isometric soft vector", label: "Isometric soft vector", desc: "Pastel isometric." },
    { value: "Paper Cut", label: "Paper Cut", desc: "Layered paper aesthetic." }
  ],

  /** Hebrew: dedicated voices. Other languages: ElevenLabs premade multilingual (Rachel / Adam). */
  languageVoices: {
    he: {
      male: { id: "8Q33DcKUm1QhbjQ4bqpD", label: "Male (Hebrew)" },
      female: { id: "5K8bxz8WOmQy4pTCvp0q", label: "Female (Hebrew)" }
    },
    en: {
      male: { id: "pNInz6obpgDQGcFmaJgB", label: "Male (English, multilingual)" },
      female: { id: "21m00Tcm4TlvDq8ikWAM", label: "Female (English, multilingual)" }
    }
  },

  defaultVoices: {
    male: { id: "pNInz6obpgDQGcFmaJgB", label: "Male (default multilingual)" },
    female: { id: "21m00Tcm4TlvDq8ikWAM", label: "Female (default multilingual)" }
  },

  imageModels: [
    { value: "kie", label: "Nano Banana Pro (Kie.ai)" },
    { value: "google", label: "Gemini 3 Pro (Vertex AI)" },
    { value: "google-31-flash", label: "Gemini 3.1 Flash (Vertex AI)" },
    { value: "nano-banana-2", label: "Nano Banana 2 (Vertex AI)" },
    { value: "gemini-25-flash-image", label: "Gemini 2.5 Flash (Vertex AI)" },
    { value: "kie-flash", label: "Gemini 3 Flash (Kie.ai)" }
  ],

  animationModels: [
    { value: "auto", label: "Auto (from tier)" },
    { value: "google", label: "Veo 3.1 Fast (Vertex AI)" },
    { value: "kling", label: "Kling 2.5 (Kie.ai)" },
    { value: "runway", label: "Runway Gen4 Turbo (Kie.ai)" },
    { value: "none", label: "None" }
  ],

  /** ZapCap template IDs from zapcap.json */
  subtitleTemplates: [
    { value: "", label: "No subtitles" },
    { value: "your-zapcap-template-id", label: "Template 1" },
    { value: "50cdfac1-0a7a-48dd-af14-4d24971e213a", label: "Template 2" },
    { value: "55267be2-9eec-4d06-aff8-edcb401b112e", label: "Template 3" },
    { value: "7b946549-ae16-4085-9dd3-c20c82504daa", label: "Template 4" },
    { value: "982ad276-a76f-4d80-a4e2-b8fae0038464", label: "Template 5" },
    { value: "a51c5222-47a7-4c37-b052-7b9853d66bf6", label: "Template 6" },
    { value: "ca050348-e2d0-49a7-9c75-7a5e8335c67d", label: "Template 7" },
    { value: "d46bb0da-cce0-4507-909d-fa8904fb8ed7", label: "Template 8" },
    { value: "dfe027d9-bd9d-4e55-a94f-d57ed368a060", label: "Template 9" },
    { value: "e659ee0c-53bb-497e-869c-90f8ec0a921f", label: "Template 10" },
    { value: "d2018215-2125-41c1-940e-f13b411fff5c", label: "Template 11" },
    { value: "1c0c9b65-47c4-41bf-a187-25a8305fd0dd", label: "Template 12" },
    { value: "a104df87-5b1a-4490-8cca-62e504a84615", label: "Template 13" },
    { value: "6255949c-4a52-4255-8a67-39ebccfaa3ef", label: "Template 14" },
    { value: "a6760d82-72c1-4190-bfdb-7d9c908732f1", label: "Template 15" }
  ]
};

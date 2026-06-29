/**
 * Step definitions: validation, data collection, conditional visibility.
 * Depends on: data.js, upload.js (zones created by app.js).
 */
const StudioSteps = (function () {
  let refImageZones = [];
  let refImageExplains = [];
  let assetZones = [];
  let cleanProductZones = [];
  let characterZones = [];
  let logoZone = null;
  /** When no video-type card has .selected (DOM glitch / bad restore), keep last known type so wizard step routing stays correct. */
  let _lastKnownVideoType = 'product video';

  function registerMediaZones(opts) {
    refImageZones = opts.refImageZones || [];
    refImageExplains = opts.refImageExplains || [];
    assetZones = opts.assetZones || [];
    cleanProductZones = opts.cleanProductZones || [];
    characterZones = opts.characterZones || [];
    logoZone = opts.logoZone || null;
  }

  function getVideoType() {
    const card = document.querySelector('[data-video-type].selected');
    if (card && card.dataset.videoType) {
      _lastKnownVideoType = card.dataset.videoType;
      return card.dataset.videoType;
    }
    return _lastKnownVideoType || 'product video';
  }

  /**
   * After session restore: ensure exactly one video-type card is selected so getVideoType() matches influencer/product/etc.
   */
  function syncVideoTypeFromSession(session) {
    const sel = document.querySelector('[data-video-type].selected');
    if (sel && sel.dataset.videoType) {
      _lastKnownVideoType = sel.dataset.videoType;
      return;
    }
    const vtRaw =
      (session && session.formSnapshot && session.formSnapshot.video_type) ||
      (session && session.videoType) ||
      '';
    const vt = typeof vtRaw === 'string' ? vtRaw.trim() : '';
    const pickCard = (typeKey) =>
      typeKey ? document.querySelector('[data-video-type="' + typeKey + '"]') : null;
    let card = vt ? pickCard(vt) : null;
    if (!card && vt) {
      const lower = vt.toLowerCase();
      card = pickCard(lower);
    }
    if (!card) {
      card = pickCard('product video');
      _lastKnownVideoType = 'product video';
    } else {
      _lastKnownVideoType = card.dataset.videoType || vt || 'product video';
    }
    if (card) {
      document.querySelectorAll('[data-video-type]').forEach((c) => c.classList.remove('selected'));
      card.classList.add('selected');
    }
  }

  function getLanguage() {
    const sel = document.getElementById('language');
    return sel ? sel.value : 'en';
  }

  /** Split TEXT 3 into key_benefits and cta_text (line must contain "CTA:"). */
  function parseUgcOfferText3(raw) {
    const t = (raw || '').trim();
    if (!t) return { key_benefits: '', cta_text: '' };
    const re = /(?:^|\n)\s*CTA\s*:\s*(.+)$/is;
    const m = t.match(re);
    if (m) {
      const lead = t.slice(0, m.index).trim();
      const cta = (m[1] || '').trim();
      return { key_benefits: lead, cta_text: cta };
    }
    return { key_benefits: t, cta_text: '' };
  }

  function getGender() {
    const btn = document.querySelector('.studio-gender-toggle button.active');
    return btn ? btn.dataset.gender : 'f';
  }

  function isUgcRealFlow() {
    return getVideoType() === 'ugc-real';
  }

  function isProductNoOnScreenCharacter() {
    if (getVideoType() !== 'product video') return false;
    const el = document.getElementById('productNoOnScreenCharacter');
    return !!(el && el.checked);
  }

  /** Clear character upload slots, brief, and hidden portrait prompt (Product Video: "No on-screen character"). */
  function clearCharacterSlotsAndBrief() {
    (characterZones || []).forEach((z) => {
      if (z.clear) z.clear();
      else if (z.setUrl) z.setUrl(null);
    });
    const ta = document.getElementById('characterBrief');
    if (ta) ta.value = '';
    const ph = document.getElementById('portraitImagePromptHidden');
    if (ph) ph.value = '';
  }

  /** Slots 2–3 are only used for personal-brand; ignore their URLs for other types so hidden zones do not false-trigger conflicts. */
  function activeCharacterSlotIndices() {
    const vt = getVideoType();
    if (vt === 'personal-brand') return [0, 1, 2];
    return [0];
  }

  function countFilledActiveCharacterSlots() {
    const zones = characterZones || [];
    let n = 0;
    activeCharacterSlotIndices().forEach((i) => {
      const z = zones[i];
      if (z && z.getUrl && z.getUrl()) n += 1;
    });
    return n;
  }

  function _clearCharacterBriefAndHidden() {
    const ta = document.getElementById('characterBrief');
    if (ta) ta.value = '';
    const ph = document.getElementById('portraitImagePromptHidden');
    if (ph) ph.value = '';
  }

  function _clearCharacterLibrarySelect() {
    const libEl = document.getElementById('characterLibrarySelect');
    if (libEl) libEl.value = '';
  }

  function _clearAllCharacterZones() {
    (characterZones || []).forEach((z) => {
      if (z.clear) z.clear();
      else if (z.setUrl) z.setUrl(null);
    });
  }

  function getStep4CharacterSourceMode() {
    const r = document.querySelector('input[name="characterSourceMode"]:checked');
    return r && r.value ? String(r.value).trim() : '';
  }

  function setCharacterSourceMode(mode) {
    const next = mode && String(mode).trim() ? String(mode).trim() : '';
    const prev = typeof document.body.dataset.studioCharacterSourceMode === 'string'
      ? document.body.dataset.studioCharacterSourceMode
      : '';
    document.body.dataset.studioCharacterSourceMode = next;
    if (!next) {
      document.querySelectorAll('input[name="characterSourceMode"]').forEach((inp) => { inp.checked = false; });
      applyStep4CharacterSourceUI();
      return;
    }
    const radio = document.querySelector('input[name="characterSourceMode"][value="' + next + '"]');
    if (radio) radio.checked = true;
    if (prev && prev !== next) {
      if (next === 'look') {
        _clearCharacterLibrarySelect();
        _clearAllCharacterZones();
        const ph = document.getElementById('portraitImagePromptHidden');
        if (ph) ph.value = '';
      } else if (next === 'library') {
        _clearCharacterBriefAndHidden();
        _clearAllCharacterZones();
      } else if (next === 'upload') {
        _clearCharacterLibrarySelect();
        _clearCharacterBriefAndHidden();
        _clearAllCharacterZones();
      }
    }
    applyStep4CharacterSourceUI();
  }

  function _inferCharacterSourceModeFromSnapshot(snap) {
    if (!snap || typeof snap !== 'object') return '';
    if (snap.character_source_mode && String(snap.character_source_mode).trim()) {
      return String(snap.character_source_mode).trim();
    }
    if (snap.character_library_select && String(snap.character_library_select).trim()) return 'library';
    const urls = snap.character_urls;
    if (Array.isArray(urls) && urls.some(Boolean)) return 'upload';
    const b = (snap.character_brief && String(snap.character_brief).trim()) || '';
    const p = (snap.portrait_image_prompt && String(snap.portrait_image_prompt).trim()) || '';
    if (b || p) return 'look';
    return '';
  }

  function applyStep4CharacterSourceUI() {
    try {
      const vt = getVideoType();
      const config = (StudioData.videoTypeConfig && StudioData.videoTypeConfig[vt]) || StudioData.videoTypeConfig['product video'] || {};
      const modeWrap = document.getElementById('characterSourceModeWrap');
      const lookSec = document.getElementById('characterLookSection');
      const libSec = document.getElementById('characterLibrarySection');
      const upSec = document.getElementById('characterUploadSection');
      const uploadLbl = document.getElementById('characterSourceUploadLabelStrong');
      const uploadSub = document.getElementById('characterSourceUploadSub');
      const hintEl = document.getElementById('characterSourceModeHint');
      if (hintEl) {
        hintEl.style.display = 'none';
        hintEl.textContent = '';
      }
      if (uploadLbl && config.characterSourceUploadLabel) uploadLbl.textContent = config.characterSourceUploadLabel;
      if (uploadSub && config.characterSourceUploadSub) uploadSub.textContent = config.characterSourceUploadSub;

      const hideAllCharacterParts = !config.showCharacter || isProductNoOnScreenCharacter();
      if (hideAllCharacterParts) {
        if (modeWrap) modeWrap.style.display = 'none';
        if (lookSec) lookSec.style.display = 'none';
        if (libSec) libSec.style.display = 'none';
        if (upSec) upSec.style.display = 'none';
        const briefWrap = document.getElementById('characterBriefWrap');
        const pnw = document.getElementById('productNoCharacterWrap');
        if (briefWrap && (vt === 'ugc-real' || !isProductNoOnScreenCharacter())) {
          briefWrap.style.display = hideAllCharacterParts ? 'none' : '';
        }
        return;
      }

      if (vt === 'ugc-real') {
        if (modeWrap) modeWrap.style.display = 'none';
        if (lookSec) lookSec.style.display = '';
        const briefWrapU = document.getElementById('characterBriefWrap');
        if (briefWrapU) briefWrapU.style.display = config.showCharacter !== false ? '' : 'none';
        if (libSec) libSec.style.display = '';
        if (upSec) upSec.style.display = '';
        const ch2u = document.getElementById('mediaCharacter2Wrap');
        const ch3u = document.getElementById('mediaCharacter3Wrap');
        if (ch2u) ch2u.style.display = 'none';
        if (ch3u) ch3u.style.display = 'none';
        return;
      }

      if (modeWrap) modeWrap.style.display = '';
      const mode = getStep4CharacterSourceMode();
      if (lookSec) lookSec.style.display = mode === 'look' ? '' : 'none';
      if (libSec) libSec.style.display = mode === 'library' ? '' : 'none';
      const briefWrap = document.getElementById('characterBriefWrap');
      if (briefWrap) briefWrap.style.display = mode === 'look' ? '' : 'none';

      if (upSec) {
        if (mode === 'upload') upSec.style.display = '';
        else if (mode === 'library') upSec.style.display = '';
        else upSec.style.display = 'none';
      }
      const ch2 = document.getElementById('mediaCharacter2Wrap');
      const ch3 = document.getElementById('mediaCharacter3Wrap');
      const showMulti = mode === 'upload' && vt === 'personal-brand';
      if (ch2) ch2.style.display = showMulti ? '' : 'none';
      if (ch3) ch3.style.display = showMulti ? '' : 'none';

      const libLw = document.getElementById('characterLibraryWrap');
      if (mode === 'library') {
        if (libLw) libLw.style.display = '';
      }
    } finally {
      try {
        if (typeof document !== 'undefined') {
          document.dispatchEvent(new CustomEvent('studio:applyStep4CharacterSourceUI'));
        }
      } catch (eEv) {}
    }
  }

  /** Optional conflict hint (e.g. extra slots) — rarely shown after mode switching. */
  function getStep4CharacterExclusiveError() {
    if (getVideoType() === 'ugc-real') return null;
    if (isProductNoOnScreenCharacter()) return null;
    const mode = getStep4CharacterSourceMode();
    if (mode !== 'library') return null;
    const libEl = document.getElementById('characterLibrarySelect');
    const libSel = libEl && libEl.value ? String(libEl.value).trim() : '';
    if (!libSel) return null;
    const vt = getVideoType();
    if (vt !== 'personal-brand' && countFilledActiveCharacterSlots() > 1) {
      return 'Saved character uses the main slot only. Clear extra character images or pick a different source.';
    }
    return null;
  }

  function refreshStep4CharacterExclusiveHint() {
    const el = document.getElementById('characterSourceModeHint');
    if (!el) return;
    if (getVideoType() === 'ugc-real' || isProductNoOnScreenCharacter()) {
      el.style.display = 'none';
      el.textContent = '';
      return;
    }
    const msg = getStep4CharacterExclusiveError();
    if (msg) {
      el.style.display = 'block';
      el.textContent = msg;
    } else {
      el.style.display = 'none';
      el.textContent = '';
    }
  }

  function getStep4SourceValidationError() {
    if (getVideoType() === 'ugc-real') return null;
    if (isProductNoOnScreenCharacter()) return null;
    const mode = getStep4CharacterSourceMode();
    if (!mode) {
      return 'Choose a character source: Character look, Saved characters, or the reference image option.';
    }
    if (mode === 'library') {
      const libEl = document.getElementById('characterLibrarySelect');
      const libSel = libEl && libEl.value ? String(libEl.value).trim() : '';
      if (!libSel) return 'Select a saved character from the list (or pick another source).';
    }
    if (mode === 'upload') {
      if (countFilledActiveCharacterSlots() < 1) {
        return 'Upload at least one character image in the slot below (or pick another source).';
      }
    }
    return getStep4CharacterExclusiveError();
  }

  function validateStep(stepNum) {
    if (stepNum === 3) {
      const prompt = (document.getElementById('prompt') || {}).value || '';
      if (!prompt.trim()) return false;
      return true;
    }
    // UGC Real: creator-led / prompt-first — assets are optional until the pipeline infers offer type.
    // Do not block step 4→5 when no uploads (matches monolith service-style runs).
    if (stepNum === 4 && getVideoType() === 'ugc-real') {
      return true;
    }
    // Non–UGC Real: require an explicit confirm checkbox on step 4 (upload, saved library, or character look / auto path).
    if (stepNum === 4 && getVideoType() !== 'ugc-real') {
      if (isProductNoOnScreenCharacter()) {
        return true;
      }
      const srcErr = getStep4SourceValidationError();
      if (srcErr) return false;
      return true;
    }
    if (stepNum === 9 && getVideoType() === 'product video') {
      const vo = (document.getElementById('voScript') || {}).value || '';
      if (!vo.trim() || vo.trim().length < 12) return false;
      return true;
    }
    if (stepNum === 10 && getVideoType() === 'ugc-real') {
      const vo = (document.getElementById('voScript') || {}).value || '';
      if (!vo.trim() || vo.trim().length < 12) return false;
      return true;
    }
    return true;
  }

  /**
   * Collect payload for POST /api/generate from steps 1-5 (input through models / Start generation).
   */
  function collectGeneratePayload() {
    const videoType = getVideoType();
    const payload = {
      video_type: videoType,
      prompt: (document.getElementById('prompt') || {}).value || '',
      duration: (function() {
        var activeBtn = document.querySelector('#durationOptions button.active');
        if (activeBtn && activeBtn.dataset.duration) return parseInt(activeBtn.dataset.duration, 10);
        var inp = document.getElementById('duration');
        return parseInt((inp || {}).value || '20', 10) || 20;
      })(),
      style: (document.getElementById('style') || {}).value || 'Auto',
      language: getLanguage(),
      country: (document.getElementById('country') || {}).value || '',
      gender: getGender(),
      output_resolution: '720p_low',
      add_subtitles: (document.getElementById('addSubtitles') && document.getElementById('addSubtitles').checked),
      generate_vo: true,
      business_name: videoType === 'influencer' ? 'Studio' : undefined
    };

    if (videoType !== 'ugc-real') {
      const videoRefUrl = (document.getElementById('videoReferenceUrl') || {}).value || '';
      if (videoRefUrl.trim()) payload.video_reference_url = videoRefUrl.trim();

      const voPreWritten = (document.getElementById('voPreWritten') || {}).value || '';
      if (voPreWritten.trim()) {
        payload.vo_script = voPreWritten.trim();
      }
    }

    const text1 = (document.getElementById('text1') || {}).value || '';
    const text2 = (document.getElementById('text2') || {}).value || '';
    const text3 = (document.getElementById('text3') || {}).value || '';
    if (text1 || text2 || text3) {
      payload.text_1 = text1;
      payload.text_2 = text2;
      payload.text_3 = text3;
    }

    const characterBrief = (document.getElementById('characterBrief') || {}).value || '';
    const productNoChar = videoType === 'product video' && isProductNoOnScreenCharacter();
    if (productNoChar) {
      payload.product_no_on_screen_character = true;
    } else if (characterBrief.trim()) {
      payload.character_description = characterBrief.trim();
    }

    const voiceCustom = (document.getElementById('voiceIdCustom') || {}).value || '';
    const voiceSelect = document.getElementById('voiceId');
    /* VO-step voice controls (step 7 / step 8vo) — override precedence:
       1. Manual paste in step 5 (voiceIdCustom)
       2. Manual paste in VO step (voVoiceIdManual)
       3. Designed-voice selection (voEffectiveVoiceId — written by app.js after /api/voice-save)
       4. Static/dynamic dropdown (voiceId / voVoiceSelect)
       NOTE: voEffectiveVoiceId is ONLY written after the voice is saved to ElevenLabs library
       (save_designed_voice returns a permanent voice_id). Generated voice IDs (temporary, from
       /api/voice-design) are NOT valid for TTS and are never stored in voEffectiveVoiceId. */
    const voVoiceManual = (document.getElementById('voVoiceIdManual') || {}).value || '';
    const voEffectiveId = (document.getElementById('voEffectiveVoiceId') || {}).value || '';
    const voVoiceSelect = document.getElementById('voVoiceSelect');
    if (voiceCustom.trim()) payload.voice_id = voiceCustom.trim();
    else if (voVoiceManual.trim()) payload.voice_id = voVoiceManual.trim();
    else if (voEffectiveId.trim()) payload.voice_id = voEffectiveId.trim();
    else if (voiceSelect && voiceSelect.value) payload.voice_id = voiceSelect.value;
    else if (voVoiceSelect && voVoiceSelect.value) payload.voice_id = voVoiceSelect.value;

    payload.slogan_text = (document.getElementById('slogan') || {}).value || '';

    const refUrls = refImageZones.map(z => z.getUrl && z.getUrl()).filter(Boolean);
    if (refUrls.length) payload.reference_image_urls = refUrls;

    const refExplains = refImageExplains.map(function (inp) { return (inp && inp.value || '').trim(); }).filter(Boolean);
    if (refExplains.length) payload.reference_image_explains = refExplains;

    const assetUrls = assetZones.map(z => z.getUrl && z.getUrl()).filter(Boolean);
    if (assetUrls.length) payload.asset_urls = assetUrls.map(url => ({ url }));

    const charUrls = characterZones.map(z => z.getUrl && z.getUrl()).filter(Boolean);
    if (!productNoChar) {
      if (charUrls.length === 1) payload.character_url = charUrls[0];
      else if (charUrls.length > 1) payload.character_urls = charUrls;
    }

    if (logoZone && logoZone.getUrl) {
      const url = logoZone.getUrl();
      if (url) payload.logo_url = url;
    }

    if (videoType === 'product video') {
      const productUrls = cleanProductZones.map(z => z.getUrl && z.getUrl()).filter(Boolean);
      if (productUrls.length) {
        payload.product_image_urls = productUrls.slice(0, 1);
        if (productUrls.length > 1) payload.clean_product_image_urls = productUrls.slice(1);
      }
      const productExplain = (document.getElementById('productExplain') || {}).value || '';
      if (productExplain) payload.product_explain = productExplain;
    } else if (videoType === 'ugc-real') {
      // Same product slots as product video — monolith + Kie need URLs for grid + physical_product validation.
      const productUrlsUgc = cleanProductZones.map(z => z.getUrl && z.getUrl()).filter(Boolean);
      if (productUrlsUgc.length) {
        payload.product_image_urls = productUrlsUgc.slice(0, 1);
        if (productUrlsUgc.length > 1) payload.clean_product_image_urls = productUrlsUgc.slice(1);
      }
    }

    const imageModel = (document.getElementById('imageModel') || {}).value;
    if (imageModel) payload.image_api = imageModel;
    const animModel = (document.getElementById('animationModel') || {}).value;
    if (animModel && animModel !== 'auto') payload.animation_model = animModel;

    const subtitleTemplateEl = document.getElementById('subtitleTemplate');
    if (subtitleTemplateEl && subtitleTemplateEl.value) payload.subtitle_template = subtitleTemplateEl.value;
    payload.subtitle_position = (document.getElementById('subtitlePosition') || {}).value || 'middle';
    payload.subtitle_emoji = (document.getElementById('subtitleEmoji') || {}).value !== 'false';

    const simCheckbox = document.getElementById('simulationMode');
    if (simCheckbox && simCheckbox.checked) {
      payload.simulation = true;
      payload.simulation_type = 'wrapper';
      payload.simulation_duration = '15s';
    } else {
      payload.simulation = false; // real pipeline: VO and all outputs are actually generated
    }

    return payload;
  }

  /**
   * Phase 1 (Job A): Parse prompt → TEXT 1/2/3, then (product video + product images) clean product image.
   * Pause after step_1 if there are no product images yet; pause after step_2 when images exist so
   * "clean product" runs right after the product-photo step, not during Generate VO.
   */
  function collectPhase1Payload() {
    const p = collectGeneratePayload();
    if (p.video_type === 'ugc-real') {
      // Stop after brief parse so the user can edit the three offer fields before nine-cell + grid work.
      p.pause_after_step = 'step_parse';
      return p;
    }
    const productUrls = cleanProductZones.map((z) => z.getUrl && z.getUrl()).filter(Boolean);
    if (p.video_type === 'product video' && productUrls.length > 0) {
      p.pause_after_step = 'step_2';
    } else {
      p.pause_after_step = 'step_1';
    }
    return p;
  }

  /**
   * Phase 2 (Job B): Seed from Job A, inject text_1/2/3 from UI, pause after VO (step_2.7).
   */
  function collectPhase2Payload(seedJobId) {
    const p = collectGeneratePayload();
    p.seed_job_id = seedJobId;
    if (p.video_type === 'ugc-real') {
      p.pause_after_step = 'step_5';
      p.text_1 = (document.getElementById('text1') || {}).value || '';
      p.text_2 = (document.getElementById('text2') || {}).value || '';
      p.text_3 = (document.getElementById('text3') || {}).value || '';
      const parsed = parseUgcOfferText3(p.text_3);
      if (parsed.key_benefits.trim()) p.key_benefits = parsed.key_benefits.trim();
      if (parsed.cta_text.trim()) p.cta_text = parsed.cta_text.trim();
      return p;
    }
    p.pause_after_step = 'step_2.7';
    p.text_1 = (document.getElementById('text1') || {}).value || '';
    p.text_2 = (document.getElementById('text2') || {}).value || '';
    p.text_3 = (document.getElementById('text3') || {}).value || '';
    return p;
  }

  /**
   * Phase 3 (Job C): Seed from Job B, run to end (scene prompts → images → animate → final).
   */
  function collectPhase3Payload(seedJobId) {
    const p = collectGeneratePayload();
    p.seed_job_id = seedJobId;
    if (p.video_type === 'ugc-real') {
      p.pause_after_step = 'step_8';
    }
    return p;
  }

  /**
   * Phase 3a: Seed from Job B, pause after step_3 (scene prompts only).
   */
  function collectPhase3PauseScenePromptsPayload(seedJobId) {
    const p = collectGeneratePayload();
    p.seed_job_id = seedJobId;
    if (p.video_type === 'ugc-real') {
      p.pause_after_step = 'step_5';
      return p;
    }
    p.pause_after_step = 'step_3';
    return p;
  }

  /**
   * Payload for POST /api/generate-music.
   */
  function collectMusicPayload() {
    const videoType = getVideoType();
    const payload = {
      text_1: (document.getElementById('text1') || {}).value || '',
      text_2: (document.getElementById('text2') || {}).value || '',
      text_3: (document.getElementById('text3') || {}).value || '',
      vo_script: (document.getElementById('voScript') || {}).value || '',
      language: getLanguage(),
      video_type: videoType
    };
    const musicDescEl = document.getElementById('musicDescription');
    if (musicDescEl && musicDescEl.value && musicDescEl.value.trim()) {
      payload.music_description_override = musicDescEl.value.trim();
    }
    return payload;
  }

  /**
   * Payload for POST /api/generate-scene-image for one scene.
   * @param {number} sceneIndex - 0-based scene index
   * @param {string} imagePrompt - first_prompt (or second_prompt) for the scene
   * @param {string} [correctionText] - user correction to prepend (or, with imageToFixUrl, the fix instructions)
   * @param {boolean} isLastScene - is CTA scene
   * @param {string} [imageToFixUrl] - when set (Fix this image), send this image as reference with correction_text
   */
  function collectSceneImagePayload(sceneIndex, imagePrompt, correctionText, isLastScene, imageToFixUrl) {
    const videoType = getVideoType();
    const productNoChar = videoType === 'product video' && isProductNoOnScreenCharacter();
    const refUrls = refImageZones.map(z => z.getUrl && z.getUrl()).filter(Boolean);
    const charUrls = characterZones.map(z => z.getUrl && z.getUrl()).filter(Boolean);
    let logoUrl = null;
    if (logoZone && logoZone.getUrl) logoUrl = logoZone.getUrl();
    const productUrls = cleanProductZones.map(z => z.getUrl && z.getUrl()).filter(Boolean);
    const text1 = (document.getElementById('text1') || {}).value || '';
    const payload = {
      image_prompt: imagePrompt || '',
      correction_text: correctionText && correctionText.trim() ? correctionText.trim() : undefined,
      image_to_fix_url: imageToFixUrl && imageToFixUrl.trim() ? imageToFixUrl.trim() : undefined,
      visual_style: (document.getElementById('style') || {}).value || 'Auto',
      video_type: videoType,
      image_api: (document.getElementById('imageModel') || {}).value || 'kie',
      reference_image_urls: refUrls.length ? refUrls : (productUrls.length ? productUrls : undefined),
      character_reference_urls: productNoChar ? undefined : (charUrls.length ? charUrls : undefined),
      has_character: productNoChar ? false : (charUrls.length > 0),
      product_description: text1 || undefined,
      is_cta_scene: !!isLastScene,
      logo_reference_url: isLastScene && logoUrl ? logoUrl : undefined
    };
    return payload;
  }

  function updateVisibilityForVideoType() {
    const vt = getVideoType();
    const config = (StudioData.videoTypeConfig && StudioData.videoTypeConfig[vt]) || StudioData.videoTypeConfig['product video'] || {};

    const promptDesc = document.getElementById('promptDesc');
    if (promptDesc && config.promptHint) {
      promptDesc.textContent = config.promptHint;
      promptDesc.style.whiteSpace = vt === 'ugc-real' ? 'pre-wrap' : '';
    }
    const step3PromptTitle = document.getElementById('step3PromptTitle');
    if (step3PromptTitle && config.promptTitle) step3PromptTitle.textContent = config.promptTitle;

    // Step 4: product = product photos first; UGC types = character-focused
    const step4TitleEl = document.getElementById('step4Title');
    const step4DescEl = document.getElementById('step4Desc');
    if (vt === 'product video') {
      if (step4TitleEl && config.productStep4Title) step4TitleEl.textContent = config.productStep4Title;
      if (step4DescEl && config.productStep4Desc) step4DescEl.textContent = config.productStep4Desc;
    } else {
      if (step4TitleEl && config.characterStepTitle) step4TitleEl.textContent = config.characterStepTitle;
      if (step4DescEl && config.characterStepHint) step4DescEl.textContent = config.characterStepHint;
    }

    // Step 10 HTML / Scene assets (wizard step 10 for product, 12 for UGC Real)
    const step9Title = document.getElementById('step9Title');
    if (step9Title && config.mediaStepTitle) step9Title.textContent = config.mediaStepTitle;
    const step9Desc = document.getElementById('step9Desc');
    if (step9Desc && config.mediaStepHint) step9Desc.textContent = config.mediaStepHint;

    const ugcSceneAssetsFlow = document.getElementById('ugcRealSceneAssetsFlowHint');
    const step10Phase3Hint = document.getElementById('step10assetsProductPhase3Hint');
    const btnScenePrompts = document.getElementById('btnGenerateScenePrompts');
    const btnGenImagesPromptsStep = document.getElementById('btnGenerateImages');
    if (vt === 'ugc-real') {
      if (ugcSceneAssetsFlow) ugcSceneAssetsFlow.style.display = 'block';
      if (step10Phase3Hint) step10Phase3Hint.style.display = 'none';
      if (btnScenePrompts) btnScenePrompts.style.display = 'none';
      if (btnGenImagesPromptsStep) btnGenImagesPromptsStep.style.display = 'none';
    } else {
      if (ugcSceneAssetsFlow) ugcSceneAssetsFlow.style.display = 'none';
      if (step10Phase3Hint) step10Phase3Hint.style.display = '';
      if (btnScenePrompts) btnScenePrompts.style.display = '';
      if (btnGenImagesPromptsStep) btnGenImagesPromptsStep.style.display = '';
    }

    const videoRefWrap = document.getElementById('videoReferenceUrlWrap');
    if (videoRefWrap) videoRefWrap.style.display = vt === 'ugc-real' ? 'none' : 'block';
    const voPreWrap = document.getElementById('voPreWrittenWrap');
    if (voPreWrap) voPreWrap.style.display = vt === 'ugc-real' ? 'none' : 'block';

    const productOnly = document.getElementById('mediaProductOnly');
    if (productOnly) {
      productOnly.style.display = config.showProductImages !== false ? 'block' : 'none';
    }

    const peWrap = document.getElementById('productExplainWrap');
    if (peWrap) peWrap.style.display = vt === 'ugc-real' ? 'none' : 'block';

    const pHint = document.getElementById('productStep4Hint');
    if (pHint) {
      if (vt === 'ugc-real') {
        pHint.textContent =
          'Upload 1–3 clear photos of your product (plain background and good lighting work best). Optional extra refs: you can add more on step 4 any time; per-cell stills are edited on the VO & grid step (step 10 of 13).';
      } else {
        pHint.innerHTML =
          'Upload 1–3 photos of your product (recommended). After you continue to step 4/5 with a prompt set, the pipeline starts here: Headline/Key message/CTA first, then <strong>clean product image</strong> — so progress may show <code>clean_product_image</code> on this part of the wizard, not on the voiceover step.';
      }
    }

    const refBlock = document.getElementById('mediaRefImagesBlock');
    if (refBlock) {
      refBlock.style.display = config.showRefImages !== false ? 'block' : 'none';
    }
    const refTitle = document.getElementById('refImagesBlockTitle');
    if (refTitle && config.refImagesLabel) refTitle.textContent = config.refImagesLabel;

    const assetsBlock = document.getElementById('mediaAssetsBlock');
    if (assetsBlock) assetsBlock.style.display = config.showAssets !== false ? 'block' : 'none';
    const assetsTitle = document.getElementById('assetsBlockTitle');
    if (assetsTitle && config.assetsLabel) assetsTitle.textContent = config.assetsLabel;

    const charBlock = document.getElementById('mediaCharacterBlock');
    if (charBlock) charBlock.style.display = config.showCharacter !== false ? 'block' : 'none';
    const pnw = document.getElementById('productNoCharacterWrap');
    if (pnw) {
      if (vt === 'product video') {
        pnw.style.display = '';
      } else {
        pnw.style.display = 'none';
        const cb = document.getElementById('productNoOnScreenCharacter');
        if (cb) cb.checked = false;
      }
    }
    const charLabel1 = document.getElementById('characterLabel1');
    if (charLabel1 && config.characterLabel) charLabel1.textContent = config.characterLabel;
    const charBlockTitle = document.getElementById('characterBlockTitle');
    if (charBlockTitle && config.characterBlockTitle) charBlockTitle.textContent = config.characterBlockTitle;

    const nonUgcCharTitle = document.getElementById('nonUgcCharGateTitle');
    const nonUgcCharDesc = document.getElementById('nonUgcCharGateDesc');
    if (nonUgcCharTitle && nonUgcCharDesc && vt !== 'ugc-real') {
      nonUgcCharTitle.textContent = 'Character approval';
      if (vt === 'product video') {
        nonUgcCharDesc.innerHTML =
          'Approve or regenerate the optional on-screen spokesperson portrait. <strong>While you are on this step,</strong> the pipeline can continue toward scene prompts and the rest of Phase 3. Use <strong>Next</strong> when the portrait is ready.';
      } else if (vt === 'personal-brand') {
        nonUgcCharDesc.innerHTML =
          'Approve the auto-generated portrait or regenerate with a short correction. <strong>While you are on this step,</strong> the server runs <strong>Phase 2</strong> in the background to generate the <strong>VO script</strong> — open the next step when you are ready to edit voice and audio.';
      } else {
        nonUgcCharDesc.innerHTML =
          'Approve the influencer portrait (or regenerate with a short correction). <strong>While you are on this step,</strong> the server runs <strong>Phase 2</strong> in the background to generate the <strong>VO script</strong> — open the next step when you are ready to edit voice and audio.';
      }
    }

    const step6Title = document.querySelector('#step7prefs .studio-step-title');
    const step6Desc = document.querySelector('#step7prefs .studio-step-desc');
    const step7Title = document.querySelector('#step8vo .studio-step-title');
    const step7Desc = document.querySelector('#step8vo .studio-step-desc');
    const ugcNineTitle = document.getElementById('ugcNineCellStepTitle');
    const ugcNineDesc = document.getElementById('ugcNineCellStepDesc');
    const musicStepDesc = document.getElementById('musicStepDesc');
    const MUSIC_STEP_DESC_DEFAULT =
      'Edit the music description if needed, then generate or regenerate. Approve when ready. On <strong>product video</strong>, music often started automatically while you were on Preferences (TEXT 1–3) — the player below should already show a track when Suno finishes.';
    const MUSIC_STEP_DESC_UGC_REAL =
      'Wizard <strong>step 11 of 16</strong>. Generate or regenerate Suno music if you want a track before final assembly. <strong>Next</strong> opens <strong>Scene assets</strong> (step 12) — optional extra product/UI shots; your nine-cell grid already defined the main visuals.';
    if (musicStepDesc) {
      musicStepDesc.innerHTML = vt === 'ugc-real' ? MUSIC_STEP_DESC_UGC_REAL : MUSIC_STEP_DESC_DEFAULT;
    }

    const scenePromptsStepDesc = document.getElementById('scenePromptsStepDesc');
    const SCENE_PROMPTS_DESC_DEFAULT =
      'Review and edit the generated scene prompts. Click <strong>Generate images</strong> to call the API and open the scene images step (your server then talks to Kie or Vertex). Use <strong>Back</strong> on that step to return here if needed.';
    const SCENE_PROMPTS_DESC_UGC_REAL =
      'This screen is skipped in the UGC Real wizard (steps merged into VO & grid). If you see it, switch video type or reload — you should use step 10 for cell prompts and images.';
    if (scenePromptsStepDesc) {
      scenePromptsStepDesc.innerHTML = vt === 'ugc-real' ? SCENE_PROMPTS_DESC_UGC_REAL : SCENE_PROMPTS_DESC_DEFAULT;
    }

    if (vt === 'ugc-real') {
      if (step6Title) step6Title.textContent = 'UGC Real — Offer text';
      if (step6Desc) {
        step6Desc.textContent =
          'This step is only the three offer fields (audience, problem, benefits + CTA). Approve them first with the primary button below. The server then uses your main prompt (step 3) plus these three texts to build the nine-cell plan: one image prompt and one VO line per grid cell. After that pause, you review the storyboard card; then you continue for the single 3×3 master grid. If you change the three fields after cells exist, use Regenerate nine-cell plan.';
      }
      if (step7Title) step7Title.textContent = 'UGC Real — VO & grid';
      if (step7Desc) {
        step7Desc.textContent =
          'Your voiceover script appears here when the pipeline saves it. After the 3×3 master grid is generated, review full-size cell images below — use Regenerate or Fix per cell — then approve and continue to Background music → Final video.';
      }
      if (ugcNineTitle) ugcNineTitle.textContent = 'Nine-cell storyboard';
      if (ugcNineDesc) {
        ugcNineDesc.textContent =
          'Built from your main prompt (step 3) and the three offer fields. While Phase 1 runs, the green activity panel above shows live status, pipeline chips, and progress. When the nine-cell plan is saved, the storyboard card appears below — then use Continue to Phase 2 (3×3 master grid).';
      }
    } else {
      if (step6Title) step6Title.textContent = 'Preferences';
      if (step6Desc) step6Desc.textContent = 'The pipeline generates these from your prompt (Headline, Key message, Call to action) in Phase 1 via an LLM parse step — usually ~15–45 seconds after the job starts processing. Review and edit when they appear. Influencer / personal-brand: the optional auto-portrait request in the API log is separate; it does not produce TEXT 1–3. Product video: background music (Suno) may also start in the background once these texts exist.';
      if (step7Title) step7Title.textContent = 'VO script';
      if (step7Desc) step7Desc.textContent = 'Review and edit the generated script. Text appears here as soon as the pipeline saves it (polling every ~2s). Product photos: clean product image normally runs in Phase 1. Approve and continue saves edits and runs the next pipeline segment (e.g. VO audio), then music and later steps.';
    }
    try {
      applyStep4CharacterSourceUI();
      refreshStep4CharacterExclusiveHint();
    } catch (eHint) {}
  }

  function getAllUploadZones() {
    const list = [].concat(refImageZones, assetZones, cleanProductZones, characterZones);
    if (logoZone) list.push(logoZone);
    return list;
  }

  /** True if the user uploaded at least one character image in step 4 (or restored session). */
  function hasUploadedCharacter() {
    if (!characterZones || !characterZones.length) return false;
    return characterZones.some(function (z) { return z.getUrl && z.getUrl(); });
  }

  /** True if character slot has a committed http(s) URL (uploaded to server), not only a local data: preview. */
  function hasHttpCharacterUrl() {
    if (!characterZones || !characterZones.length) return false;
    return characterZones.some(function (z) {
      const u = z.getUrl && z.getUrl();
      return u && typeof u === 'string' && /^https?:\/\//i.test(u.trim());
    });
  }

  /** True if a local file is selected in a character zone but not uploaded yet (preview only). */
  function hasPendingCharacterFile() {
    if (!characterZones || !characterZones.length) return false;
    return characterZones.some(function (z) {
      return z.getFile && z.getFile() && (!z.getUrl || !z.getUrl());
    });
  }

  /** Set character zone 1 URL (used after Studio approves auto-generated portrait). */
  function setPrimaryCharacterUrl(url) {
    if (characterZones && characterZones[0] && characterZones[0].setUrl) {
      characterZones[0].setUrl(url || null);
    }
  }

  /** First character slot URL when it is a hosted http(s) link (for step-7 preview after apply). */
  function getPrimaryCharacterHttpUrl() {
    if (!characterZones || !characterZones[0] || !characterZones[0].getUrl) return '';
    const u = characterZones[0].getUrl();
    if (u == null || typeof u !== 'string') return '';
    const t = u.trim();
    return /^https?:\/\//i.test(t) ? t : '';
  }

  function getFormAndUploadSnapshot() {
    const videoTypeCard = document.querySelector('[data-video-type].selected');
    const genderBtn = document.querySelector('.studio-gender-toggle button.active');
    const get = (id) => {
      const el = document.getElementById(id);
      if (!el) return undefined;
      if (el.type === 'checkbox') return el.checked;
      return el.value != null ? el.value : undefined;
    };
    const snap = {
      prompt: get('prompt'),
      duration: get('duration'),
      style: get('style'),
      country: get('country'),
      language: get('language'),
      gender: genderBtn ? genderBtn.dataset.gender : undefined,
      video_type: videoTypeCard ? videoTypeCard.dataset.videoType : undefined,
      add_subtitles: document.getElementById('addSubtitles') ? document.getElementById('addSubtitles').checked : undefined,
      video_reference_url: get('videoReferenceUrl'),
      vo_pre_written: get('voPreWritten'),
      text1: get('text1'),
      text2: get('text2'),
      text3: get('text3'),
      voice_id: get('voiceId'),
      voice_id_custom: get('voiceIdCustom'),
      slogan: get('slogan'),
      product_explain: get('productExplain'),
      character_brief: get('characterBrief'),
      portrait_image_prompt: get('portraitImagePromptHidden'),
      image_model: get('imageModel'),
      animation_model: get('animationModel'),
      subtitle_template: get('subtitleTemplate'),
      subtitle_position: get('subtitlePosition'),
      subtitle_emoji: get('subtitleEmoji'),
      simulation_mode: document.getElementById('simulationMode') ? document.getElementById('simulationMode').checked : undefined,
      vo_script: get('voScript'),
      music_description: get('musicDescription'),
      ref_image_urls: (refImageZones || []).map(z => z.getUrl && z.getUrl()).filter(Boolean),
      ref_image_explains: (refImageExplains || []).map(inp => inp && inp.value).filter(x => x != null),
      asset_urls: (assetZones || []).map(z => z.getUrl && z.getUrl()).filter(Boolean),
      clean_product_urls: (cleanProductZones || []).map(z => z.getUrl && z.getUrl()).filter(Boolean),
      character_urls: (characterZones || []).map(z => z.getUrl && z.getUrl()).filter(Boolean),
      logo_url: logoZone && logoZone.getUrl ? logoZone.getUrl() : null,
      product_no_on_screen_character: isProductNoOnScreenCharacter(),
      character_library_select: (() => {
        const el = document.getElementById('characterLibrarySelect');
        return el && el.value ? String(el.value).trim() : '';
      })(),
      character_library_name: (() => {
        const el = document.getElementById('characterLibraryName');
        return el && el.value ? String(el.value).trim() : '';
      })(),
      character_source_mode: getStep4CharacterSourceMode() || undefined
    };
    return snap;
  }

  function setFormAndUploadSnapshot(snap) {
    if (!snap) return;
    const set = (id, value) => {
      const el = document.getElementById(id);
      if (!el || value === undefined) return;
      if (el.type === 'checkbox') el.checked = !!value;
      else el.value = value;
    };
    set('prompt', snap.prompt);
    set('duration', snap.duration);
    set('style', snap.style);
    set('country', snap.country);
    set('language', snap.language);
    set('videoReferenceUrl', snap.video_reference_url);
    set('voPreWritten', snap.vo_pre_written);
    set('text1', snap.text1);
    set('text2', snap.text2);
    set('text3', snap.text3);
    set('voiceId', snap.voice_id);
    set('voiceIdCustom', snap.voice_id_custom);
    set('slogan', snap.slogan);
    set('productExplain', snap.product_explain);
    set('characterBrief', snap.character_brief);
    set('portraitImagePromptHidden', snap.portrait_image_prompt);
    set('imageModel', snap.image_model);
    set('animationModel', snap.animation_model);
    set('subtitleTemplate', snap.subtitle_template);
    set('subtitlePosition', snap.subtitle_position);
    set('subtitleEmoji', snap.subtitle_emoji);
    set('voScript', snap.vo_script);
    set('musicDescription', snap.music_description);
    if (snap.add_subtitles !== undefined) {
      const addEl = document.getElementById('addSubtitles');
      if (addEl) addEl.checked = snap.add_subtitles;
    }
    if (snap.simulation_mode !== undefined) {
      const simEl = document.getElementById('simulationMode');
      if (simEl) simEl.checked = snap.simulation_mode;
    }
    if (snap.video_type) {
      const vCard = document.querySelector('[data-video-type="' + snap.video_type + '"]');
      if (vCard) {
        document.querySelectorAll('[data-video-type]').forEach((card) => card.classList.remove('selected'));
        vCard.classList.add('selected');
        _lastKnownVideoType = snap.video_type;
      }
    }
    if (snap.gender) {
      document.querySelectorAll('.studio-gender-toggle button').forEach(btn => btn.classList.remove('active'));
      const btn = document.querySelector('.studio-gender-toggle button[data-gender="' + snap.gender + '"]');
      if (btn) btn.classList.add('active');
    }
    if (snap.product_no_on_screen_character !== undefined) {
      const pnEl = document.getElementById('productNoOnScreenCharacter');
      if (pnEl) pnEl.checked = !!snap.product_no_on_screen_character;
    }
    if (snap.character_library_select) {
      try {
        window._studioRestoreCharacterLibraryId = String(snap.character_library_select).trim();
      } catch (eLib) {}
    }
    if (snap.character_library_name !== undefined) {
      set('characterLibraryName', snap.character_library_name);
    }
    const durInp = document.getElementById('duration');
    const durWrap = document.getElementById('durationOptions');
    if (durInp && durWrap) {
      const dv = String(snap.duration || durInp.value || '').trim();
      if (dv) {
        durWrap.querySelectorAll('button[data-duration]').forEach((b) => {
          b.classList.toggle('active', String(b.dataset.duration) === dv);
        });
      }
    }
    (refImageZones || []).forEach((z, i) => {
      if (snap.ref_image_urls && snap.ref_image_urls[i] && z.setUrl) z.setUrl(snap.ref_image_urls[i]);
    });
    (refImageExplains || []).forEach((inp, i) => {
      if (inp && snap.ref_image_explains && snap.ref_image_explains[i] !== undefined) inp.value = snap.ref_image_explains[i];
    });
    (assetZones || []).forEach((z, i) => {
      if (snap.asset_urls && snap.asset_urls[i] && z.setUrl) z.setUrl(snap.asset_urls[i]);
    });
    (cleanProductZones || []).forEach((z, i) => {
      if (snap.clean_product_urls && snap.clean_product_urls[i] && z.setUrl) z.setUrl(snap.clean_product_urls[i]);
    });
    (characterZones || []).forEach((z, i) => {
      if (snap.character_urls && snap.character_urls[i] && z.setUrl) z.setUrl(snap.character_urls[i]);
    });
    if (logoZone && logoZone.setUrl && snap.logo_url) logoZone.setUrl(snap.logo_url);
    const inferredMode = _inferCharacterSourceModeFromSnapshot(snap);
    if (inferredMode) {
      document.body.dataset.studioCharacterSourceMode = inferredMode;
      const rMode = document.querySelector('input[name="characterSourceMode"][value="' + inferredMode + '"]');
      if (rMode) rMode.checked = true;
    } else {
      document.body.dataset.studioCharacterSourceMode = '';
      document.querySelectorAll('input[name="characterSourceMode"]').forEach((inp) => { inp.checked = false; });
    }
    applyStep4CharacterSourceUI();
  }

  /** Reset wizard to a blank state (new video). Call after starting a new cloud session. */
  function resetWizardFormAndZones() {
    const set = (id, value) => {
      const el = document.getElementById(id);
      if (!el) return;
      if (el.type === 'checkbox') el.checked = !!value;
      else el.value = value != null ? String(value) : '';
    };
    set('prompt', '');
    set('duration', '20');
    const durWrapReset = document.getElementById('durationOptions');
    if (durWrapReset) {
      durWrapReset.querySelectorAll('button[data-duration]').forEach((b) => {
        b.classList.toggle('active', b.dataset.duration === '20');
      });
    }
    set('style', 'Auto');
    set('country', '');
    const lang = document.getElementById('language');
    if (lang && lang.options.length) lang.selectedIndex = 0;
    set('videoReferenceUrl', '');
    set('voPreWritten', '');
    set('text1', '');
    set('text2', '');
    set('text3', '');
    set('voiceId', '');
    set('voiceIdCustom', '');
    set('slogan', '');
    set('productExplain', '');
    set('characterBrief', '');
    set('portraitImagePromptHidden', '');
    set('voScript', '');
    set('musicDescription', '');
    const addSub = document.getElementById('addSubtitles');
    if (addSub) addSub.checked = true;
    const sim = document.getElementById('simulationMode');
    if (sim) sim.checked = false;
    const pnReset = document.getElementById('productNoOnScreenCharacter');
    if (pnReset) pnReset.checked = false;
    document.body.dataset.studioCharacterSourceMode = '';
    document.querySelectorAll('input[name="characterSourceMode"]').forEach((inp) => { inp.checked = false; });
    try {
      window._studioRestoreCharacterLibraryId = '';
    } catch (eR) {}
    const libReset = document.getElementById('characterLibrarySelect');
    if (libReset) libReset.value = '';
    set('characterLibraryName', '');
    document.querySelectorAll('[data-video-type]').forEach((card) => card.classList.remove('selected'));
    const prodCard = document.querySelector('[data-video-type="product video"]');
    if (prodCard) prodCard.classList.add('selected');
    _lastKnownVideoType = 'product video';
    document.querySelectorAll('.studio-gender-toggle button').forEach((btn) => btn.classList.remove('active'));
    const fBtn = document.querySelector('.studio-gender-toggle button[data-gender="f"]');
    if (fBtn) fBtn.classList.add('active');
    (refImageZones || []).forEach((z) => {
      if (z.clear) z.clear();
      else if (z.setUrl) z.setUrl(null);
    });
    (refImageExplains || []).forEach((inp) => {
      if (inp) inp.value = '';
    });
    (assetZones || []).forEach((z) => {
      if (z.clear) z.clear();
      else if (z.setUrl) z.setUrl(null);
    });
    (cleanProductZones || []).forEach((z) => {
      if (z.clear) z.clear();
      else if (z.setUrl) z.setUrl(null);
    });
    (characterZones || []).forEach((z) => {
      if (z.clear) z.clear();
      else if (z.setUrl) z.setUrl(null);
    });
    if (logoZone) {
      if (logoZone.clear) logoZone.clear();
      else if (logoZone.setUrl) logoZone.setUrl(null);
    }
    const pe = document.getElementById('prompt');
    const pc = document.getElementById('promptCount');
    if (pe && pc) pc.textContent = (pe.value || '').length;
    const voEl = document.getElementById('voScript');
    const wc = document.getElementById('voWordCount');
    if (voEl && wc) wc.textContent = (voEl.value || '').trim().split(/\s+/).filter(Boolean).length;
    updateVisibilityForVideoType();
  }

  return {
    registerMediaZones,
    getVideoType,
    syncVideoTypeFromSession,
    getLanguage,
    getGender,
    validateStep,
    collectGeneratePayload,
    isUgcRealFlow,
    parseUgcOfferText3,
    collectPhase1Payload,
    collectPhase2Payload,
    collectPhase3Payload,
    collectPhase3PauseScenePromptsPayload,
    collectMusicPayload,
    collectSceneImagePayload,
    isProductNoOnScreenCharacter,
    clearCharacterSlotsAndBrief,
    updateVisibilityForVideoType,
    getAllUploadZones,
    hasUploadedCharacter,
    hasHttpCharacterUrl,
    hasPendingCharacterFile,
    setPrimaryCharacterUrl,
    getPrimaryCharacterHttpUrl,
    getFormAndUploadSnapshot,
    setFormAndUploadSnapshot,
    resetWizardFormAndZones,
    getStep4CharacterExclusiveError,
    refreshStep4CharacterExclusiveHint,
    getStep4CharacterSourceMode,
    setCharacterSourceMode,
    applyStep4CharacterSourceUI,
    getStep4SourceValidationError
  };
})();

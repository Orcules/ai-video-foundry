import { updateAnimationModels } from './config.js';
import { state } from './state.js';

export function updatePipelineVisibility(videoType) {
  const vt = videoType.toLowerCase();
  const pipeline = vt === 'influencer'
    ? 'influencer'
    : vt === 'personal-brand'
      ? 'personal-brand'
      : vt === 'ugc-real'
        ? 'influencer'
        : 'product';
  document.querySelectorAll('[data-pipelines]').forEach(el => {
    const allowed = el.dataset.pipelines.split(' ');
    el.style.display = allowed.includes(pipeline) ? '' : 'none';
  });
  const offerRow = document.getElementById('offerTypeRow');
  if (offerRow) {
    offerRow.style.display = vt === 'ugc-real' ? '' : 'none';
  }
}

export function initVideoTypeListener() {
  document.getElementById('videoType').addEventListener('change', (e) => {
    const val = e.target.value.toLowerCase();
    const isInfluencer = val === 'influencer';
    const isPersonalBrand = val === 'personal-brand';
    const isUgcReal = val === 'ugc-real';
    const pipeline = isInfluencer
      ? 'influencer'
      : isPersonalBrand
        ? 'personal_brand'
        : isUgcReal
          ? 'ugc_real'
          : 'product';
    updateAnimationModels(pipeline);
    updatePipelineVisibility(e.target.value);
    updateStyleDefault(e.target.value);
    updateTierDefaults();
  });
}

export function addUrlInput(listId, className) {
  const list = document.getElementById(listId);
  const row = document.createElement('div');
  row.className = 'url-input-row';
  const input = document.createElement('input');
  input.type = 'url';
  input.placeholder = 'https://...';
  input.className = className;
  input.autocomplete = 'one-time-code';
  const btn = document.createElement('button');
  btn.className = 'upload-btn';
  btn.title = 'Upload file';
  btn.innerHTML = '&#8682;';
  btn.onclick = function() { window.uploadFile(this); };
  const rmBtn = document.createElement('button');
  rmBtn.className = 'remove-btn';
  rmBtn.title = 'Remove';
  rmBtn.innerHTML = '&times;';
  rmBtn.onclick = function() { window.removeUrlRow(this); };
  row.appendChild(input);
  row.appendChild(btn);
  row.appendChild(rmBtn);
  list.appendChild(row);
  updateRowButtons(row);
  input.addEventListener('input', () => updateRowButtons(row));
}

export function addAssetInput() {
  const list = document.getElementById('assetList');
  const row = document.createElement('div');
  row.className = 'url-input-row';
  const input = document.createElement('input');
  input.type = 'url';
  input.placeholder = 'https://example.com/clip.mp4';
  input.className = 'asset-url';
  input.style.flex = '1';
  input.autocomplete = 'one-time-code';
  const sel = document.createElement('select');
  sel.className = 'asset-type';
  sel.style.cssText = 'width:80px;flex:none;font-size:11px';
  sel.title = 'Asset type hint';
  sel.innerHTML = '<option value="auto">Auto</option><option value="image">Image</option><option value="video">Video</option>';
  const lbl = document.createElement('label');
  lbl.style.cssText = 'display:flex;align-items:center;gap:3px;flex:none;font-size:11px;white-space:nowrap';
  lbl.title = 'Preserve original audio in video assets';
  const chk = document.createElement('input');
  chk.type = 'checkbox';
  chk.className = 'asset-keep-audio';
  lbl.appendChild(chk);
  lbl.appendChild(document.createTextNode(' Audio'));
  const btn = document.createElement('button');
  btn.className = 'upload-btn';
  btn.title = 'Upload file';
  btn.innerHTML = '&#8682;';
  btn.onclick = function() { window.uploadFile(this); };
  const rmBtn = document.createElement('button');
  rmBtn.className = 'remove-btn';
  rmBtn.title = 'Remove';
  rmBtn.innerHTML = '&times;';
  rmBtn.onclick = function() { window.removeUrlRow(this); };
  row.appendChild(input);
  row.appendChild(sel);
  row.appendChild(lbl);
  row.appendChild(btn);
  row.appendChild(rmBtn);
  list.appendChild(row);
  updateRowButtons(row);
  input.addEventListener('input', () => updateRowButtons(row));
}

const STYLE_DEFAULTS = {
  'product': 'Cinematic photography',
  'influencer': 'Ultra photorealistic',
  'personal-brand': 'Ultra photorealistic',
  'personal_brand': 'Ultra photorealistic',
  'ugc-real': 'Ultra photorealistic',
  'ugc_real': 'Ultra photorealistic',
};

export function updateStyleDefault(videoType) {
  const vt = (videoType || '').toLowerCase();
  const pipeline = vt === 'influencer'
    ? 'influencer'
    : vt === 'personal-brand'
      ? 'personal-brand'
      : vt === 'ugc-real'
        ? 'ugc-real'
        : 'product';
  const resolvedStyle = STYLE_DEFAULTS[pipeline] || 'Cinematic photography';
  const autoOption = document.querySelector('#style option[value="Auto"]');
  if (autoOption) autoOption.textContent = `Auto — ${resolvedStyle} (default)`;
}

export function onAssetModeChange() {
  const mode = document.getElementById('assetMode').value;
  const hintsRow = document.getElementById('voDurationHintsRow');
  if (hintsRow) {
    hintsRow.style.display = mode === 'smart' ? '' : 'none';
    if (mode !== 'smart') {
      document.getElementById('voDurationHints').value = 'false';
    }
  }
}

export function toggleBeatSyncStrategy() {
  const wrapper = document.getElementById('beatSyncStrategyWrapper');
  wrapper.style.display = document.getElementById('soundSyncMethod').value === 'beat_sync' ? '' : 'none';
}

export function updatePromptCounter() {
  const len = (document.getElementById('prompt').value || '').trim().length;
  const charsEl = document.getElementById('promptChars');
  const modeEl = document.getElementById('promptMode');
  charsEl.textContent = len + ' chars';
  if (len > 200) {
    charsEl.style.color = '#22c55e';
    modeEl.innerHTML = 'VO mode: <strong style="color:#22c55e">detailed</strong> — your prompt used directly as VO brief';
  } else {
    charsEl.style.color = '#6e7681';
    modeEl.innerHTML = 'VO mode: <strong>generic</strong> — VO generated from AI-parsed content';
  }
}

export function getUrlList(className) {
  return Array.from(document.querySelectorAll('.' + className))
    .map(el => el.value.trim())
    .filter(v => v);
}

/** Toggle upload/remove button visibility based on whether the input has a value. */
export function updateRowButtons(row) {
  if (!row || !row.classList.contains('url-input-row')) return;
  const input = row.querySelector('input[type="url"]');
  const uploadBtn = row.querySelector('.upload-btn');
  const removeBtn = row.querySelector('.remove-btn');
  if (!input) return;
  const hasValue = input.value.trim() !== '';
  if (uploadBtn) uploadBtn.style.display = hasValue ? 'none' : '';
  if (removeBtn) removeBtn.style.display = hasValue ? '' : 'none';
}

/** Map video_model string to a friendly display name. */
function friendlyModelName(model) {
  if (!model) return model;
  if (model.startsWith('veo')) return 'Google Veo';
  if (model.startsWith('kling')) return 'Kling';
  if (model.startsWith('runway')) return 'Runway';
  return model;
}

/** Map tier image_model to the friendly Image API label. */
function friendlyImageApi(imageModel) {
  if (!imageModel) return null;
  if (imageModel === 'nano-banana-pro') return 'Nano Banana Pro (Kie.ai)';
  if (imageModel === 'gemini-3-flash') return 'Gemini 3 Flash (Kie.ai)';
  if (imageModel === 'gemini-3.1-flash-image-preview') return 'Gemini 3.1 Flash (Vertex AI)';
  if (imageModel.startsWith('gemini')) return 'Gemini 3 Pro (Vertex AI)';
  return imageModel;
}

/**
 * Resolve "Auto (from tier)" labels to actual tier values.
 * Reads current video type + output tier from the DOM, looks up tiersConfig,
 * and updates animation model label + advanced override placeholders.
 * Graceful fallback: if tiersConfig is null (server unreachable), labels stay generic.
 */
export function updateTierDefaults() {
  if (!state.tiersConfig) return;

  const vt = (document.getElementById('videoType').value || '').toLowerCase();
  const pipeline = vt === 'influencer'
    ? 'influencer'
    : vt === 'personal-brand'
      ? 'personal_brand'
      : vt === 'ugc-real'
        ? 'ugc_real'
        : 'product';
  const tier = document.getElementById('outputResolution').value || '720p_low';

  const section = state.tiersConfig[pipeline];
  if (!section) return;
  const defaults = section[tier];
  if (!defaults) return;

  // --- Animation model auto label ---
  const autoOpt = document.querySelector('#animationModel option[value="auto"]');
  if (autoOpt) {
    const friendly = friendlyModelName(defaults.video_model);
    autoOpt.textContent = friendly
      ? `Auto \u2014 ${friendly} (${defaults.video_model})`
      : 'Auto';
  }

  // --- Image API auto label ---
  const imageAutoOpt = document.querySelector('#imageApi option[value="auto"]');
  if (imageAutoOpt) {
    const friendlyImg = friendlyImageApi(defaults.image_model);
    imageAutoOpt.textContent = friendlyImg
      ? `Auto \u2014 ${friendlyImg}`
      : 'Auto';
  }

  // --- Advanced override select defaults ---
  const selectMap = {
    overrideVideoProvider: defaults.video_provider,
    overrideVideoResolution: defaults.video_resolution,
    imageProvider: defaults.image_provider,
    imageResolution: defaults.image_resolution,
  };
  for (const [id, val] of Object.entries(selectMap)) {
    const opt = document.querySelector(`#${id} option[value=""]`);
    if (opt) opt.textContent = val ? `Auto \u2014 ${val}` : 'Auto';
  }
}

/** Attach input listeners and set initial state for all url-input-rows. */
export function initRowButtonStates() {
  document.querySelectorAll('.url-input-row').forEach(row => {
    updateRowButtons(row);
    const input = row.querySelector('input[type="url"]');
    if (input) input.addEventListener('input', () => updateRowButtons(row));
  });
}

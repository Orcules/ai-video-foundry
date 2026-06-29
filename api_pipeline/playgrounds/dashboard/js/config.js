import { state } from './state.js';
import { updateTierDefaults } from './form.js';
import { pushWaiting, popWaiting } from './waiting-overlay.js';

export function authHeaders(extra) {
  const h = {};
  if (state.API_KEY) h['Authorization'] = 'Bearer ' + state.API_KEY;
  if (extra) Object.assign(h, extra);
  return h;
}

export function initServer() {
  // API key is hardcoded
  document.getElementById('apiKeyInput').value = state.API_KEY;

  const saved = localStorage.getItem('tvd_api_base');
  const origin = window.location.origin;
  const isFileProtocol = origin === 'null' || origin.startsWith('file');

  if (saved) {
    state.API_BASE = saved;
    const select = document.getElementById('serverSelect');
    const match = Array.from(select.options).find(o => o.value === saved);
    if (match) {
      select.value = saved;
    } else {
      select.value = 'custom';
      document.getElementById('serverCustom').style.display = '';
      document.getElementById('serverCustom').value = saved;
    }
  } else if (isFileProtocol) {
    // Opened from disk — default to localhost
    state.API_BASE = 'http://localhost:8000';
    document.getElementById('serverSelect').value = 'http://localhost:8000';
  }

  checkServerHealth();
}

export function onServerSelect() {
  const val = document.getElementById('serverSelect').value;
  const customInput = document.getElementById('serverCustom');

  if (val === 'custom') {
    customInput.style.display = '';
    customInput.focus();
    return;
  }

  customInput.style.display = 'none';

  if (val === 'auto') {
    state.API_BASE = window.location.origin;
  } else {
    state.API_BASE = val;
  }

  localStorage.setItem('tvd_api_base', state.API_BASE);
  checkServerHealth();
}

export function onCustomServer() {
  const val = document.getElementById('serverCustom').value.trim().replace(/\/+$/, '');
  if (val) {
    state.API_BASE = val;
    localStorage.setItem('tvd_api_base', state.API_BASE);
    checkServerHealth();
  }
}

export function onApiKeyChange() {
  state.API_KEY = document.getElementById('apiKeyInput').value.trim();
  checkServerHealth();
}

export async function checkServerHealth() {
  const dot = document.getElementById('serverStatus');
  dot.className = 'server-status';
  dot.title = 'Checking ' + state.API_BASE + '...';

  try {
    const resp = await fetch(state.API_BASE + '/api/health', { signal: AbortSignal.timeout(5000) });
    if (resp.ok) {
      dot.className = 'server-status ok';
      dot.title = 'Connected to ' + state.API_BASE;
    } else {
      dot.className = 'server-status fail';
      dot.title = 'Server returned ' + resp.status;
    }
  } catch (e) {
    dot.className = 'server-status fail';
    dot.title = 'Cannot reach ' + state.API_BASE + ' — ' + e.message;
  }
}

export function updateAnimationModels(pipeline) {
  // Normalize: data-pipelines uses hyphens, config keys use underscores
  const key = pipeline.replace(/-/g, '_');
  const select = document.getElementById('animationModel');
  const prev = select.value;
  // Remove all options except Auto
  while (select.options.length > 1) select.remove(1);
  const allModels = state.animationModelsConfig[key] || state.animationModelsConfig.product || [];
  allModels.filter(m => m.value !== 'auto').forEach(m => {
    const opt = document.createElement('option');
    opt.value = m.value;
    opt.textContent = m.label;
    select.appendChild(opt);
  });
  if ([...select.options].some(o => o.value === prev)) select.value = prev;
  else select.value = 'auto';
  updateVideoProviderOptions();
}

// Video model → allowed video providers (mirrors _ANIMATION_MODEL_MAP in input_translator.py)
const MODEL_PROVIDERS = {
  google: ['direct'],
  kling: ['kie'],
  runway: ['kie'],
  none: [],
};

// Image model → allowed image providers
const IMAGE_MODEL_PROVIDERS = {
  kie: ['kie'],
  'kie-flash': ['kie'],
  google: ['direct'],
};

export function updateVideoProviderOptions() {
  const model = document.getElementById('animationModel').value;
  const select = document.getElementById('overrideVideoProvider');
  const prev = select.value;

  const allowed = MODEL_PROVIDERS[model]; // undefined for 'auto'

  // Rebuild options
  select.innerHTML = '';

  // Auto is always first
  const autoOpt = document.createElement('option');
  autoOpt.value = '';
  autoOpt.textContent = 'Auto';
  select.appendChild(autoOpt);

  if (allowed === undefined) {
    // 'auto' or unknown — show all providers
    for (const val of ['direct', 'kie']) {
      const opt = document.createElement('option');
      opt.value = val;
      opt.textContent = val;
      select.appendChild(opt);
    }
  } else {
    for (const val of allowed) {
      const opt = document.createElement('option');
      opt.value = val;
      opt.textContent = val;
      select.appendChild(opt);
    }
  }

  // Restore previous selection if still valid, otherwise Auto
  if ([...select.options].some(o => o.value === prev)) select.value = prev;
  else select.value = '';
}

export function updateImageProviderOptions() {
  const model = document.getElementById('imageApi').value;
  const select = document.getElementById('imageProvider');
  const prev = select.value;

  const allowed = IMAGE_MODEL_PROVIDERS[model]; // undefined for 'auto'

  select.innerHTML = '';

  const autoOpt = document.createElement('option');
  autoOpt.value = '';
  autoOpt.textContent = 'Auto';
  select.appendChild(autoOpt);

  if (allowed === undefined) {
    for (const val of ['direct', 'kie']) {
      const opt = document.createElement('option');
      opt.value = val;
      opt.textContent = val;
      select.appendChild(opt);
    }
  } else {
    for (const val of allowed) {
      const opt = document.createElement('option');
      opt.value = val;
      opt.textContent = val;
      select.appendChild(opt);
    }
  }

  if ([...select.options].some(o => o.value === prev)) select.value = prev;
  else select.value = '';
}

// Fetch animation model config from server
export async function loadAnimationConfig() {
  pushWaiting('load_settings');
  try {
    try {
      const resp = await fetch(state.API_BASE + '/api/config', { headers: authHeaders() });
      if (resp.ok) {
        const cfg = await resp.json();
        if (cfg.animation_models) state.animationModelsConfig = cfg.animation_models;
        if (cfg.resolution_tiers) state.tiersConfig = cfg.resolution_tiers;
      }
    } catch (e) { /* use hardcoded fallback */ }
    // Initialize dropdown for current pipeline selection
    const vt = (document.getElementById('videoType').value || '').toLowerCase();
    const pipeline = vt === 'influencer' ? 'influencer' : vt === 'personal-brand' ? 'personal_brand' : 'product';
    updateAnimationModels(pipeline);
    updateTierDefaults();
  } finally {
    popWaiting();
  }
}

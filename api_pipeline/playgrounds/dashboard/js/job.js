import { state } from './state.js';
import { authHeaders } from './config.js';
import { pushWaiting, popWaiting } from './waiting-overlay.js';
import { setStatus, setButtons, setProgress, registerRestartPopulator, updateQueueDisplay } from './ui.js';
import { addEvent } from './event-log.js';
import { startCostPoll, stopCostPoll, updateCostDisplay } from './cost.js';
import { connectSSE } from './sse.js';
import { startQueuePoll, stopQueuePoll } from './queue.js';
import { getUrlList } from './form.js';
import { cleanupPreviousJob } from './monitor.js';

// Register the restart populator callback in ui.js to break the circular dep
registerRestartPopulator(populateRestartDropdown);

export async function populateRestartDropdown() {
  const select = document.getElementById('restartStep');
  if (!state.currentJobId) return;

  pushWaiting('load_settings');
  try {
    // Fetch steps and job data in parallel
    const [stepsResp, jobResp] = await Promise.all([
      fetch(state.API_BASE + '/api/jobs/' + state.currentJobId + '/steps', { headers: authHeaders() }),
      fetch(state.API_BASE + '/api/jobs/' + state.currentJobId, { headers: authHeaders() }),
    ]);
    if (!stepsResp.ok || !jobResp.ok) return;

    const stepsData = await stepsResp.json();
    const jobData = await jobResp.json();
    const intermediates = jobData.intermediates || {};

    select.innerHTML = '';
    for (const step of stepsData.steps) {
      // Only show steps that have at least one intermediate key present
      const hasData = step.keys && step.keys.some(k => k in intermediates);
      if (!hasData) continue;

      const opt = document.createElement('option');
      opt.value = step.id;
      opt.textContent = step.id + ' \u2014 ' + step.label;
      select.appendChild(opt);
    }

    // If no completed steps, hide restart row
    if (select.options.length === 0) {
      document.getElementById('restartRow').style.display = 'none';
    }
  } catch (e) {
    console.error('Failed to fetch steps:', e);
  } finally {
    popWaiting();
  }
}

export async function startJob() {
  const isSimulation = document.getElementById('simulationMode').checked;
  const prompt = document.getElementById('prompt').value.trim();
  if (!isSimulation && !prompt) {
    alert('Please enter a prompt.');
    return;
  }

  cleanupPreviousJob();
  setStatus('running', isSimulation ? (document.getElementById('simTypeMonolith').checked ? 'Monolith Simulation...' : 'Wrapper Simulation...') : 'Submitting...');
  setButtons('running');

  const videoType = document.getElementById('videoType').value;
  const body = {
    video_type: videoType,
    prompt: prompt || 'Simulation mode',
    duration: parseInt(document.getElementById('duration').value) || 20,
    style: document.getElementById('style').value,
    animation_model: document.getElementById('animationModel').value,
    language: document.getElementById('language').value || 'en',
    add_subtitles: document.getElementById('addSubtitles').checked,
    subtitle_emoji: document.getElementById('subtitleEmoji').checked,
    subtitle_position: document.getElementById('subtitlePosition').value,
    quality_check: document.getElementById('qualityCheck').checked,
    generate_vo: document.getElementById('generateVo').checked,
    sound_sync_method: document.getElementById('soundSyncMethod').value,
    beat_sync_strategy: document.getElementById('beatSyncStrategy').value,
    output_resolution: document.getElementById('outputResolution').value,
  };

  const vtLower = videoType.toLowerCase();
  const isUgcReal = vtLower === 'ugc-real';

  // dissolve_seconds: all pipelines support it, but only send if relevant
  const ds = parseFloat(document.getElementById('dissolveSeconds').value);
  body.dissolve_seconds = isNaN(ds) ? 0.4 : ds;

  // Product images
  const productImgs = getUrlList('product-img-url');
  if (productImgs.length) body.product_image_urls = productImgs;
  if (vtLower === 'product video') {
    body.product_image_mode = document.getElementById('productImageMode').value;
  }
  if (isUgcReal) {
    const offerTypeEl = document.getElementById('offerType');
    const offerType = offerTypeEl ? offerTypeEl.value : '';
    body.offer_type = offerType || 'physical_product';
  }

  // Image API (all pipelines)
  {
    const imageApi = document.getElementById('imageApi').value;
    if (imageApi && imageApi !== 'auto') body.image_api = imageApi;
  }
  // Character 2/3 URLs (all pipelines)
  {
    const char2 = document.getElementById('character2Url').value.trim();
    const char3 = document.getElementById('character3Url').value.trim();
    const charUrls = [char2, char3].filter(v => v);
    if (charUrls.length) body.character_urls = charUrls;
  }

  // Influencer and personal-brand fields
  const isInfluencerLike = vtLower === 'influencer' || vtLower === 'personal-brand' || isUgcReal;
  if (isInfluencerLike) {
    body.gender = document.getElementById('gender').value;
    const refImgs = getUrlList('ref-img-url');
    if (refImgs.length) body.reference_image_urls = refImgs;
    // Build asset_urls with optional type hints and keep_audio
    const assetRows = document.querySelectorAll('#assetList .url-input-row');
    const assetItems = [];
    assetRows.forEach(row => {
      const url = row.querySelector('.asset-url').value.trim();
      const typeSel = row.querySelector('.asset-type');
      const typeVal = typeSel ? typeSel.value : 'auto';
      const keepAudio = row.querySelector('.asset-keep-audio')?.checked || false;
      if (url) {
        if ((typeVal && typeVal !== 'auto') || keepAudio) {
          const obj = {url};
          if (typeVal && typeVal !== 'auto') obj.type = typeVal;
          if (keepAudio) obj.keep_audio = true;
          assetItems.push(obj);
        } else {
          assetItems.push(url);  // backward compat: plain string
        }
      }
    });
    if (assetItems.length) body.asset_urls = assetItems;
    body.enrich_cta_with_influencer = document.getElementById('enrichCtaInfluencer').value === 'true';
    body.remove_character_bg = document.getElementById('removeCharacterBg').value === 'true';
    // Smart asset mode (influencer only)
    if (vtLower === 'influencer') {
      body.asset_mode = document.getElementById('assetMode').value;
      body.vo_duration_hints = document.getElementById('voDurationHints').value === 'true';
      const minInfR = parseFloat(document.getElementById('minInfluencerClipRatio').value);
      if (!isNaN(minInfR)) body.min_influencer_clip_ratio = minInfR;
      const maxInfR = parseFloat(document.getElementById('maxInfluencerClipRatio').value);
      if (!isNaN(maxInfR)) body.max_influencer_clip_ratio = maxInfR;
      const hlText = document.getElementById('highlights').value.trim();
      if (hlText) {
        body.highlights = hlText.split('\n').map(s => s.trim()).filter(Boolean);
      }
      const surpriseMode = document.getElementById('surpriseMode').value;
      if (surpriseMode) body.surprise_mode = surpriseMode;
      body.generate_extended = document.getElementById('generateExtended').checked;
      // End card business info
      const bizName = document.getElementById('businessName').value.trim();
      const bizAddr = document.getElementById('businessAddress').value.trim();
      const bizPhone = document.getElementById('businessPhone').value.trim();
      if (bizName) body.business_name = bizName;
      if (bizAddr) body.business_address = bizAddr;
      if (bizPhone) body.business_phone = bizPhone;
      const bizWeb = document.getElementById('businessWebsite').value.trim();
      if (bizWeb) body.business_website = bizWeb;
      const ecColor = document.getElementById('endCardColor').value.trim();
      const ecDetailColor = document.getElementById('endCardDetailColor').value.trim();
      if (ecColor && ecColor !== 'white') body.end_card_color = ecColor;
      if (ecDetailColor && ecDetailColor !== 'white') body.end_card_detail_color = ecDetailColor;
      const ecPos = document.getElementById('endCardPosition').value;
      if (ecPos && ecPos !== 'middle') body.end_card_position = ecPos;
    }
  }

  // Film grain (all pipelines)
  {
    const fg = document.getElementById('filmGrain').value;
    if (fg !== 'default') body.film_grain = fg === 'true';
  }

  // Simulation flags
  if (isSimulation) {
    body.simulation = true;
    body.simulation_type = document.getElementById('simTypeMonolith').checked ? 'monolith' : 'wrapper';
    const simDurSelect = document.getElementById('simDuration');
    const simDurValue = simDurSelect.value;
    if (simDurValue === 'custom') {
      const customVal = document.getElementById('simDurationCustom').value.trim();
      body.simulation_duration = customVal || 'none';
    } else {
      body.simulation_duration = simDurValue;
    }
  }

  // Advanced overrides (only include if non-empty)
  const overrideFields = {
    video_provider: 'overrideVideoProvider',
    video_resolution: 'overrideVideoResolution',
    image_provider: 'imageProvider',
    image_resolution: 'imageResolution',
  };
  for (const [key, id] of Object.entries(overrideFields)) {
    const val = document.getElementById(id).value.trim();
    if (val) body[key] = val;
  }

  // Optional fields
  const optionals = {
    character_url: 'characterUrl',
    character_description: 'characterDescription',
    logo_url: 'logoUrl',
    slogan_text: 'sloganText',
    voice_id: 'voiceId',
    country: 'country',
    video_reference_url: 'videoReferenceUrl',
    customer_id: 'customerId',
  };
  for (const [key, id] of Object.entries(optionals)) {
    const val = document.getElementById(id).value.trim();
    if (val) body[key] = val;
  }

  pushWaiting('submit_job');
  try {
    const resp = await fetch(state.API_BASE + '/api/generate', {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(body),
    });

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: resp.statusText }));
      // Extract readable message from Pydantic 422 array-format errors
      let msg = 'Request failed';
      if (typeof err.detail === 'string') {
        msg = err.detail;
      } else if (Array.isArray(err.detail)) {
        msg = err.detail.map(e => e.msg || e.message || JSON.stringify(e)).join('; ');
      }
      if (resp.status === 429) {
        throw new Error('Queue full — ' + msg);
      }
      throw new Error(msg);
    }

    const data = await resp.json();
    state.currentJobId = data.job_id;
    document.getElementById('jobIdDisplay').textContent = state.currentJobId;
    document.getElementById('copyJobIdBtn').style.display = '';

    // Show input normalization warnings
    if (data.warnings && data.warnings.length) {
      for (const w of data.warnings) {
        addEvent({
          timestamp: new Date().toLocaleTimeString('en-US', { hour12: false }),
          step: 'INPUT',
          message: `Auto-corrected ${w.field}: '${w.original}' \u2192 '${w.normalized}'`,
          event_type: 'warn',
          progress: -1,
          elapsed: null,
        });
      }
    }

    if (data.status === 'queued') {
      setStatus('queued', 'Queued #' + (data.queue_position || '?'));
      setButtons('queued');
      updateQueueDisplay(data.queue_position, data.active_jobs, data.max_concurrent);
      addEvent({ timestamp: new Date().toLocaleTimeString('en-US', { hour12: false }), step: 'SERVER', message: data.message, event_type: 'info', progress: 0, elapsed: null });
      // Start polling — the drain loop will transition the job to pending/processing
      startQueuePoll(state.currentJobId);
    } else {
      updateQueueDisplay(null);
      connectSSE(state.currentJobId);
    }
  } catch (e) {
    setStatus('error', 'Submit Failed');
    addEvent({ timestamp: new Date().toLocaleTimeString('en-US', { hour12: false }), step: 'CLIENT', message: 'Failed to submit job: ' + e.message, event_type: 'error', progress: -1, elapsed: null });
    setButtons('idle');
  } finally {
    popWaiting();
  }
}

export async function pauseJob() {
  if (!state.currentJobId) return;

  document.getElementById('pauseBtn').disabled = true;

  pushWaiting('job_control');
  try {
    const resp = await fetch(state.API_BASE + '/api/jobs/' + state.currentJobId + '/pause', {
      method: 'POST',
      headers: authHeaders(),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: resp.statusText }));
      addEvent({ timestamp: new Date().toLocaleTimeString('en-US', { hour12: false }), step: 'CLIENT', message: 'Pause failed: ' + (err.detail || 'unknown'), event_type: 'warn', progress: -1, elapsed: null });
      document.getElementById('pauseBtn').disabled = false;
    } else {
      setStatus('paused', 'Pausing...');
    }
  } catch (e) {
    addEvent({ timestamp: new Date().toLocaleTimeString('en-US', { hour12: false }), step: 'CLIENT', message: 'Pause request failed: ' + e.message, event_type: 'error', progress: -1, elapsed: null });
    document.getElementById('pauseBtn').disabled = false;
  } finally {
    popWaiting();
  }
}

export async function abortJob() {
  if (!state.currentJobId) return;

  document.getElementById('abortBtn').disabled = true;

  pushWaiting('job_control');
  try {
    const resp = await fetch(state.API_BASE + '/api/jobs/' + state.currentJobId + '/abort', {
      method: 'POST',
      headers: authHeaders(),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: resp.statusText }));
      addEvent({ timestamp: new Date().toLocaleTimeString('en-US', { hour12: false }), step: 'CLIENT', message: 'Abort failed: ' + (err.detail || 'unknown'), event_type: 'warn', progress: -1, elapsed: null });
      document.getElementById('abortBtn').disabled = false;
    } else {
      stopQueuePoll();
      updateQueueDisplay(null);
      setStatus('aborted', 'Aborting...');
      // Hide Resume/Restart immediately — user should not interact while aborting.
      // The terminal SSE 'abort' event will finalize the UI state.
      document.getElementById('resumeBtn').style.display = 'none';
      document.getElementById('restartRow').style.display = 'none';
      // If no SSE stream (e.g. aborting from queued state), finalize immediately
      if (!state.eventSource) {
        setStatus('aborted', 'Aborted');
        setButtons('idle');
        addEvent({ timestamp: new Date().toLocaleTimeString('en-US', { hour12: false }), step: 'SERVER', message: 'Job aborted', event_type: 'abort', progress: -1, elapsed: null });
      }
    }
  } catch (e) {
    addEvent({ timestamp: new Date().toLocaleTimeString('en-US', { hour12: false }), step: 'CLIENT', message: 'Abort request failed: ' + e.message, event_type: 'error', progress: -1, elapsed: null });
    document.getElementById('abortBtn').disabled = false;
  } finally {
    popWaiting();
  }
}

export async function resumeJob() {
  if (!state.currentJobId) return;

  document.getElementById('resumeBtn').disabled = true;

  pushWaiting('job_control');
  try {
    const resp = await fetch(state.API_BASE + '/api/jobs/' + state.currentJobId + '/resume', {
      method: 'POST',
      headers: authHeaders(),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: resp.statusText }));
      addEvent({ timestamp: new Date().toLocaleTimeString('en-US', { hour12: false }), step: 'CLIENT', message: 'Resume failed: ' + (err.detail || 'unknown'), event_type: 'warn', progress: -1, elapsed: null });
      document.getElementById('resumeBtn').disabled = false;
    } else {
      const data = await resp.json();
      setStatus('running', 'Resuming...');
      setButtons('running');
      connectSSE(state.currentJobId, data.event_cursor || 0);
    }
  } catch (e) {
    addEvent({ timestamp: new Date().toLocaleTimeString('en-US', { hour12: false }), step: 'CLIENT', message: 'Resume request failed: ' + e.message, event_type: 'error', progress: -1, elapsed: null });
    document.getElementById('resumeBtn').disabled = false;
  } finally {
    popWaiting();
  }
}

export async function restartJob() {
  if (!state.currentJobId) return;

  const fromStep = document.getElementById('restartStep').value;
  if (!fromStep) {
    alert('Please select a step to restart from.');
    return;
  }

  document.getElementById('restartBtn').disabled = true;

  pushWaiting('job_control');
  try {
    const resp = await fetch(state.API_BASE + '/api/jobs/' + state.currentJobId + '/restart?from_step=' + encodeURIComponent(fromStep), {
      method: 'POST',
      headers: authHeaders(),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: resp.statusText }));
      addEvent({ timestamp: new Date().toLocaleTimeString('en-US', { hour12: false }), step: 'CLIENT', message: 'Restart failed: ' + (err.detail || 'unknown'), event_type: 'warn', progress: -1, elapsed: null });
      document.getElementById('restartBtn').disabled = false;
    } else {
      const data = await resp.json();
      setStatus('running', 'Restarting...');
      setButtons('running');
      setProgress(0, '');
      updateCostDisplay(null);
      connectSSE(state.currentJobId, data.event_cursor || 0);
    }
  } catch (e) {
    addEvent({ timestamp: new Date().toLocaleTimeString('en-US', { hour12: false }), step: 'CLIENT', message: 'Restart request failed: ' + e.message, event_type: 'error', progress: -1, elapsed: null });
    document.getElementById('restartBtn').disabled = false;
  } finally {
    popWaiting();
  }
}

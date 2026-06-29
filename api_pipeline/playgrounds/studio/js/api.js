/**
 * API client for Video Studio: server config, generate, SSE, upload.
 */
const StudioAPI = (function () {
  const STORAGE_KEY_BASE = 'studio_api_base';
  const STORAGE_KEY_API_KEY = 'studio_api_key';

  /** Throttle successful GET /api/jobs/{id} lines in API Log (still log every error / status change). */
  const _getJobLogThrottle = Object.create(null);
  const GET_JOB_LOG_MIN_MS = 1800;

  /** True for localhost / 127.0.0.1 / ::1 hostnames. */
  function _isLoopbackHostname(hostname) {
    var h = String(hostname || '').toLowerCase();
    return h === 'localhost' || h === '127.0.0.1' || h === '[::1]' || h === '::1';
  }

  /**
   * When the page is served from loopback (e.g. http://localhost:8000) but localStorage
   * has http://127.0.0.1:8000 (or vice versa), browsers treat them as different origins.
   * Prefer the page origin so /api/config and the Studio HTML share one origin.
   */
  function _loopbackSameService(storedBase, pageOrigin) {
    if (!storedBase || !pageOrigin || pageOrigin === 'null') return false;
    try {
      var a = new URL(storedBase);
      var b = new URL(pageOrigin);
      if (!_isLoopbackHostname(a.hostname) || !_isLoopbackHostname(b.hostname)) return false;
      var pa = a.port || (a.protocol === 'https:' ? '443' : '80');
      var pb = b.port || (b.protocol === 'https:' ? '443' : '80');
      return pa === pb && a.protocol === b.protocol;
    } catch (e) {
      return false;
    }
  }

  function apiLog(entry) {
    if (typeof window !== 'undefined' && window.StudioAPILog && window.StudioAPILog.append) {
      window.StudioAPILog.append(entry);
    }
  }

  /**
   * Synthetic EXT row for server routes that call Vertex/Kie/ElevenLabs/etc. but do not push pipeline-events
   * until a job exists. Shown when "External APIs only" is enabled.
   */
  function apiLogThirdParty(bodySummary, status, responseSummary) {
    apiLog({
      method: 'EXT',
      path: 'Studio → server → third-party',
      bodySummary: bodySummary || '',
      status: status == null ? 200 : status,
      responseSummary: responseSummary || ''
    });
  }

  function bodySummary(body, maxLen) {
    if (body == null) return '';
    if (typeof body !== 'object') return String(body).slice(0, maxLen || 80);
    var keys = Object.keys(body);
    var parts = [];
    if (body.job_id) parts.push('job_id:' + body.job_id);
    if (body.seed_job_id) parts.push('seed_job_id:' + body.seed_job_id);
    if (body.video_type) parts.push('video_type:' + body.video_type);
    if (body.prompt && typeof body.prompt === 'string') parts.push('prompt:' + body.prompt.length + 'ch');
    if (body.pause_after_step) parts.push('pause_after:' + body.pause_after_step);
    if (parts.length) return parts.join(', ');
    return keys.length + ' keys';
  }

  function getBaseUrl() {
    const stored = localStorage.getItem(STORAGE_KEY_BASE);
    const pageOrigin =
      typeof window !== 'undefined' && window.location && window.location.origin
        ? window.location.origin
        : '';
    if (stored) {
      const s = stored.replace(/\/$/, '');
      if (pageOrigin && _loopbackSameService(s, pageOrigin)) {
        return pageOrigin.replace(/\/$/, '');
      }
      return s;
    }
    return pageOrigin ? pageOrigin.replace(/\/$/, '') : '';
  }

  function setBaseUrl(url) {
    if (url) localStorage.setItem(STORAGE_KEY_BASE, url.replace(/\/$/, ''));
    else localStorage.removeItem(STORAGE_KEY_BASE);
  }

  function getApiKey() {
    return localStorage.getItem(STORAGE_KEY_API_KEY) || '';
  }

  function setApiKey(key) {
    if (key) localStorage.setItem(STORAGE_KEY_API_KEY, key);
    else localStorage.removeItem(STORAGE_KEY_API_KEY);
  }

  function getAuthHeaders() {
    const key = getApiKey();
    const headers = { 'Content-Type': 'application/json' };
    if (key) headers['Authorization'] = 'Bearer ' + key;
    return headers;
  }

  async function getAuthHeadersWithStudioUser() {
    const headers = Object.assign({}, getAuthHeaders());
    if (typeof StudioAuth !== 'undefined' && StudioAuth.getAccessToken) {
      try {
        const t = await StudioAuth.getAccessToken();
        if (t) {
          headers['X-Studio-User-Token'] = t;
          // Keep Authorization alongside JWT: the API accepts a valid sk-tvd Bearer first, and if the
          // Bearer is unknown (e.g. placeholder "dev"), it falls back to JWT + STUDIO_FALLBACK_API_KEY
          // or a single api_tenants row (see api_pipeline/auth.py _resolve_tenant_for_request).
        }
      } catch (_) {}
    }
    return headers;
  }

  /**
   * True if the next protected API call can send either Bearer sk-tvd-... or X-Studio-User-Token.
   * Call before /api/generate so step 4–5 auto-start fails fast with a clear message (not a raw 401).
   */
  async function ensureCanCallProtectedApi() {
    const k = getApiKey();
    const cloud =
      typeof StudioAuth !== 'undefined' &&
      StudioAuth.isCloudConfigured &&
      StudioAuth.isCloudConfigured();
    if (k && !(cloud && k === 'dev')) return true;
    if (typeof StudioAuth === 'undefined' || !StudioAuth.getAccessToken) return false;
    try {
      if (StudioAuth.isAuthEnabled && !StudioAuth.isAuthEnabled()) return false;
      const t = await StudioAuth.getAccessToken();
      return !!t;
    } catch (_) {
      return false;
    }
  }

  function mergeSessionIdIntoBody(body) {
    const b = body && typeof body === 'object' ? Object.assign({}, body) : {};
    if (typeof window !== 'undefined' && window._studioServerSessionId) {
      b.session_id = window._studioServerSessionId;
    }
    return b;
  }

  function waitPush(phase) {
    if (typeof StudioWaitingOverlay !== 'undefined' && StudioWaitingOverlay.push) {
      StudioWaitingOverlay.push(phase);
    }
  }

  function waitPop() {
    if (typeof StudioWaitingOverlay !== 'undefined' && StudioWaitingOverlay.pop) {
      StudioWaitingOverlay.pop();
    }
  }

  /** Nested retrySceneAnimations calls share one overlay. */
  var _retryAnimWaitDepth = 0;

  /**
   * POST /api/generate
   * @param {Object} body - GenerateVideoRequest payload
   * @returns {Promise<{ job_id, status, message, warnings? }>}
   */
  async function generate(body) {
    waitPush('start_pipeline');
    try {
      const url = getBaseUrl() + '/api/generate';
      const payload = mergeSessionIdIntoBody(body);
      const res = await fetch(url, {
        method: 'POST',
        headers: await getAuthHeadersWithStudioUser(),
        body: JSON.stringify(payload)
      });
      const text = await res.text();
      let data = null;
      try {
        if (text) data = JSON.parse(text);
      } catch (_) {}
      apiLog({ method: 'POST', path: '/api/generate', bodySummary: bodySummary(payload), status: res.status, responseSummary: data && data.job_id ? 'job_id:' + data.job_id : '' });
      if (res.ok) {
        apiLogThirdParty(
          '/api/generate — monolith outbound (Vertex, Kie, ElevenLabs, Rendi, ZapCap, Suno per pipeline)',
          res.status,
          data && data.job_id ? 'poll job pipeline-events for EXT/COST lines' : ''
        );
      }
      if (!res.ok) {
        let msg = 'Generate failed: ' + res.status;
        if (data && data.detail) {
          const d = data.detail;
          if (typeof d === 'string') msg = d;
          else if (Array.isArray(d)) {
            // FastAPI 422: [{loc:[...], msg:"...", type:"..."}, ...]
            msg = msg + ' — ' + d.map(function (e) {
              const loc = Array.isArray(e && e.loc) ? e.loc.join('.') : '';
              return (loc ? loc + ': ' : '') + ((e && e.msg) || JSON.stringify(e));
            }).join('; ');
          } else {
            msg = msg + ' — ' + JSON.stringify(d);
          }
        } else if (text) msg = msg + ' ' + text.slice(0, 200);
        throw new Error(msg);
      }
      return data;
    } finally {
      waitPop();
    }
  }

  /**
   * GET /api/jobs/{id}
   */
  async function fetchJobLogs(jobId) {
    const path = '/api/jobs/' + jobId + '/logs';
    const res = await fetch(getBaseUrl() + path, { headers: await getAuthHeadersWithStudioUser() });
    const text = await res.text();
    let data = null;
    try {
      if (text) data = JSON.parse(text);
    } catch (_) {}
    if (!res.ok) throw new Error('Job logs failed: ' + res.status);
    return data;
  }

  async function getJob(jobId) {
    const path = '/api/jobs/' + jobId;
    const url = getBaseUrl() + path;

    async function doFetch() {
      return fetch(url, { headers: await getAuthHeadersWithStudioUser() });
    }

    var res = await doFetch();
    if (res.status === 401 && typeof StudioAuth !== 'undefined' && StudioAuth.getAccessToken) {
      try {
        await StudioAuth.getAccessToken();
      } catch (_) {}
      res = await doFetch();
    }

    const text = await res.text();
    let data = null;
    try {
      if (text) data = JSON.parse(text);
    } catch (_) {}
    if (!res.ok) {
      var detail = '';
      if (data && data.detail != null) {
        detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
      }
      apiLog({
        method: 'GET',
        path: path,
        bodySummary: '',
        status: res.status,
        responseSummary: detail ? detail.slice(0, 200) : 'get job error'
      });
      throw new Error('Get job failed: ' + res.status + (detail ? ' — ' + detail : ''));
    }

    var sig =
      data && data.status != null
        ? 'st:' +
          String(data.status) +
          (data.current_step != null ? ' step:' + String(data.current_step) : '') +
          (data.progress != null ? ' p:' + String(data.progress) : '')
        : 'ok';
    var now = Date.now();
    var st = _getJobLogThrottle[jobId] || { t: 0, sig: '' };
    if (now - st.t >= GET_JOB_LOG_MIN_MS || st.sig !== sig) {
      _getJobLogThrottle[jobId] = { t: now, sig: sig };
      apiLog({
        method: 'GET',
        path: path,
        status: res.status,
        responseSummary: sig.slice(0, 160)
      });
    }
    return data;
  }

  /**
   * POST /api/jobs/{id}/resume — resume a paused job (continues from checkpoints).
   */
  /**
   * @param {string} jobId
   * @param {{ stop_after_scene_animations?: boolean }} [opts] - true = pause after animations_review; false = run to final video; omit = server infers (product → pause before Rendi unless already at animation review)
   */
  async function resumeJob(jobId, opts) {
    waitPush('resume');
    try {
      const path = '/api/jobs/' + jobId + '/resume';
      // Must send explicit false for "run to final video"; omitting the key uses server defaults
      // (product video may re-set pause_after_step=step_12 if current_step is stale, e.g. still "music").
      const bodyPayload = {};
      if (opts != null && typeof opts.stop_after_scene_animations === 'boolean') {
        bodyPayload.stop_after_scene_animations = opts.stop_after_scene_animations;
      }
      const body = JSON.stringify(bodyPayload);
      const res = await fetch(getBaseUrl() + path, {
        method: 'POST',
        headers: Object.assign({}, await getAuthHeadersWithStudioUser(), { 'Content-Type': 'application/json' }),
        body: body
      });
      const text = await res.text();
      let data = null;
      try {
        if (text) data = JSON.parse(text);
      } catch (_) {}
      apiLog({ method: 'POST', path: path, status: res.status, responseSummary: data && data.job_id ? 'job_id:' + data.job_id : '' });
      if (res.ok) {
        apiLogThirdParty(path + ' — monolith resumes (third-party calls continue)', res.status, 'watch EXT/COST on job poll');
      }
      if (!res.ok) throw new Error('Resume failed: ' + res.status + ' ' + text);
      return data;
    } finally {
      waitPop();
    }
  }

  /**
   * POST /api/jobs/{id}/retry — retry a failed job; keeps Supabase intermediates (scene images/videos, VO, etc.).
   * Use when final assembly (Rendi) failed but clips already exist — do NOT use retry-scene-animations for that.
   * @returns {Promise<object>} API response plus _studioRetryKind: 'final_assembly' for UI copy.
   */
  async function retryFailedJob(jobId) {
    waitPush('resume');
    try {
      const path = '/api/jobs/' + jobId + '/retry';
      const res = await fetch(getBaseUrl() + path, {
        method: 'POST',
        headers: await getAuthHeadersWithStudioUser(),
        body: '{}'
      });
      const text = await res.text();
      let data = null;
      try {
        if (text) data = JSON.parse(text);
      } catch (_) {}
      apiLog({
        method: 'POST',
        path: path,
        status: res.status,
        responseSummary: data && data.job_id ? 'retry job_id:' + data.job_id : ''
      });
      if (res.ok) {
        apiLogThirdParty(path + ' — monolith retry (Rendi/ZapCap etc.)', res.status, 'watch EXT/COST on job poll');
      }
      if (!res.ok) throw new Error('Retry job failed: ' + res.status + ' ' + text);
      return Object.assign({}, data || {}, { _studioRetryKind: 'final_assembly' });
    } finally {
      waitPop();
    }
  }

  /**
   * POST /api/animate-scene — re-animate a single scene with optional motion prompt override.
   * @param {{ job_id: string, scene_index: number, motion_prompt?: string, image_url?: string, duration?: number }} body
   * @returns {Promise<{ scene_index: number, video_url: string }>}
   */
  async function animateScene(body) {
    const path = '/api/animate-scene';
    const res = await fetch(getBaseUrl() + path, {
      method: 'POST',
      headers: await getAuthHeadersWithStudioUser(),
      body: JSON.stringify(body || {})
    });
    const text = await res.text();
    let data = null;
    try {
      if (text) data = JSON.parse(text);
    } catch (_) {}
    apiLog({
      method: 'POST',
      path: path,
      bodySummary: 'scene=' + ((body && body.scene_index) != null ? body.scene_index : '?'),
      status: res.status,
      responseSummary: data && data.video_url ? 'video_url' : ''
    });
    if (res.ok) {
      apiLogThirdParty(path + ' — monolith _generate_video (Veo / Kling / Runway) for one scene', res.status, data && data.video_url ? 'video_url' : '');
    }
    if (!res.ok) {
      let msg = 'Animate scene failed: ' + res.status;
      if (data && data.detail) {
        msg = msg + ' — ' + (typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail));
      } else if (text) {
        msg = msg + ' ' + text.slice(0, 200);
      }
      throw new Error(msg);
    }
    return data;
  }

  /**
   * POST /api/jobs/{id}/retry-scene-animations — clear clips and re-run animations (or pause first if processing).
   */
  async function retrySceneAnimations(jobId) {
    if (_retryAnimWaitDepth === 0) waitPush('retry_animations');
    _retryAnimWaitDepth++;
    try {
      const path = '/api/jobs/' + jobId + '/retry-scene-animations';
      const res = await fetch(getBaseUrl() + path, {
        method: 'POST',
        headers: await getAuthHeadersWithStudioUser(),
        body: '{}'
      });
      const text = await res.text();
      let data = null;
      try {
        if (text) data = JSON.parse(text);
      } catch (_) {}
      apiLog({
        method: 'POST',
        path: path,
        status: res.status,
        responseSummary: data && data.phase ? 'phase:' + data.phase : ''
      });
      if (res.ok) {
        apiLogThirdParty(path + ' — monolith re-runs animations (Kie/Vertex video APIs)', res.status, 'watch EXT/COST on job poll');
      }
      if (!res.ok) throw new Error('Retry animations failed: ' + res.status + ' ' + text);
      if (data && data.phase === 'pause_requested') {
        for (var i = 0; i < 120; i++) {
          await new Promise(function (r) {
            setTimeout(r, 2000);
          });
          var job = await getJob(jobId);
          if (job.status === 'paused') return retrySceneAnimations(jobId);
          if (job.status === 'failed' || job.status === 'completed' || job.status === 'aborted') {
            throw new Error('Job became ' + job.status + ' while waiting for pause. Use Retry again or the Scene assets step (10).');
          }
        }
        throw new Error('Timed out waiting for pause (video API may still be running). Try again in a few minutes.');
      }
      return data;
    } finally {
      _retryAnimWaitDepth--;
      if (_retryAnimWaitDepth === 0) waitPop();
    }
  }

  /**
   * POST /api/jobs/{id}/restart?from_step=...
   * @param {string} jobId
   * @param {string} fromStep
   */
  async function restartJob(jobId, fromStep) {
    waitPush('restart');
    try {
      const path = '/api/jobs/' + jobId + '/restart?from_step=' + encodeURIComponent(fromStep);
      const res = await fetch(getBaseUrl() + path, {
        method: 'POST',
        headers: await getAuthHeadersWithStudioUser()
      });
      const text = await res.text();
      let data = null;
      try {
        if (text) data = JSON.parse(text);
      } catch (_) {}
      apiLog({
        method: 'POST',
        path: path,
        status: res.status,
        responseSummary: data && data.job_id ? 'restart job_id:' + data.job_id : ''
      });
      if (res.ok) {
        apiLogThirdParty(path + ' — monolith restart from step', res.status, 'watch EXT/COST on job poll');
      }
      if (!res.ok) throw new Error('Restart job failed: ' + res.status + ' ' + text);
      return data;
    } finally {
      waitPop();
    }
  }

  /**
   * PATCH /api/jobs/{id}/intermediates — merge intermediate key(s) into job (e.g. before resume).
   * @param {string} jobId
   * @param {Object} body — { key, value } or { intermediates: { key1: val1, ... } }
   * @returns {Promise<{ ok: boolean }>}
   */
  /**
   * @param {string} jobId
   * @param {Object} body
   * @param {{ skipWaitingOverlay?: boolean }} [options] - set true for quick saves while the UI already shows inline progress (e.g. scene image regen).
   */
  async function patchIntermediates(jobId, body, options) {
    var skipOverlay = options && options.skipWaitingOverlay;
    if (!skipOverlay) waitPush('patch');
    try {
      const path = '/api/jobs/' + jobId + '/intermediates';
      const res = await fetch(getBaseUrl() + path, {
        method: 'PATCH',
        headers: await getAuthHeadersWithStudioUser(),
        body: JSON.stringify(body)
      });
      const text = await res.text();
      let data = null;
      try {
        if (text) data = JSON.parse(text);
      } catch (_) {}
      var keys = body && body.intermediates ? Object.keys(body.intermediates).join(',') : (body && body.key ? body.key : '');
      apiLog({ method: 'PATCH', path: path, bodySummary: keys ? 'intermediates: ' + keys : bodySummary(body), status: res.status });
      if (!res.ok) throw new Error('Patch intermediates failed: ' + res.status + ' ' + text);
      return data;
    } finally {
      if (!skipOverlay) waitPop();
    }
  }

  /**
   * POST /api/generate-music — standalone music (description + Suno URL).
   * @param {Object} body — text_1, text_2, text_3, vo_script, language, video_type, music_description_override?
   * @returns {Promise<{ music_description: string, music_url: string }>}
   */
  async function generateMusic(body) {
    // No full-screen overlay: music often runs while the user edits the VO script (step 7); hints + player update in the UI.
    const path = '/api/generate-music';
    const res = await fetch(getBaseUrl() + path, {
      method: 'POST',
      headers: await getAuthHeadersWithStudioUser(),
      body: JSON.stringify(body)
    });
    const text = await res.text();
    let data = null;
    try {
      if (text) data = JSON.parse(text);
    } catch (_) {}
    apiLog({ method: 'POST', path: path, bodySummary: bodySummary(body), status: res.status, responseSummary: data && data.music_url ? 'music_url' : '' });
    if (res.ok) {
      apiLogThirdParty(path + ' — Suno via Kie.ai on server', res.status, data && data.music_url ? 'music_url' : '');
    }
    if (!res.ok) throw new Error('Generate music failed: ' + res.status + ' ' + text);
    return data;
  }

  /**
   * GET /api/voices — suggested ElevenLabs voice IDs for language (and optional gender).
   * @param {string} language — ISO 639-1 (e.g. "he", "en")
   * @param {string} [gender] — "m"/"male" or "f"/"female"
   * @returns {Promise<Array<{ voice_id: string, label: string }>>}
   */
  async function getVoices(language, gender) {
    // No full-screen overlay: the server only maps language/gender to IDs from 11_labs.json (no ElevenLabs list call).
    // Step 7 already shows "Loading voices…" in the dropdown; blocking the whole page was misleading when the API is slow to reach (Tailscale / auth).
    const path = '/api/voices?language=' + encodeURIComponent(language || 'en') + (gender ? '&gender=' + encodeURIComponent(gender) : '');
    const res = await fetch(getBaseUrl() + path, { method: 'GET', headers: await getAuthHeadersWithStudioUser() });
    const text = await res.text();
    let data = null;
    try {
      if (text) data = JSON.parse(text);
    } catch (_) {}
    apiLog({ method: 'GET', path: '/api/voices', bodySummary: 'language=' + (language || 'en'), status: res.status, responseSummary: Array.isArray(data) ? data.length + ' voices' : '' });
    if (!res.ok) throw new Error('Get voices failed: ' + res.status + ' ' + text);
    return data;
  }

  /**
   * POST /api/generate-vo — generate VO audio via ElevenLabs, returns GCS URL.
   * @param {Object} body — vo_script, language, voice_id, video_type, job_id?
   * @returns {Promise<{ vo_audio_url: string, vo_duration?: number, vo_word_segments?: array }>}
   */
  async function generateVo(body) {
    // No full-screen overlay: ElevenLabs can take 10–60s on long scripts; the VO step button shows "Generating…".
    const path = '/api/generate-vo';
    const payload = Object.assign({ with_word_timestamps: false }, body || {});
    const res = await fetch(getBaseUrl() + path, {
      method: 'POST',
      headers: await getAuthHeadersWithStudioUser(),
      body: JSON.stringify(payload)
    });
    const text = await res.text();
    let data = null;
    try {
      if (text) data = JSON.parse(text);
    } catch (_) {}
    apiLog({ method: 'POST', path: path, bodySummary: bodySummary(body), status: res.status, responseSummary: data && data.vo_audio_url ? 'vo_audio_url' : '' });
    if (res.ok) {
      apiLogThirdParty(path + ' — ElevenLabs TTS (+ GCS upload on server)', res.status, data && data.vo_audio_url ? 'vo_audio_url' : '');
    }
    if (!res.ok) throw new Error('Generate VO failed: ' + res.status + ' ' + text);
    return data;
  }

  /**
   * POST /api/voice-design — run ElevenLabs Design-a-Voice and return preview objects.
   * The server builds voice_description automatically from character_description / character_image_url.
   * @param {{ language: string, gender?: string, character_description?: string, character_image_url?: string, seed?: number }} body
   * @returns {Promise<{ previews: Array<{ generated_voice_id, audio_base_64, media_type, duration_secs }>, text: string, voice_description: string }>}
   */
  async function designVoice(body) {
    const path = '/api/voice-design';
    const res = await fetch(getBaseUrl() + path, {
      method: 'POST',
      headers: await getAuthHeadersWithStudioUser(),
      body: JSON.stringify(body || {})
    });
    const text = await res.text();
    let data = null;
    try {
      if (text) data = JSON.parse(text);
    } catch (_) {}
    apiLog({ method: 'POST', path: path, bodySummary: bodySummary(body), status: res.status, responseSummary: data && data.previews ? data.previews.length + ' previews' : '' });
    if (res.ok) {
      apiLogThirdParty(path + ' — ElevenLabs Design-a-Voice (+ optional Gemini for description)', res.status, data && data.previews ? data.previews.length + ' previews' : '');
    }
    if (!res.ok) {
      var dmsg = data && data.detail ? (typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)) : '';
      throw new Error('Voice design failed: ' + res.status + (dmsg ? ' — ' + dmsg : ''));
    }
    return data;
  }

  /**
   * GET /api/voice-preview/{generated_voice_id}/stream — URL with auth query params.
   * Browsers cannot send Authorization / X-Studio-User-Token on <audio src>, so we append
   * ?token= or ?studio_user_token= (same pattern as SSE).
   * @param {string} generatedVoiceId
   * @returns {Promise<string>}
   */
  async function voicePreviewStreamUrlWithAuth(generatedVoiceId) {
    var path = '/api/voice-preview/' + encodeURIComponent(generatedVoiceId) + '/stream';
    var base = getBaseUrl() + path;
    var u;
    try {
      u = new URL(base);
    } catch (e1) {
      try {
        u = new URL(path, typeof window !== 'undefined' && window.location ? window.location.origin : 'http://localhost');
      } catch (e2) {
        return base;
      }
    }
    var key = getApiKey();
    var jwt = null;
    if (typeof StudioAuth !== 'undefined' && StudioAuth.getAccessToken) {
      try {
        jwt = await StudioAuth.getAccessToken();
      } catch (_) {}
    }
    if (key && key !== 'dev') {
      u.searchParams.set('token', key);
    } else if (jwt) {
      u.searchParams.set('studio_user_token', jwt);
    } else if (key) {
      u.searchParams.set('token', key);
    }
    u.searchParams.set('_t', String(Date.now()));
    return u.toString();
  }

  /**
   * POST /api/voice-save — save a designed voice to the ElevenLabs library.
   * Converts a temporary generated_voice_id into a permanent voice_id usable for TTS.
   * @param {{ generated_voice_id: string, voice_name?: string, voice_description?: string }} body
   * @returns {Promise<{ voice_id: string, voice_name: string }>}
   */
  async function saveDesignedVoice(body) {
    const path = '/api/voice-save';
    const res = await fetch(getBaseUrl() + path, {
      method: 'POST',
      headers: await getAuthHeadersWithStudioUser(),
      body: JSON.stringify(body || {})
    });
    const text = await res.text();
    let data = null;
    try {
      if (text) data = JSON.parse(text);
    } catch (_) {}
    apiLog({ method: 'POST', path: path, bodySummary: bodySummary(body), status: res.status, responseSummary: data && data.voice_id ? 'voice_id:' + data.voice_id.slice(0, 12) : '' });
    if (res.ok) {
      apiLogThirdParty(path + ' — ElevenLabs save designed voice', res.status, data && data.voice_id ? 'voice_id' : '');
    }
    if (!res.ok) {
      var dmsg = data && data.detail ? (typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)) : '';
      throw new Error('Save voice failed: ' + res.status + (dmsg ? ' — ' + dmsg : ''));
    }
    return data;
  }

  /**
   * POST /api/generate-character — auto portrait in the background when user did not upload a character (Studio).
   * @param {Object} body — prompt?, character_description?, video_type, gender?, country?, language?, visual_style?, correction_text? (prompt and/or character_description required)
   * @returns {Promise<{ image_url?: string, description?: string }>}
   */
  async function generateCharacter(body) {
    // Background-only in Studio: never block the UI with the full-screen waiting overlay.
    const path = '/api/generate-character';
    const res = await fetch(getBaseUrl() + path, {
      method: 'POST',
      headers: await getAuthHeadersWithStudioUser(),
      body: JSON.stringify(body || {})
    });
    const text = await res.text();
    let data = null;
    try {
      if (text) data = JSON.parse(text);
    } catch (_) {}
    apiLog({ method: 'POST', path: path, bodySummary: bodySummary(body), status: res.status, responseSummary: data && data.image_url ? 'image_url' : '' });
    if (res.ok) {
      apiLogThirdParty(path + ' — image gen (Vertex / Kie on server)', res.status, data && data.image_url ? 'image_url' : '');
    }
    if (!res.ok) {
      var dmsg = '';
      if (data && data.detail != null) {
        dmsg = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
      }
      var tail = dmsg || (text ? text.slice(0, 400) : '');
      throw new Error('Generate character failed: ' + res.status + (tail ? ' — ' + tail : ''));
    }
    return data;
  }

  /**
   * POST /api/suggest-character-briefs — short character-look lines from the step-3 prompt (Vertex Gemini Flash-Lite class).
   * @param {Object} body — prompt (required), language?, country?, video_type?
   * @returns {Promise<{ suggestions: string[] }>}
   */
  async function suggestCharacterBriefs(body) {
    const path = '/api/suggest-character-briefs';
    const res = await fetch(getBaseUrl() + path, {
      method: 'POST',
      headers: await getAuthHeadersWithStudioUser(),
      body: JSON.stringify(body || {})
    });
    const text = await res.text();
    let data = null;
    try {
      if (text) data = JSON.parse(text);
    } catch (_) {}
    apiLog({
      method: 'POST',
      path: path,
      bodySummary: bodySummary(body),
      status: res.status,
      responseSummary: data && data.suggestions ? 'n=' + data.suggestions.length : ''
    });
    if (res.ok) {
      apiLogThirdParty(path + ' — Vertex Gemini (text) on server', res.status, data && data.suggestions ? 'n=' + data.suggestions.length : '');
    }
    if (!res.ok) {
      var dmsg = '';
      if (data && data.detail != null) {
        dmsg = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
      }
      var tail = dmsg || (text ? text.slice(0, 400) : '');
      throw new Error('Suggest character briefs failed: ' + res.status + (tail ? ' — ' + tail : ''));
    }
    return data;
  }

  /**
   * POST /api/generate-scene-image — single scene image (optional correction text).
   * @param {Object} body — image_prompt, correction_text?, visual_style, video_type, image_api, reference_image_urls?, character_reference_urls?, has_character, product_description?, is_cta_scene, logo_reference_url?
   * @returns {Promise<{ image_url: string }>}
   */
  async function generateSceneImage(body) {
    // No full-screen overlay: each card shows "Regenerating…" / placeholder so the user can edit other scenes in parallel.
    const path = '/api/generate-scene-image';
    const res = await fetch(getBaseUrl() + path, {
      method: 'POST',
      headers: await getAuthHeadersWithStudioUser(),
      body: JSON.stringify(body)
    });
    const text = await res.text();
    let data = null;
    try {
      if (text) data = JSON.parse(text);
    } catch (_) {}
    apiLog({ method: 'POST', path: path, bodySummary: bodySummary(body), status: res.status, responseSummary: data && data.image_url ? 'image_url' : '' });
    if (res.ok) {
      apiLogThirdParty(path + ' — scene image API (Vertex Gemini / Kie on server)', res.status, data && data.image_url ? 'image_url' : '');
    }
    if (!res.ok) throw new Error('Generate scene image failed: ' + res.status + ' ' + text);
    return data;
  }

  /**
   * Connect to SSE stream for job events.
   * @param {string} jobId
   * @param {function(eventType: string, data: object)} onEvent
   * @returns {Promise<EventSource>} resolves when EventSource is created (caller may .close())
   */
  async function connectSSE(jobId, onEvent) {
    const path = '/api/jobs/' + jobId + '/events';
    apiLog({ method: 'SSE', path: path, bodySummary: 'connect' });
    let url = getBaseUrl() + path;
    const key = getApiKey();
    var jwt = null;
    if (typeof StudioAuth !== 'undefined' && StudioAuth.getAccessToken) {
      try {
        jwt = await StudioAuth.getAccessToken();
      } catch (_) {}
    }
    const q = [];
    if (key && key !== 'dev') {
      q.push('token=' + encodeURIComponent(key));
    } else if (jwt) {
      q.push('studio_user_token=' + encodeURIComponent(jwt));
    } else if (key) {
      q.push('token=' + encodeURIComponent(key));
    }
    if (q.length) url += (url.indexOf('?') >= 0 ? '&' : '?') + q.join('&');
    const es = new EventSource(url);
    es.onmessage = function (e) {
      try {
        const msg = JSON.parse(e.data);
        const eventType = msg.event || msg.step || 'message';
        onEvent(eventType, msg);
      } catch (err) {
        onEvent('raw', { data: e.data });
      }
    };
    es.onerror = function () {
      onEvent('error', {});
    };
    return es;
  }

  /**
   * POST /api/upload - multipart form with file
   * @param {File} file
   * @returns {Promise<string>} URL of uploaded file
   */
  /**
   * Character library (GET /api/characters) — requires Bearer or Studio JWT + SUPABASE_SERVICE_ROLE_KEY on server.
   * @returns {Promise<object[]>}
   */
  async function listCharacters() {
    const path = '/api/characters';
    const res = await fetch(getBaseUrl() + path, { headers: await getAuthHeadersWithStudioUser() });
    const text = await res.text();
    let data = null;
    try {
      if (text) data = JSON.parse(text);
    } catch (_) {}
    apiLog({ method: 'GET', path: path, status: res.status, responseSummary: Array.isArray(data) ? data.length + ' items' : '' });
    if (!res.ok) {
      var detL = data && data.detail != null ? (typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)) : (text || '').slice(0, 280);
      throw new Error('List characters failed: ' + res.status + ' ' + detL);
    }
    return Array.isArray(data) ? data : [];
  }

  async function createCharacter(body) {
    const path = '/api/characters';
    const url = getBaseUrl() + path;
    let res;
    try {
      res = await fetch(url, {
        method: 'POST',
        headers: Object.assign({}, await getAuthHeadersWithStudioUser(), { 'Content-Type': 'application/json' }),
        body: JSON.stringify(body || {})
      });
    } catch (netErr) {
      throw new Error('Network error reaching ' + url + ' — check Studio server selector. (' + (netErr.message || netErr) + ')');
    }
    const text = await res.text();
    let data = null;
    try {
      if (text) data = JSON.parse(text);
    } catch (_) {}
    apiLog({ method: 'POST', path: path, status: res.status, responseSummary: data && data.character_id ? 'id:' + data.character_id : '' });
    if (!res.ok) {
      var detC = data && data.detail != null ? (typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)) : (text || '').slice(0, 280);
      throw new Error('Create character failed: ' + res.status + ' ' + detC);
    }
    return data;
  }

  async function getCharacter(characterId) {
    const path = '/api/characters/' + encodeURIComponent(characterId);
    const res = await fetch(getBaseUrl() + path, { headers: await getAuthHeadersWithStudioUser() });
    const text = await res.text();
    let data = null;
    try {
      if (text) data = JSON.parse(text);
    } catch (_) {}
    if (!res.ok) throw new Error('Get character failed: ' + res.status);
    return data;
  }

  async function updateCharacter(characterId, body) {
    const path = '/api/characters/' + encodeURIComponent(characterId);
    const res = await fetch(getBaseUrl() + path, {
      method: 'PUT',
      headers: Object.assign({}, await getAuthHeadersWithStudioUser(), { 'Content-Type': 'application/json' }),
      body: JSON.stringify(body || {})
    });
    const text = await res.text();
    let data = null;
    try {
      if (text) data = JSON.parse(text);
    } catch (_) {}
    if (!res.ok) throw new Error('Update character failed: ' + res.status + ' ' + (text || '').slice(0, 200));
    return data;
  }

  async function deleteCharacter(characterId) {
    const path = '/api/characters/' + encodeURIComponent(characterId);
    const res = await fetch(getBaseUrl() + path, {
      method: 'DELETE',
      headers: await getAuthHeadersWithStudioUser()
    });
    const text = await res.text();
    if (!res.ok) throw new Error('Delete character failed: ' + res.status + ' ' + (text || '').slice(0, 200));
    try {
      return JSON.parse(text);
    } catch (_) {
      return { ok: true };
    }
  }

  async function uploadFile(file) {
    waitPush('upload');
    try {
      const path = '/api/upload';
      const form = new FormData();
      form.append('file', file);
      const headers = Object.assign({}, await getAuthHeadersWithStudioUser());
      delete headers['Content-Type'];
      const res = await fetch(getBaseUrl() + path, { method: 'POST', headers, body: form });
      const text = await res.text();
      let data = null;
      try {
        if (text) data = JSON.parse(text);
      } catch (_) {}
      apiLog({ method: 'POST', path: path, bodySummary: file && file.name ? 'file: ' + file.name : '', status: res.status, responseSummary: data && data.url ? 'url' : '' });
      if (res.ok) {
        apiLogThirdParty(path + ' — file stored (e.g. GCS) on server', res.status, data && data.url ? 'url' : '');
      }
      if (!res.ok) throw new Error('Upload failed: ' + res.status + ' ' + text);
      if (data && data.url) return data.url;
      throw new Error('Upload response missing url');
    } finally {
      waitPop();
    }
  }

  return {
    getBaseUrl,
    setBaseUrl,
    getApiKey,
    setApiKey,
    getAuthHeaders,
    getAuthHeadersWithStudioUser,
    ensureCanCallProtectedApi,
    generate,
    getJob,
    fetchJobLogs,
    resumeJob,
    retryFailedJob,
    retrySceneAnimations,
    animateScene,
    restartJob,
    patchIntermediates,
    generateMusic,
    getVoices,
    generateVo,
    generateCharacter,
    suggestCharacterBriefs,
    generateSceneImage,
    connectSSE,
    uploadFile,
    listCharacters,
    createCharacter,
    getCharacter,
    updateCharacter,
    deleteCharacter,
    designVoice,
    voicePreviewStreamUrlWithAuth,
    saveDesignedVoice
  };
})();

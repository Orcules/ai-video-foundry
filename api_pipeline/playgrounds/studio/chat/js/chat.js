/* VidBuddy chat studio — message loop, side-panel mounter, SSE bridge, uploads. */
(function () {
  'use strict';

  // ────────────────────────────────────────────────────────────────
  // State
  // ────────────────────────────────────────────────────────────────
  // Reuse the same localStorage key as the classic studio so a user who's
  // already signed in / set their key there is auto-authenticated here.
  const STORAGE_KEY_API = 'studio_api_key';
  const STORAGE_KEY_SESSION = 'vbChatSessionId';
  let cloudAuthEnabled = false;
  let signinMode = 'signin';  // or 'signup'
  let sessionId = null;
  let jobId = null;
  let pendingAttachments = []; // [{url, kind, name}]
  let lastJobSnapshot = null;
  let sseConnection = null;
  let pollTimer = null;
  let currentPanel = null;
  // Storyboard the chat agent built. Mutated in-place by the review panel as
  // the user edits scenes/clips. Sent verbatim to PATCH /storyboard on save and
  // to POST /commit-custom on Generate.
  let currentStoryboard = null;

  // ────────────────────────────────────────────────────────────────
  // DOM
  // ────────────────────────────────────────────────────────────────
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));
  const els = {
    thread: $('#chatThread'),
    composer: $('#chatComposer'),
    input: $('#chatInput'),
    sendBtn: $('#chatSendBtn'),
    typing: $('#typingIndicator'),
    attachBtn: $('#attachBtn'),
    attachInput: $('#attachInput'),
    attachments: $('#chatAttachments'),
    sideEmpty: $('#sideEmpty'),
    sideContent: $('#sideContent'),
    sidePanel: $('#sidePanel'),
    authBanner: $('#authBanner'),
    apiKeyInput: $('#apiKeyInput'),
    apiKeySaveBtn: $('#apiKeySaveBtn'),
    resetBtn: $('#resetSessionBtn'),
    costChip: $('#costChip'),
    costChipText: $('#costChipText'),
    signinOverlay: $('#signinOverlay'),
    signinForm: $('#signinForm'),
    signinEmail: $('#signinEmail'),
    signinPassword: $('#signinPassword'),
    signinSubmit: $('#signinSubmit'),
    signinSubmitLabel: $('#signinSubmitLabel'),
    signinError: $('#signinError'),
    signinModeHint: $('#signinModeHint'),
    signinToggleText: $('#signinToggleText'),
    signinToggleBtn: $('#signinToggleBtn'),
  };

  // ────────────────────────────────────────────────────────────────
  // Auth
  // ────────────────────────────────────────────────────────────────
  function getApiKey() {
    return localStorage.getItem(STORAGE_KEY_API) || '';
  }

  function setApiKey(key) {
    if (key) localStorage.setItem(STORAGE_KEY_API, key);
    else localStorage.removeItem(STORAGE_KEY_API);
    refreshAuthBanner();
  }

  function refreshAuthBanner() {
    const has = !!getApiKey();
    els.authBanner.hidden = has;
    els.input.disabled = !has;
    els.attachBtn.disabled = !has;
    if (!has) {
      // Friendlier copy: point to the sign-in flow in the classic studio if
      // cloud auth is configured. Otherwise just say "API key".
      const txt = els.authBanner.querySelector('.chat-auth-text');
      if (cloudAuthEnabled && txt) {
        txt.innerHTML = 'Sign in via the <a href="/studio/" style="color:var(--vb-primary);font-weight:700;">classic studio</a> first, then come back here. Or paste an API key below.';
      } else if (txt) {
        txt.textContent = 'Enter your API key to start.';
      }
      els.input.placeholder = 'Sign in or paste an API key above to start chatting…';
    } else {
      els.input.placeholder = 'Tell me about the video you want to make…';
    }
  }

  async function autoConfigureAuth() {
    // 1) Initialize StudioAuth (Supabase). Determines if cloud sign-in is on
    //    AND restores any existing session from localStorage automatically.
    if (typeof StudioAuth !== 'undefined' && StudioAuth.init) {
      try {
        await StudioAuth.init();
        cloudAuthEnabled = !!(StudioAuth.isCloudConfigured && StudioAuth.isCloudConfigured());
      } catch (_) { /* ignore */ }
    } else {
      try {
        const res = await fetch('/api/config');
        if (res.ok) {
          const cfg = await res.json();
          cloudAuthEnabled = !!(cfg.studio_cloud_available || cfg.studio_auth_enabled);
        }
      } catch (_) { /* ignore */ }
    }

    // 2) When cloud auth is on: a Supabase JWT is enough — Bearer can stay 'dev'.
    //    The server resolves the implicit tenant from the JWT + STUDIO_FALLBACK_API_KEY.
    if (cloudAuthEnabled) {
      if (!getApiKey()) setApiKey('dev');
      return;
    }

    // 3) Cloud auth NOT configured: any non-empty Bearer works (dev tenant).
    if (!getApiKey()) setApiKey('dev');
  }

  async function isSignedInToCloud() {
    if (!cloudAuthEnabled) return true; // sign-in not required
    if (typeof StudioAuth === 'undefined' || !StudioAuth.getAccessToken) return false;
    try {
      const t = await StudioAuth.getAccessToken();
      return !!t;
    } catch (_) {
      return false;
    }
  }

  function showSigninOverlay(show) {
    if (!els.signinOverlay) return;
    els.signinOverlay.hidden = !show;
    if (show) setTimeout(() => els.signinEmail && els.signinEmail.focus(), 50);
  }

  function setSigninMode(mode) {
    signinMode = mode;
    const isUp = mode === 'signup';
    els.signinModeHint.textContent = isUp
      ? 'Create an account to start making videos.'
      : 'Welcome back. Sign in to start creating.';
    els.signinSubmitLabel.textContent = isUp ? 'Create account' : 'Sign in';
    els.signinToggleText.textContent = isUp ? 'Already have an account?' : 'New here?';
    els.signinToggleBtn.textContent = isUp ? 'Sign in' : 'Create an account';
    els.signinPassword.autocomplete = isUp ? 'new-password' : 'current-password';
  }

  function showSigninError(msg) {
    if (!els.signinError) {
      alert(msg); // last-ditch fallback if the DOM isn't what we expect
      return;
    }
    els.signinError.hidden = false;
    els.signinError.textContent = msg;
  }

  async function handleSignin(ev) {
    if (ev && ev.preventDefault) ev.preventDefault();
    if (els.signinError) {
      els.signinError.hidden = true;
      els.signinError.textContent = '';
    }
    const origLabel = els.signinSubmitLabel ? els.signinSubmitLabel.textContent : null;
    if (els.signinSubmit) els.signinSubmit.disabled = true;
    if (els.signinSubmitLabel) {
      els.signinSubmitLabel.textContent = signinMode === 'signup' ? 'Creating account…' : 'Signing in…';
    }
    const email = (els.signinEmail && els.signinEmail.value || '').trim();
    const pw = (els.signinPassword && els.signinPassword.value) || '';
    console.debug('[chat] handleSignin start', { mode: signinMode, hasEmail: !!email, hasPw: !!pw, hasStudioAuth: typeof StudioAuth !== 'undefined' });
    try {
      if (!email || !pw) {
        showSigninError('Please enter your email and password.');
        return;
      }
      if (typeof StudioAuth === 'undefined') {
        showSigninError('Auth library not loaded — refresh the page (Ctrl/Cmd+Shift+R).');
        return;
      }
      const fn = signinMode === 'signup' ? StudioAuth.signUp : StudioAuth.signIn;
      if (typeof fn !== 'function') {
        showSigninError('Auth not configured. Check the server console for /api/config errors.');
        return;
      }
      const res = await fn(email, pw);
      console.debug('[chat] handleSignin result', res);
      if (res && res.error) throw res.error;
      if (signinMode === 'signup') {
        const session = (res && res.data && res.data.session) || null;
        if (!session) {
          showSigninError('Account created — check your email for the confirmation link, then sign in.');
          setSigninMode('signin');
          return;
        }
      }
      showSigninOverlay(false);
      await startNewSession();
    } catch (e) {
      console.error('[chat] sign-in failed:', e);
      showSigninError((e && e.message) ? e.message : 'Sign-in failed.');
    } finally {
      if (els.signinSubmit) els.signinSubmit.disabled = false;
      if (els.signinSubmitLabel && origLabel != null) els.signinSubmitLabel.textContent = origLabel;
    }
  }

  // ────────────────────────────────────────────────────────────────
  // API helpers
  // ────────────────────────────────────────────────────────────────
  async function buildAuthHeaders(extra) {
    const headers = Object.assign({}, extra || {});
    const apiKey = getApiKey() || 'dev';
    headers['Authorization'] = `Bearer ${apiKey}`;
    // Include the Supabase JWT when the user is signed in. The server uses this
    // to map to STUDIO_FALLBACK_API_KEY (or a single api_tenants row) when the
    // Bearer is unknown — same path the classic studio uses.
    if (typeof StudioAuth !== 'undefined' && StudioAuth.getAccessToken) {
      try {
        const t = await StudioAuth.getAccessToken();
        if (t) headers['X-Studio-User-Token'] = t;
      } catch (_) { /* ignore — proceed without JWT */ }
    }
    return headers;
  }

  async function apiFetch(path, opts = {}) {
    const headers = await buildAuthHeaders(
      Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {})
    );
    const res = await fetch(path, Object.assign({}, opts, { headers }));
    if (!res.ok) {
      let detail = '';
      try {
        const body = await res.json();
        detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail || body);
      } catch (_) {
        detail = await res.text();
      }
      throw new Error(`HTTP ${res.status}: ${detail}`);
    }
    return res.json();
  }

  async function apiUpload(file, opts = {}) {
    const headers = await buildAuthHeaders();
    const fd = new FormData();
    fd.append('file', file);
    // F: opt-in Gemini Vision classification + slot-mismatch warning. Pass the
    // upload panel slot ("uploads_character"/"uploads_product"/etc.) so the
    // server can warn if the upload doesn't match.
    const qs = new URLSearchParams();
    if (opts.classify !== false) qs.set('classify', 'true');
    if (opts.slot) qs.set('slot', opts.slot);
    const url = qs.toString() ? `/api/upload?${qs}` : '/api/upload';
    const res = await fetch(url, { method: 'POST', headers, body: fd });
    if (!res.ok) {
      const t = await res.text();
      throw new Error(`Upload failed: ${t}`);
    }
    const result = await res.json();
    // Surface mismatch warning to the chat thread as a soft event so the user
    // sees it without being blocked.
    if (result.warning) {
      try { appendSystemEvent(`⚠ ${result.warning}`, 'warning'); } catch (_) {}
    }
    return result;
  }

  // ────────────────────────────────────────────────────────────────
  // Thread rendering
  // ────────────────────────────────────────────────────────────────
  function appendMessage(role, text, opts = {}) {
    const div = document.createElement('div');
    div.className = `chat-msg ${role}`;
    if (opts.iconHtml) {
      div.innerHTML = opts.iconHtml + escapeHtml(text);
    } else {
      div.textContent = text;
    }
    els.thread.appendChild(div);
    scrollThreadToBottom();
    return div;
  }

  function appendSystemEvent(text, iconName = 'info') {
    return appendMessage('system', text, {
      iconHtml: `<span class="material-symbols-outlined">${iconName}</span>`,
    });
  }

  function scrollThreadToBottom() {
    requestAnimationFrame(() => {
      els.thread.scrollTop = els.thread.scrollHeight;
    });
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;').replaceAll("'", '&#39;');
  }

  function setTyping(on) {
    els.typing.hidden = !on;
    if (on) scrollThreadToBottom();
  }

  // ────────────────────────────────────────────────────────────────
  // Summary card (rendered inline in thread)
  // ────────────────────────────────────────────────────────────────
  function renderSummaryCard(summary) {
    const card = document.createElement('div');
    card.className = 'chat-summary-card';
    const cost = summary.estimated_cost_usd_low != null
      ? `$${summary.estimated_cost_usd_low.toFixed(2)} – $${summary.estimated_cost_usd_high.toFixed(2)}`
      : 'estimating…';
    const wall = summary.estimated_wall_clock_min_low != null
      ? `${summary.estimated_wall_clock_min_low}–${summary.estimated_wall_clock_min_high} min`
      : '~5 min';
    const rows = [
      ['Pipeline', summary.video_type_label || ''],
      ['Goal', summary.user_goal || ''],
      ['Duration', summary.duration_seconds ? `${summary.duration_seconds}s` : ''],
      ['Style', summary.style || ''],
      ['Language', summary.language || ''],
      ['Country', summary.country || ''],
    ].filter(([_, v]) => v);

    const rowsHtml = rows.map(([k, v]) =>
      `<div class="chat-summary-row"><span class="label">${escapeHtml(k)}</span><span class="value">${escapeHtml(v)}</span></div>`
    ).join('');

    const highlightsHtml = (summary.highlights || []).slice(0, 6).map(h =>
      `<li><span class="material-symbols-outlined">check_circle</span><span>${escapeHtml(h)}</span></li>`
    ).join('');

    card.innerHTML = `
      <div class="chat-summary-card-title">
        <span class="material-symbols-outlined">auto_awesome</span>
        Here's the plan
      </div>
      <div class="chat-summary-grid">${rowsHtml}</div>
      ${highlightsHtml ? `<ul class="chat-summary-highlights">${highlightsHtml}</ul>` : ''}
      <div class="chat-summary-cost">
        <span class="label">Estimated cost · time</span>
        <span class="value">${escapeHtml(cost)} · ${escapeHtml(wall)}</span>
      </div>
      <div class="chat-summary-actions">
        <button type="button" class="chat-btn-primary" data-act="generate">
          <span class="material-symbols-outlined" style="font-size:1.1rem">rocket_launch</span>
          Generate
        </button>
        <button type="button" class="chat-btn-secondary" data-act="simulate">
          Simulate (free preview)
        </button>
        <button type="button" class="chat-btn-secondary" data-act="edit">
          Edit details
        </button>
      </div>
    `;
    card.querySelector('[data-act="generate"]').addEventListener('click', () => commitGeneration(false, card));
    card.querySelector('[data-act="simulate"]').addEventListener('click', () => commitGeneration(true, card));
    card.querySelector('[data-act="edit"]').addEventListener('click', () => {
      els.input.focus();
      els.input.value = 'I want to change something:';
      autoSizeInput();
    });
    els.thread.appendChild(card);
    scrollThreadToBottom();
    return card;
  }

  // ────────────────────────────────────────────────────────────────
  // Chat session lifecycle
  // ────────────────────────────────────────────────────────────────
  async function startNewSession() {
    sessionId = null;
    jobId = null;
    lastJobSnapshot = null;
    closeStreaming();
    els.thread.innerHTML = '';
    pendingAttachments = [];
    renderAttachments();
    showEmptySide();
    try {
      const res = await apiFetch('/api/studio-chat/start', {
        method: 'POST',
        body: JSON.stringify({}),
      });
      sessionId = res.session_id;
      localStorage.setItem(STORAGE_KEY_SESSION, sessionId);
      handleEnvelope(res.envelope, { skipUserEcho: true });
    } catch (e) {
      appendMessage('error', `Couldn't start a chat session: ${e.message}`);
    }
  }

  async function sendMessage(text) {
    if (!sessionId) {
      await startNewSession();
      if (!sessionId) return;
    }
    const trimmed = (text || '').trim();
    const atts = pendingAttachments.slice();
    if (!trimmed && atts.length === 0) return;

    if (trimmed) appendMessage('user', trimmed);
    if (atts.length) {
      atts.forEach(a => {
        const note = document.createElement('div');
        note.className = 'chat-msg user';
        note.style.opacity = '0.85';
        note.innerHTML = `<span class="material-symbols-outlined" style="font-size:1rem;vertical-align:-2px;">attach_file</span> ${escapeHtml(a.name || a.url)}`;
        els.thread.appendChild(note);
      });
    }
    pendingAttachments = [];
    renderAttachments();
    els.input.value = '';
    autoSizeInput();
    setTyping(true);

    try {
      const res = await apiFetch('/api/studio-chat/message', {
        method: 'POST',
        body: JSON.stringify({
          session_id: sessionId,
          message: trimmed,
          attachments: atts.map(a => ({ url: a.url, kind: a.kind })),
        }),
      });
      handleEnvelope(res.envelope, { slots: res.slots, jobId: res.job_id });
    } catch (e) {
      appendMessage('error', `Chat error: ${e.message}`);
    } finally {
      setTyping(false);
    }
  }

  function handleEnvelope(envelope, ctx = {}) {
    if (!envelope) return;
    if (envelope.detected_language) {
      document.documentElement.lang = envelope.detected_language;
      // Right-align RTL languages
      const rtl = ['he', 'ar', 'fa', 'ur'].includes(envelope.detected_language.split('-')[0]);
      els.thread.style.direction = rtl ? 'rtl' : 'ltr';
    }
    if (envelope.reply) appendMessage('assistant', envelope.reply);
    const ua = envelope.ui_action || {};
    if (ua.type === 'show_summary' && ua.summary) {
      renderSummaryCard(ua.summary);
    } else if (ua.type === 'show_panel' && ua.panel && ua.panel !== 'none') {
      mountSidePanel(ua.panel, ctx.slots || null);
    } else if (ua.type === 'request_upload' && ua.panel) {
      mountSidePanel(ua.panel, ctx.slots || null, { autoOpen: true });
    }
    if (ctx.jobId && !jobId) {
      jobId = ctx.jobId;
      attachJobStream(jobId);
    }
  }

  // ────────────────────────────────────────────────────────────────
  // Side panel: empty + mountSidePanel
  // ────────────────────────────────────────────────────────────────
  function showEmptySide() {
    els.sideEmpty.hidden = false;
    els.sideContent.innerHTML = '';
    currentPanel = null;
  }

  function setSideContent(html) {
    els.sideEmpty.hidden = true;
    els.sideContent.innerHTML = html;
  }

  function mountSidePanel(name, slots, opts = {}) {
    if (!name || name === 'none') return;
    currentPanel = name;
    const slotData = slots || {};
    const handlers = {
      uploads_product: () => panelUpload('product', 'product_image_urls', slotData, 'Product photos', 'Drop 2–3 product photos here so we can use them in the scenes.'),
      uploads_character: () => panelUpload('character', 'character_urls', slotData, 'Your photo', 'Drop a photo of yourself (or your host) — we\'ll use it for the on-camera scenes.'),
      uploads_logo: () => panelUpload('logo', 'logo_url', slotData, 'Brand logo', 'Drop your logo (PNG with transparency works best).', { single: true }),
      uploads_assets: () => panelUpload('assets', 'asset_urls', slotData, 'Existing clips', 'Drop short video clips or photos to insert as-is.'),
      character_preview: () => panelCharacterPreview(slotData),
      scene_prompts: () => panelGenericFromJob('scene_prompts', 'Scene prompts', 'edit'),
      scene_images: () => panelGenericFromJob('scene_images', 'Scene images', 'image'),
      vo_player: () => panelGenericFromJob('vo_player', 'Voiceover', 'audio'),
      music_player: () => panelGenericFromJob('music_player', 'Background music', 'audio'),
      final_video: () => panelGenericFromJob('final_video', 'Final video', 'video'),
      storyboard_review: () => panelStoryboardReview(),
    };
    const handler = handlers[name];
    if (!handler) {
      console.warn('Unknown side panel:', name);
      return;
    }
    handler();
    if (opts.autoOpen) {
      const dz = els.sideContent.querySelector('.side-dropzone');
      const inp = els.sideContent.querySelector('input[type="file"]');
      if (inp) inp.click();
      else if (dz) dz.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  }

  function panelUpload(kind, slotKey, slots, title, hint, opts = {}) {
    const single = !!opts.single;
    const existing = single
      ? (slots[slotKey] ? [slots[slotKey]] : [])
      : (Array.isArray(slots[slotKey]) ? slots[slotKey] : []);
    const thumbs = existing.map((url, i) => `
      <div class="side-thumb" data-url="${escapeHtml(url)}" style="background-image:url('${escapeHtml(url)}')">
        <button class="remove" data-idx="${i}" type="button" title="Remove">
          <span class="material-symbols-outlined">close</span>
        </button>
      </div>
    `).join('');

    setSideContent(`
      <div class="side-card">
        <div class="side-card-title"><span class="material-symbols-outlined">cloud_upload</span>${escapeHtml(title)}</div>
        <p class="side-card-sub">${escapeHtml(hint)}</p>
        <label class="side-dropzone" id="sideDropzone">
          <span class="material-symbols-outlined">upload_file</span>
          Click to choose ${single ? 'a file' : 'files'}, or drag &amp; drop here
          <input type="file" accept="image/*,video/*" ${single ? '' : 'multiple'} hidden />
        </label>
        <div class="side-thumbs" id="sideThumbs">${thumbs}</div>
        <div style="margin-top: 1rem; display:flex; gap:0.5rem;">
          <button type="button" class="chat-btn-primary" id="sideUploadDone">
            Done — back to chat
          </button>
        </div>
      </div>
    `);

    const dz = $('#sideDropzone');
    const fileInput = dz.querySelector('input[type="file"]');
    const thumbsDiv = $('#sideThumbs');
    const doneBtn = $('#sideUploadDone');

    fileInput.addEventListener('change', async (ev) => {
      const files = Array.from(ev.target.files || []);
      ev.target.value = '';
      for (const f of files) {
        await handleSideUpload(f, kind, slotKey, single, thumbsDiv);
      }
    });
    dz.addEventListener('dragover', (e) => { e.preventDefault(); dz.classList.add('dragover'); });
    dz.addEventListener('dragleave', () => dz.classList.remove('dragover'));
    dz.addEventListener('drop', async (e) => {
      e.preventDefault();
      dz.classList.remove('dragover');
      const files = Array.from(e.dataTransfer.files || []);
      for (const f of files) await handleSideUpload(f, kind, slotKey, single, thumbsDiv);
    });
    doneBtn.addEventListener('click', () => {
      const urls = $$('#sideThumbs .side-thumb').map(t => t.dataset.url).filter(Boolean);
      const summary = single
        ? (urls.length ? `Uploaded ${kind}.` : `No ${kind} uploaded.`)
        : `Uploaded ${urls.length} ${kind} file${urls.length === 1 ? '' : 's'}.`;
      sendMessage(summary);
    });
  }

  async function handleSideUpload(file, kind, slotKey, single, thumbsDiv) {
    const placeholder = document.createElement('div');
    placeholder.className = 'side-thumb';
    placeholder.style.background = 'var(--vb-surface-container)';
    placeholder.innerHTML = `<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--vb-on-surface-variant);font-size:0.75rem;">Uploading…</div>`;
    if (single) thumbsDiv.innerHTML = '';
    thumbsDiv.appendChild(placeholder);
    try {
      // F: tell the server which slot this upload is for so it can warn on mismatch
      const slotPanel = ({product:'uploads_product', character:'uploads_character', logo:'uploads_logo', assets:'uploads_assets'})[kind] || '';
      const res = await apiUpload(file, { slot: slotPanel });
      placeholder.dataset.url = res.url;
      placeholder.style.backgroundImage = `url('${res.url}')`;
      placeholder.style.backgroundSize = 'cover';
      placeholder.style.backgroundPosition = 'center';
      placeholder.innerHTML = `
        <button class="remove" type="button" title="Remove">
          <span class="material-symbols-outlined">close</span>
        </button>
      `;
      placeholder.querySelector('.remove').addEventListener('click', () => placeholder.remove());
      // Tell the agent immediately so slots stay in sync
      pendingAttachments.push({ url: res.url, kind, name: file.name });
      renderAttachments();
    } catch (e) {
      placeholder.remove();
      appendMessage('error', `Upload failed: ${e.message}`);
    }
  }

  function panelCharacterPreview(slots) {
    const url = (slots.character_urls && slots.character_urls[0]) ||
      (lastJobSnapshot && lastJobSnapshot.intermediates && lastJobSnapshot.intermediates.character_url) ||
      '';
    if (!url) {
      setSideContent(`
        <div class="side-card">
          <div class="side-card-title"><span class="material-symbols-outlined">face</span>Character</div>
          <p class="side-card-sub">No character image yet.</p>
        </div>
      `);
      return;
    }
    setSideContent(`
      <div class="side-card">
        <div class="side-card-title"><span class="material-symbols-outlined">face</span>Character preview</div>
        <div class="side-thumbs"><div class="side-thumb" style="background-image:url('${escapeHtml(url)}'); aspect-ratio: 9/16;"></div></div>
      </div>
    `);
  }

  function panelGenericFromJob(panelKind, title, contentKind) {
    const inter = (lastJobSnapshot && lastJobSnapshot.intermediates) || {};
    let body = `<p class="side-card-sub">Not ready yet — I'll show this when it's generated.</p>`;

    if (panelKind === 'scene_prompts' && Array.isArray(inter.scene_prompts)) {
      body = `<ol style="padding-left:1.2rem;display:flex;flex-direction:column;gap:0.5rem;">` +
        inter.scene_prompts.map(p => `<li class="side-text-block" style="padding:.5rem .65rem;">${escapeHtml(p.first_prompt || p.prompt || JSON.stringify(p))}</li>`).join('') +
        `</ol>`;
    } else if (panelKind === 'scene_images' && Array.isArray(inter.scene_images)) {
      const imgs = inter.scene_images.filter(Boolean);
      body = `<div class="side-thumbs">` + imgs.map(u => `<div class="side-thumb" style="background-image:url('${escapeHtml(u)}')"></div>`).join('') + `</div>`;
    } else if (panelKind === 'vo_player' && (inter.vo_url || inter.vo_audio_url)) {
      const url = inter.vo_url || inter.vo_audio_url;
      body = `<div class="side-media-player"><audio controls src="${escapeHtml(url)}"></audio></div>`;
    } else if (panelKind === 'music_player' && (inter.music_url || inter.background_music_url)) {
      const url = inter.music_url || inter.background_music_url;
      body = `<div class="side-media-player"><audio controls src="${escapeHtml(url)}"></audio></div>`;
    } else if (panelKind === 'final_video' && (inter.final_video_url || inter.subtitled_url)) {
      const url = inter.final_video_url || inter.subtitled_url;
      body = `<div class="side-media-player"><video controls playsinline src="${escapeHtml(url)}"></video></div>
              <div style="margin-top:0.75rem;"><a class="chat-link-btn" href="${escapeHtml(url)}" target="_blank" download><span class="material-symbols-outlined">download</span>Download</a></div>`;
    }

    const iconMap = { edit: 'description', image: 'image', audio: 'graphic_eq', video: 'play_circle' };
    setSideContent(`
      <div class="side-card">
        <div class="side-card-title"><span class="material-symbols-outlined">${iconMap[contentKind] || 'movie'}</span>${escapeHtml(title)}</div>
        ${body}
      </div>
    `);
  }

  function renderProgressCard() {
    const snap = lastJobSnapshot || {};
    const pct = Math.max(0, Math.min(100, Math.round(snap.progress || 0)));
    const step = snap.current_step || 'starting…';
    const status = snap.status || 'pending';
    let html = `
      <div class="side-card">
        <div class="side-card-title"><span class="material-symbols-outlined">construction</span>Building your video</div>
        <div class="side-progress-bar"><div class="side-progress-bar-fill" style="width:${pct}%"></div></div>
        <div class="side-progress-label">
          <span>${escapeHtml(step)}</span>
          <span>${pct}% · ${escapeHtml(status)}</span>
        </div>
      </div>
    `;
    const inter = snap.intermediates || {};
    if (inter.character_url) {
      html += `
        <div class="side-card">
          <div class="side-card-title"><span class="material-symbols-outlined">face</span>Character</div>
          <div class="side-thumbs"><div class="side-thumb" style="background-image:url('${escapeHtml(inter.character_url)}'); aspect-ratio:9/16;"></div></div>
        </div>
      `;
    }
    const imgs = (inter.scene_images || []).filter(Boolean);
    if (imgs.length) {
      html += `
        <div class="side-card">
          <div class="side-card-title"><span class="material-symbols-outlined">image</span>Scene images</div>
          <div class="side-thumbs">${imgs.map(u => `<div class="side-thumb" style="background-image:url('${escapeHtml(u)}')"></div>`).join('')}</div>
        </div>
      `;
    }
    const finalUrl = inter.final_video_url || inter.subtitled_url || inter.rendi_scene_voice_url;
    if (finalUrl) {
      html += `
        <div class="side-card">
          <div class="side-card-title"><span class="material-symbols-outlined">play_circle</span>Final video</div>
          <div class="side-media-player"><video controls playsinline src="${escapeHtml(finalUrl)}"></video></div>
          <div style="margin-top:0.75rem;"><a class="chat-link-btn" href="${escapeHtml(finalUrl)}" target="_blank" download><span class="material-symbols-outlined">download</span>Download</a></div>
        </div>
      `;
    }
    setSideContent(html);
  }

  // ────────────────────────────────────────────────────────────────
  // Generation commit
  // ────────────────────────────────────────────────────────────────
  async function commitGeneration(simulation, cardEl) {
    if (!sessionId) return;
    const buttons = cardEl ? cardEl.querySelectorAll('button') : [];
    buttons.forEach(b => b.disabled = true);
    try {
      const res = await apiFetch('/api/studio-chat/commit', {
        method: 'POST',
        body: JSON.stringify({
          session_id: sessionId,
          simulation,
          simulation_duration: simulation ? '30s' : undefined,
        }),
      });
      jobId = res.job_id;
      appendSystemEvent(simulation ? 'Simulation started — this is a free preview.' : 'Generation started — sit back, I\'ll show you each piece as it\'s ready.', 'rocket_launch');
      renderProgressCard();
      attachJobStream(jobId);
    } catch (e) {
      // The server returns a structured detail when required slots are missing:
      //   { error: 'missing_required_slots', missing_fields: [...], agent_reply: '...', ui_action: {...} }
      const parsed = parseStructuredError(e && e.message);
      if (parsed && parsed.error === 'missing_required_slots') {
        if (parsed.agent_reply) appendMessage('assistant', parsed.agent_reply);
        if (parsed.ui_action && parsed.ui_action.type && parsed.ui_action.type !== 'none') {
          handleEnvelope({
            reply: '',
            detected_language: document.documentElement.lang || 'en',
            ui_action: parsed.ui_action,
          });
        }
      } else {
        appendMessage('error', `Couldn't start generation: ${e.message}`);
      }
      buttons.forEach(b => b.disabled = false);
    }
  }

  function parseStructuredError(message) {
    if (!message) return null;
    // apiFetch wraps detail as: "HTTP 422: {json or string}"
    const m = String(message).match(/^HTTP \d+:\s*(\{[\s\S]*\})$/);
    if (!m) return null;
    try { return JSON.parse(m[1]); } catch (_) { return null; }
  }

  // ────────────────────────────────────────────────────────────────
  // Storyboard review panel (custom pipeline)
  // ────────────────────────────────────────────────────────────────

  const CLIP_TYPE_OPTIONS = [
    { value: 'generate', label: 'AI-generated' },
    { value: 'asset_video', label: 'User video clip' },
    { value: 'asset_image_animate', label: 'Animate user photo' },
    { value: 'composite', label: 'Composite (logo / overlay)' },
    { value: 'ken_burns', label: 'Ken Burns (still + pan)' },
    { value: 'seedance_multishot', label: 'Seedance multi-shot 🎬' },
    { value: 'motion_graphic', label: 'Motion graphics ✨' },
  ];
  const TOOL_HINT_OPTIONS = [
    { value: 'auto', label: 'Auto' },
    { value: 'veo', label: 'Veo (lipsync, dialog)' },
    { value: 'seedance', label: 'Seedance (multi-shot consistency)' },
    { value: 'kling', label: 'Kling (heavy motion)' },
    { value: 'runway', label: 'Runway (cinematic)' },
    { value: 'kenburns', label: 'Ken Burns (free)' },
    { value: 'trim', label: 'Trim only' },
  ];
  const SHOT_TYPE_OPTIONS = [
    { value: '', label: '— shot type —' },
    { value: 'extreme_close_up', label: 'Extreme close-up' },
    { value: 'close_up', label: 'Close-up' },
    { value: 'medium', label: 'Medium' },
    { value: 'medium_wide', label: 'Medium wide' },
    { value: 'wide', label: 'Wide' },
    { value: 'establishing', label: 'Establishing' },
    { value: 'over_shoulder', label: 'Over-the-shoulder' },
    { value: 'insert', label: 'Insert' },
    { value: 'pov', label: 'POV' },
  ];
  const PRIMARY_MOVE_OPTIONS = [
    { value: '', label: '— camera move —' },
    { value: 'static', label: 'Static' },
    { value: 'slow_dolly_in', label: 'Slow dolly in' },
    { value: 'slow_dolly_out', label: 'Slow dolly out' },
    { value: 'fast_dolly_in', label: 'Fast dolly in' },
    { value: 'orbit', label: 'Orbit' },
    { value: 'tracking', label: 'Tracking' },
    { value: 'whip_pan', label: 'Whip pan' },
    { value: 'crash_zoom', label: 'Crash zoom' },
    { value: 'ken_burns', label: 'Ken Burns' },
    { value: 'pan_left', label: 'Pan left' },
    { value: 'pan_right', label: 'Pan right' },
    { value: 'tilt_up', label: 'Tilt up' },
    { value: 'tilt_down', label: 'Tilt down' },
    { value: 'handheld', label: 'Handheld' },
  ];
  const SPEED_OPTIONS = [
    { value: 'slow', label: 'Slow' },
    { value: 'moderate', label: 'Moderate' },
    { value: 'fast', label: 'Fast' },
  ];

  // E1: per-scene image model picker
  const IMAGE_MODEL_OPTIONS = [
    { value: '', label: '— image model —' },
    { value: 'nano-banana-pro', label: 'Nano Banana Pro (hero)' },
    { value: 'nano-banana-2', label: 'Nano Banana 2 (fast)' },
    { value: 'gemini-3-pro-image-preview', label: 'Gemini 3 Pro Image' },
    { value: 'gemini-3.1-flash-image-preview', label: 'Gemini 3.1 Flash Image' },
  ];
  // E1: per-clip explicit video model picker
  const VIDEO_MODEL_OPTIONS = [
    { value: '', label: '— video model (auto) —' },
    { value: 'veo-3.1-fast', label: 'Veo 3.1 Fast (default)' },
    { value: 'veo-3.1', label: 'Veo 3.1 (hero)' },
    { value: 'veo-3.1-ref-fast', label: 'Veo 3.1 Ref Fast (character lock, 8s)' },
    { value: 'veo-3.1-ref', label: 'Veo 3.1 Ref (character lock, hero, 8s)' },
    { value: 'seedance-2', label: 'Seedance 2 (multi-shot)' },
    { value: 'kling-2.6', label: 'Kling 2.6 (heavy motion)' },
    { value: 'kling-2.5', label: 'Kling 2.5' },
    { value: 'runway-gen4.5', label: 'Runway Gen 4.5 (photoreal)' },
    { value: 'runway-gen4-turbo', label: 'Runway Gen 4 Turbo' },
    { value: 'kenburns', label: 'Ken Burns (free)' },
  ];

  // Current chat mode. "concierge" uses the lighter build-storyboard call;
  // "director" uses Gemini 3 Pro via direct-storyboard. Updated by the
  // mode-switch buttons in the header AND echoed back from /session/{id}.
  let chatMode = 'concierge';

  async function ensureStoryboard(forceRebuild = false) {
    if (currentStoryboard && !forceRebuild) return currentStoryboard;
    if (!sessionId) throw new Error('No active chat session');
    // Director mode → richer Gemini 3 Pro plan; Concierge → slot-fill builder.
    const url = chatMode === 'director'
      ? '/api/studio-chat/direct-storyboard'
      : '/api/studio-chat/build-storyboard';
    const res = await apiFetch(url, {
      method: 'POST',
      body: JSON.stringify({ session_id: sessionId }),
    });

    // P1.3: Director may return needs_assets instead of storyboard. Surface to chat + open upload panel.
    if (res.needs_assets && Array.isArray(res.needs_assets) && res.needs_assets.length) {
      if (res.reply) appendMessage('assistant', res.reply);
      // Append each asset request as its own concise system event
      res.needs_assets.forEach(req => {
        if (req && req.reason) {
          appendSystemEvent(`📎 ${req.reason}`, 'upload');
        }
      });
      // Open the first requested upload panel in the side
      if (res.ui_action && res.ui_action.type === 'request_upload') {
        mountSidePanel(res.ui_action.panel || 'uploads_assets', currentStoryboard || {}, { autoOpen: false });
      }
      // Throw a soft signal so the caller (panelStoryboardReview) shows a status instead of a generic error
      const err = new Error('NEEDS_ASSETS');
      err.needsAssets = res.needs_assets;
      err.reply = res.reply;
      throw err;
    }

    currentStoryboard = res.storyboard || null;

    // P1.5: friendly_errors from the server are pre-translated for humans
    if (res.friendly_errors && res.friendly_errors.length) {
      res.friendly_errors.forEach(msg => appendSystemEvent(`⚠ ${msg}`, 'warning'));
    } else if (res.validation_errors && res.validation_errors.length) {
      console.warn('Storyboard validation errors:', res.validation_errors);
    }
    return currentStoryboard;
  }

  async function setChatMode(mode) {
    if (mode === chatMode) return;
    if (mode !== 'concierge' && mode !== 'director') return;
    const prev = chatMode;
    chatMode = mode;
    // Visual update
    $$('.chat-mode-btn').forEach(b => {
      b.classList.toggle('active', b.dataset.mode === mode);
    });
    // Persist server-side if we have a session
    if (sessionId) {
      try {
        await apiFetch('/api/studio-chat/mode', {
          method: 'POST',
          body: JSON.stringify({ session_id: sessionId, mode }),
        });
      } catch (e) {
        // Roll back UI on failure
        chatMode = prev;
        $$('.chat-mode-btn').forEach(b => {
          b.classList.toggle('active', b.dataset.mode === prev);
        });
        appendMessage('error', `Could not switch mode: ${e.message}`);
      }
    }
  }

  async function panelStoryboardReview() {
    setSideContent(`
      <div class="side-card storyboard-review">
        <div class="side-card-title">
          <span class="material-symbols-outlined">view_timeline</span>
          Storyboard review
        </div>
        <div id="storyboardStatus" class="storyboard-status">Building storyboard…</div>
        <div id="storyboardBody" hidden></div>
      </div>
    `);
    const statusEl = $('#storyboardStatus');
    const bodyEl = $('#storyboardBody');
    try {
      await ensureStoryboard();
      if (!currentStoryboard) throw new Error('Builder returned no storyboard');
      statusEl.hidden = true;
      bodyEl.hidden = false;
      renderStoryboardEditor(bodyEl);
    } catch (e) {
      // P1.3: NEEDS_ASSETS is a soft signal — the upload panel was already opened by ensureStoryboard().
      // Show a friendly status here (not a red error) so the user knows to upload then retry.
      if (e && e.message === 'NEEDS_ASSETS') {
        statusEl.innerHTML = `<div style="padding:0.5rem 0;"><span class="material-symbols-outlined" style="vertical-align:middle;">cloud_upload</span> Waiting for uploads…<br/><span style="color:var(--vb-on-surface-variant); font-size:0.85em;">Upload the files I asked for, then click below to continue.</span></div>
          <div style="margin-top:0.5rem;"><button type="button" class="chat-btn-primary" id="storyboardRetry">I've uploaded — try again</button></div>`;
      } else {
        statusEl.innerHTML = `<span style="color: var(--vb-error);">Couldn't build storyboard: ${escapeHtml(e.message)}</span>
          <div style="margin-top:0.5rem;"><button type="button" class="chat-btn-secondary" id="storyboardRetry">Try again</button></div>`;
      }
      const retry = $('#storyboardRetry');
      if (retry) retry.addEventListener('click', () => { currentStoryboard = null; panelStoryboardReview(); });
    }
  }

  function renderStoryboardEditor(rootEl) {
    const sb = currentStoryboard;
    const meta = sb.meta || {};
    const vo = sb.voiceover || {};
    const scenes = Array.isArray(sb.scenes) ? sb.scenes : [];
    const totalDur = scenes.reduce((acc, s) => acc + (Number(s.duration) || 0), 0);

    rootEl.innerHTML = `
      <div class="storyboard-meta">
        <div class="storyboard-meta-row">
          <input class="storyboard-title" id="sbMetaTitle" value="${escapeHtml(meta.title || '')}" placeholder="Video title" />
          <span class="storyboard-dur-chip"><span class="material-symbols-outlined">schedule</span><span id="sbDurTotal">${totalDur.toFixed(1)}</span>s</span>
        </div>
        <div class="storyboard-meta-row storyboard-meta-row-2">
          <label class="storyboard-meta-label">Style
            <input id="sbMetaStyle" value="${escapeHtml(meta.style || 'Auto')}" />
          </label>
          <label class="storyboard-meta-label">Fidelity to assets
            <input type="range" id="sbMetaFidelity" min="0" max="100" value="${Math.round((meta.fidelity_to_assets ?? 0.5) * 100)}" />
            <span id="sbMetaFidelityVal">${Math.round((meta.fidelity_to_assets ?? 0.5) * 100)}%</span>
          </label>
        </div>
      </div>

      <div class="storyboard-vo">
        <label class="storyboard-meta-label">Voiceover script (use ||| to split scenes)</label>
        <textarea id="sbVoScript" rows="3">${escapeHtml(vo.script || '')}</textarea>
      </div>

      <!-- E4: image-first preview section. Hidden until user clicks "Render image previews". -->
      <div class="storyboard-image-previews" id="sbImagePreviews" hidden>
        <div class="storyboard-image-previews-head">
          <span class="material-symbols-outlined">image</span>
          <strong>Image storyboard</strong>
          <span class="sb-preview-hint">— review the look before animating</span>
        </div>
        <div class="sb-image-grid" id="sbImageGrid"></div>
      </div>

      <div class="storyboard-scenes" id="sbScenes">${scenes.map(renderSceneCard).join('')}</div>

      <div class="storyboard-actions">
        <button type="button" class="chat-btn-secondary" id="sbRebuild">Rebuild from chat</button>
        <button type="button" class="chat-btn-secondary" id="sbRenderPreviews">🖼️ Render image previews</button>
        <button type="button" class="chat-btn-secondary" id="sbSave">Save edits</button>
        <button type="button" class="chat-btn-primary" id="sbGenerate">Approve &amp; generate</button>
      </div>
    `;

    // If the storyboard already has previews (e.g. user came back to the panel),
    // show the image grid immediately.
    if (scenes.some(s => s.preview_image_url)) {
      $('#sbImagePreviews').hidden = false;
      renderImageGrid();
    }

    // Bind change handlers
    $('#sbMetaTitle').addEventListener('input', e => { sb.meta.title = e.target.value; });
    $('#sbMetaStyle').addEventListener('input', e => { sb.meta.style = e.target.value; });
    const fidEl = $('#sbMetaFidelity');
    const fidValEl = $('#sbMetaFidelityVal');
    fidEl.addEventListener('input', e => {
      const v = Number(e.target.value) / 100;
      sb.meta.fidelity_to_assets = v;
      fidValEl.textContent = `${e.target.value}%`;
    });
    $('#sbVoScript').addEventListener('input', e => {
      sb.voiceover = sb.voiceover || {};
      sb.voiceover.script = e.target.value;
    });
    bindSceneCardHandlers();

    $('#sbRebuild').addEventListener('click', async () => {
      currentStoryboard = null;
      await panelStoryboardReview();
    });
    $('#sbSave').addEventListener('click', async () => {
      const btn = $('#sbSave');
      btn.disabled = true;
      btn.textContent = 'Saving…';
      try {
        const res = await apiFetch('/api/studio-chat/storyboard', {
          method: 'PATCH',
          body: JSON.stringify({ session_id: sessionId, storyboard: currentStoryboard }),
        });
        currentStoryboard = res.storyboard;
        btn.textContent = 'Saved ✓';
        setTimeout(() => { btn.textContent = 'Save edits'; btn.disabled = false; }, 1500);
      } catch (e) {
        btn.textContent = 'Save failed';
        btn.disabled = false;
        appendMessage('error', `Save failed: ${e.message}`);
      }
    });
    $('#sbGenerate').addEventListener('click', () => commitCustomGeneration());
    $('#sbRenderPreviews').addEventListener('click', () => renderPreviewsClick());
  }

  // E4: render the image-preview grid based on currentStoryboard.scenes[].preview_image_url
  function renderImageGrid() {
    const sb = currentStoryboard;
    if (!sb) return;
    const gridEl = $('#sbImageGrid');
    if (!gridEl) return;
    const scenes = Array.isArray(sb.scenes) ? sb.scenes : [];
    gridEl.innerHTML = scenes.map((scene, idx) => {
      const url = scene.preview_image_url;
      const model = scene.preview_image_model || '(auto)';
      const role = scene.narrative_role || `Scene ${scene.scene_number ?? idx + 1}`;
      return `
        <div class="sb-img-card" data-scene-idx="${idx}">
          <div class="sb-img-thumb">
            ${url
              ? `<img src="${escapeHtml(url)}" alt="Scene ${idx + 1}" loading="lazy" />`
              : `<div class="sb-img-placeholder"><span class="material-symbols-outlined">image</span><span>Not rendered</span></div>`
            }
          </div>
          <div class="sb-img-meta">
            <div class="sb-img-role">${escapeHtml(role)}</div>
            <div class="sb-img-model" title="Preview image model">${escapeHtml(model)}</div>
          </div>
          <div class="sb-img-actions">
            <button type="button" class="sb-icon-btn" data-act="reroll" title="Re-roll this scene's preview">
              <span class="material-symbols-outlined">refresh</span>
            </button>
          </div>
        </div>
      `;
    }).join('');

    // Per-card reroll handler (delegated)
    gridEl.querySelectorAll('.sb-icon-btn[data-act="reroll"]').forEach(btn => {
      btn.addEventListener('click', async (ev) => {
        const card = ev.currentTarget.closest('.sb-img-card');
        if (!card) return;
        const idx = Number(card.dataset.sceneIdx);
        const thumb = card.querySelector('.sb-img-thumb');
        if (thumb) thumb.innerHTML = `<div class="sb-img-placeholder"><span class="material-symbols-outlined">hourglass_top</span><span>Re-rolling…</span></div>`;
        try {
          const res = await apiFetch('/api/studio-chat/reroll-scene-preview', {
            method: 'POST',
            body: JSON.stringify({
              session_id: sessionId,
              scene_idx: idx,
              preview_image_model: currentStoryboard.scenes[idx].preview_image_model || null,
            }),
          });
          if (res.preview_url) {
            currentStoryboard.scenes[idx].preview_image_url = res.preview_url;
            renderImageGrid();
          } else {
            if (thumb) thumb.innerHTML = `<div class="sb-img-placeholder" style="color:var(--vb-error);"><span class="material-symbols-outlined">error</span><span>Re-roll failed</span></div>`;
          }
        } catch (e) {
          if (thumb) thumb.innerHTML = `<div class="sb-img-placeholder" style="color:var(--vb-error);"><span class="material-symbols-outlined">error</span><span>${escapeHtml(e.message)}</span></div>`;
        }
      });
    });
  }

  async function renderPreviewsClick() {
    const btn = $('#sbRenderPreviews');
    if (!btn) return;
    const previewsEl = $('#sbImagePreviews');
    const gridEl = $('#sbImageGrid');
    btn.disabled = true;
    btn.textContent = '🖼️ Rendering…';
    previewsEl.hidden = false;
    const sceneCount = (currentStoryboard.scenes || []).length;
    gridEl.innerHTML = Array(sceneCount).fill(0).map(() =>
      `<div class="sb-img-card"><div class="sb-img-thumb"><div class="sb-img-placeholder"><span class="material-symbols-outlined">hourglass_top</span><span>Rendering…</span></div></div></div>`
    ).join('');
    try {
      const res = await apiFetch('/api/studio-chat/render-storyboard-previews', {
        method: 'POST',
        body: JSON.stringify({ session_id: sessionId }),
      });
      // Server returns the updated storyboard with preview_image_url filled in
      if (res.storyboard) currentStoryboard = res.storyboard;
      renderImageGrid();
      btn.textContent = `🖼️ ${res.rendered}/${res.rendered + res.failed} rendered (${res.elapsed_seconds}s)`;
      setTimeout(() => { btn.textContent = '🖼️ Re-render previews'; btn.disabled = false; }, 3000);
    } catch (e) {
      btn.textContent = '🖼️ Render image previews';
      btn.disabled = false;
      appendMessage('error', `Preview render failed: ${e.message}`);
    }
  }

  function renderSceneCard(scene, idx) {
    const clips = Array.isArray(scene.clips) ? scene.clips : [];
    const cam = scene.camera || {};
    return `
      <div class="sb-scene" data-scene-idx="${idx}">
        <div class="sb-scene-head">
          <div class="sb-scene-num">Scene ${scene.scene_number ?? idx + 1}</div>
          <input class="sb-scene-role" data-field="narrative_role" value="${escapeHtml(scene.narrative_role || '')}" placeholder="role (hook / solution / cta…)" />
          <span class="sb-scene-dur">${Number(scene.duration || 0).toFixed(1)}s</span>
          <div class="sb-scene-move">
            <button type="button" class="sb-icon-btn" data-act="up" title="Move up"><span class="material-symbols-outlined">keyboard_arrow_up</span></button>
            <button type="button" class="sb-icon-btn" data-act="down" title="Move down"><span class="material-symbols-outlined">keyboard_arrow_down</span></button>
            <button type="button" class="sb-icon-btn sb-icon-btn-danger" data-act="delete" title="Delete scene"><span class="material-symbols-outlined">delete</span></button>
          </div>
        </div>
        <div class="sb-camera-row" title="Director's cinematic intent — one primary move per clip">
          <label class="sb-camera-field"><span>Shot</span>
            <select data-camera-field="shot_type">
              ${SHOT_TYPE_OPTIONS.map(o => `<option value="${o.value}"${o.value === (cam.shot_type || '') ? ' selected' : ''}>${o.label}</option>`).join('')}
            </select>
          </label>
          <label class="sb-camera-field"><span>Camera</span>
            <select data-camera-field="primary_move">
              ${PRIMARY_MOVE_OPTIONS.map(o => `<option value="${o.value}"${o.value === (cam.primary_move || '') ? ' selected' : ''}>${o.label}</option>`).join('')}
            </select>
          </label>
          <label class="sb-camera-field"><span>Speed</span>
            <select data-camera-field="speed">
              ${SPEED_OPTIONS.map(o => `<option value="${o.value}"${o.value === (cam.speed || 'moderate') ? ' selected' : ''}>${o.label}</option>`).join('')}
            </select>
          </label>
        </div>
        <div class="sb-scene-model-row" title="Image model that renders this scene's preview/first frame">
          <label class="sb-camera-field"><span>Preview image model</span>
            <select data-scene-field="preview_image_model">
              ${IMAGE_MODEL_OPTIONS.map(o => `<option value="${o.value}"${o.value === (scene.preview_image_model || '') ? ' selected' : ''}>${o.label}</option>`).join('')}
            </select>
          </label>
        </div>
        <div class="sb-scene-reroll-row" title="Generate this scene's video clip from its preview image (costs money — runs the real I2V model)">
          <button type="button" class="sb-scene-reroll-btn" data-scene-action="reroll-video">
            <span class="material-symbols-outlined">autorenew</span>
            ${scene._reroll_video_url ? 'Re-roll video again' : '♻ Re-roll video'}
          </button>
          <span class="sb-scene-reroll-status" data-scene-reroll-status></span>
        </div>
        ${scene._reroll_video_url ? `
          <div class="sb-scene-reroll-preview" data-scene-reroll-preview>
            <video src="${escapeHtml(scene._reroll_video_url)}" controls preload="metadata" playsinline></video>
            <a class="sb-scene-reroll-link" href="${escapeHtml(scene._reroll_video_url)}" target="_blank" rel="noopener">Open in new tab</a>
          </div>
        ` : `<div class="sb-scene-reroll-preview" data-scene-reroll-preview hidden></div>`}
        ${scene.director_note ? `<div class="sb-director-note" title="Director's rationale for this scene"><span class="material-symbols-outlined">lightbulb</span>${escapeHtml(scene.director_note)}</div>` : ''}
        <label class="sb-field">
          <span>Voiceover for this scene</span>
          <textarea data-field="vo_text" rows="2">${escapeHtml(scene.vo_text || '')}</textarea>
        </label>
        <div class="sb-clips">
          ${clips.map((c, ci) => renderClipCard(c, ci)).join('')}
        </div>
      </div>
    `;
  }

  function renderClipCard(clip, ci) {
    const ctype = clip.type || 'generate';
    const hint = clip.tool_hint || 'auto';
    const ing = clip.ingredients || {};
    // Badges for special clip types
    let badge = '';
    if (ctype === 'seedance_multishot') {
      badge = '<span class="sb-clip-badge sb-badge-seedance" title="Multi-shot consistency via Seedance 2.0">🎬 Multi-shot</span>';
    } else if (ctype === 'motion_graphic') {
      badge = '<span class="sb-clip-badge sb-badge-motiongraphic" title="Kinetic typography / animated text">✨ Motion graphics</span>';
    } else if (ctype === 'composite') {
      badge = '<span class="sb-clip-badge sb-badge-composite" title="Logo / slogan overlay composite">🏷️ Composite</span>';
    }
    // Ingredients section — only shown when there's a character_sheet / venue_sheet / style_sheet on the storyboard
    const sb = currentStoryboard || {};
    const hasAnySheet = !!(sb.character_sheet || sb.venue_sheet || sb.style_sheet);
    const ingredientsHtml = hasAnySheet ? `
        <div class="sb-clip-ingredients" title="Lock references for this clip — Veo Ingredients / Seedance refs">
          <span class="sb-clip-ingredients-label">Lock refs:</span>
          ${sb.character_sheet ? `<label class="sb-chip-toggle"><input type="checkbox" data-ingredient="use_character_sheet"${ing.use_character_sheet ? ' checked' : ''}/>Character</label>` : ''}
          ${sb.venue_sheet ? `<label class="sb-chip-toggle"><input type="checkbox" data-ingredient="use_venue_sheet"${ing.use_venue_sheet ? ' checked' : ''}/>Venue</label>` : ''}
          ${sb.style_sheet ? `<label class="sb-chip-toggle"><input type="checkbox" data-ingredient="use_style_sheet"${ing.use_style_sheet ? ' checked' : ''}/>Style</label>` : ''}
        </div>` : '';
    return `
      <div class="sb-clip" data-clip-idx="${ci}">
        ${badge ? `<div class="sb-clip-badges">${badge}</div>` : ''}
        <div class="sb-clip-row">
          <label class="sb-clip-field"><span>Type</span>
            <select data-field="type">
              ${CLIP_TYPE_OPTIONS.map(o => `<option value="${o.value}"${o.value === ctype ? ' selected' : ''}>${o.label}</option>`).join('')}
            </select>
          </label>
          <label class="sb-clip-field"><span>Video model</span>
            <select data-field="video_model_override">
              ${VIDEO_MODEL_OPTIONS.map(o => `<option value="${o.value}"${o.value === (clip.video_model_override || '') ? ' selected' : ''}>${o.label}</option>`).join('')}
            </select>
          </label>
          <label class="sb-clip-field"><span>Tool</span>
            <select data-field="tool_hint">
              ${TOOL_HINT_OPTIONS.map(o => `<option value="${o.value}"${o.value === hint ? ' selected' : ''}>${o.label}</option>`).join('')}
            </select>
          </label>
          <label class="sb-clip-field sb-clip-dur"><span>Dur</span>
            <input type="number" min="0.5" max="20" step="0.1" data-field="duration" value="${Number(clip.duration || 0).toFixed(1)}" />
          </label>
        </div>
        ${ingredientsHtml}
        ${clip.director_note ? `<div class="sb-director-note sb-director-note-clip" title="Director's rationale for this clip"><span class="material-symbols-outlined">lightbulb</span>${escapeHtml(clip.director_note)}</div>` : ''}
        ${ctype === 'generate' || ctype === 'composite' || ctype === 'motion_graphic' || ctype === 'seedance_multishot' ? `
          <label class="sb-field">
            <span>${ctype === 'motion_graphic' ? 'Text + visual content' : "Visual prompt (what's in the frame)"}</span>
            <textarea data-field="first_prompt" rows="2">${escapeHtml(clip.first_prompt || '')}</textarea>
          </label>` : ''}
        ${ctype !== 'asset_video' ? `
          <label class="sb-field">
            <span>${ctype === 'seedance_multishot' ? 'Shot sequence (use "then" / "cut to" between shots)' : 'Motion prompt (how camera and subject move)'}</span>
            <textarea data-field="motion_prompt" rows="2">${escapeHtml(clip.motion_prompt || '')}</textarea>
          </label>` : ''}
      </div>
    `;
  }

  function bindSceneCardHandlers() {
    const sb = currentStoryboard;
    const scenesEl = $('#sbScenes');
    if (!sb || !scenesEl) return;

    // Camera dropdown (per-scene, change event) — delegated
    scenesEl.addEventListener('change', (ev) => {
      const t = ev.target;
      if (!(t instanceof HTMLSelectElement)) return;
      const camField = t.dataset.cameraField;
      if (!camField) return;
      const sceneCard = t.closest('.sb-scene');
      if (!sceneCard) return;
      const sIdx = Number(sceneCard.dataset.sceneIdx);
      const scene = sb.scenes[sIdx];
      if (!scene) return;
      scene.camera = scene.camera || {};
      if (t.value === '') {
        delete scene.camera[camField];
        if (Object.keys(scene.camera).length === 0) delete scene.camera;
      } else {
        scene.camera[camField] = t.value;
      }
    });

    // E1: per-scene image model picker (preview_image_model) — delegated
    scenesEl.addEventListener('change', (ev) => {
      const t = ev.target;
      if (!(t instanceof HTMLSelectElement)) return;
      const sf = t.dataset.sceneField;
      if (!sf) return;
      const sceneCard = t.closest('.sb-scene');
      if (!sceneCard) return;
      // Don't touch clips — only scene-level fields
      if (t.closest('.sb-clip')) return;
      const sIdx = Number(sceneCard.dataset.sceneIdx);
      const scene = sb.scenes[sIdx];
      if (!scene) return;
      if (t.value === '') delete scene[sf];
      else scene[sf] = t.value;
    });

    // Ingredients checkbox handler (per-clip)
    scenesEl.addEventListener('change', (ev) => {
      const t = ev.target;
      if (!(t instanceof HTMLInputElement) || t.type !== 'checkbox') return;
      const ingKey = t.dataset.ingredient;
      if (!ingKey) return;
      const sceneCard = t.closest('.sb-scene');
      const clipCard = t.closest('.sb-clip');
      if (!sceneCard || !clipCard) return;
      const sIdx = Number(sceneCard.dataset.sceneIdx);
      const cIdx = Number(clipCard.dataset.clipIdx);
      const clip = (sb.scenes[sIdx]?.clips || [])[cIdx];
      if (!clip) return;
      clip.ingredients = clip.ingredients || {};
      clip.ingredients[ingKey] = t.checked;
    });

    // Field edits (delegated)
    scenesEl.addEventListener('input', (ev) => {
      const t = ev.target;
      if (!(t instanceof HTMLElement)) return;
      const field = t.dataset.field;
      if (!field) return;
      const sceneCard = t.closest('.sb-scene');
      const clipCard = t.closest('.sb-clip');
      if (!sceneCard) return;
      const sIdx = Number(sceneCard.dataset.sceneIdx);
      const scene = sb.scenes[sIdx];
      if (!scene) return;
      if (clipCard) {
        const cIdx = Number(clipCard.dataset.clipIdx);
        const clip = (scene.clips || [])[cIdx];
        if (!clip) return;
        if (field === 'duration') {
          clip.duration = Number(t.value) || 0;
          // Update scene duration as sum of clips
          scene.duration = (scene.clips || []).reduce((acc, c) => acc + (Number(c.duration) || 0), 0);
          sceneCard.querySelector('.sb-scene-dur').textContent = scene.duration.toFixed(1) + 's';
          updateTotalDuration();
        } else {
          clip[field] = t.value;
        }
      } else {
        scene[field] = t.value;
      }
    });

    // Type change: re-render the clip card so the right fields appear
    scenesEl.addEventListener('change', (ev) => {
      const t = ev.target;
      if (!(t instanceof HTMLSelectElement)) return;
      if (t.dataset.field !== 'type') return;
      const clipCard = t.closest('.sb-clip');
      const sceneCard = t.closest('.sb-scene');
      if (!clipCard || !sceneCard) return;
      const sIdx = Number(sceneCard.dataset.sceneIdx);
      const cIdx = Number(clipCard.dataset.clipIdx);
      const scene = sb.scenes[sIdx];
      const clip = (scene && scene.clips || [])[cIdx];
      if (!clip) return;
      clip.type = t.value;
      clipCard.outerHTML = renderClipCard(clip, cIdx);
    });

    // Per-scene action buttons that aren't .sb-icon-btn (e.g. re-roll video)
    scenesEl.addEventListener('click', async (ev) => {
      const actionBtn = ev.target.closest('[data-scene-action]');
      if (!actionBtn) return;
      const action = actionBtn.dataset.sceneAction;
      const sceneCard = actionBtn.closest('.sb-scene');
      if (!sceneCard) return;
      const sIdx = Number(sceneCard.dataset.sceneIdx);
      const scene = sb.scenes[sIdx];
      if (!scene) return;

      if (action === 'reroll-video') {
        ev.preventDefault();
        if (actionBtn.disabled) return;
        if (!scene.preview_image_url) {
          appendMessage('error', `Scene ${sIdx + 1} has no preview image yet — render the previews first.`);
          return;
        }
        const firstClip = (scene.clips || [])[0];
        if (!firstClip) {
          appendMessage('error', `Scene ${sIdx + 1} has no clips — cannot reroll video.`);
          return;
        }
        if (firstClip.type === 'framework_render') {
          appendMessage('error', `Scene ${sIdx + 1} uses a framework_render clip — re-roll video is not supported for this clip type yet.`);
          return;
        }
        const ok = confirm(`Re-roll the video for Scene ${sIdx + 1}?\n\nThis runs the real I2V model and will cost money (typically $0.10–$0.80 depending on model + duration).`);
        if (!ok) return;

        const statusEl = sceneCard.querySelector('[data-scene-reroll-status]');
        const previewEl = sceneCard.querySelector('[data-scene-reroll-preview]');
        const originalLabel = actionBtn.innerHTML;
        actionBtn.disabled = true;
        actionBtn.innerHTML = `<span class="material-symbols-outlined">hourglass_top</span>Rendering…`;
        if (statusEl) statusEl.textContent = 'Calling I2V model — this can take 30–120s.';

        try {
          const overrides = {};
          if (firstClip.video_model_override) overrides.video_model_override = firstClip.video_model_override;
          if (firstClip.first_prompt) overrides.first_prompt = firstClip.first_prompt;
          if (firstClip.motion_prompt) overrides.motion_prompt = firstClip.motion_prompt;
          const body = {
            session_id: sessionId,
            scene_idx: sIdx,
          };
          if (Object.keys(overrides).length > 0) body.overrides = overrides;

          const res = await apiFetch('/api/studio-chat/reroll-scene-video', {
            method: 'POST',
            body: JSON.stringify(body),
          });
          if (!res || !res.video_url) throw new Error('reroll returned no video_url');

          // Mirror what the server stored on session.storyboard so subsequent
          // renders (and commit-custom) see the rerolled output.
          scene._reroll_video_url = res.video_url;
          firstClip._reroll_video_url = res.video_url;
          firstClip._reroll_video_model = res.model_used;
          firstClip._reroll_video_provider = res.provider_used;
          if (res.director_note) firstClip.director_note = res.director_note;

          if (previewEl) {
            previewEl.hidden = false;
            previewEl.innerHTML = `
              <video src="${escapeHtml(res.video_url)}" controls preload="metadata" playsinline></video>
              <a class="sb-scene-reroll-link" href="${escapeHtml(res.video_url)}" target="_blank" rel="noopener">Open in new tab</a>
            `;
          }
          const costStr = (res.cost_estimate && typeof res.cost_estimate.total_usd === 'number')
            ? ` · ~$${res.cost_estimate.total_usd.toFixed(3)}`
            : '';
          if (statusEl) statusEl.textContent = `Rerolled via ${res.model_used} in ${Number(res.elapsed_seconds || 0).toFixed(1)}s${costStr}`;
          actionBtn.innerHTML = `<span class="material-symbols-outlined">autorenew</span>Re-roll video again`;
          actionBtn.disabled = false;
        } catch (e) {
          if (statusEl) statusEl.textContent = '';
          actionBtn.innerHTML = originalLabel;
          actionBtn.disabled = false;
          appendMessage('error', `Re-roll failed for Scene ${sIdx + 1}: ${e.message}`);
        }
        return;
      }
    });

    // Move / delete scene
    scenesEl.addEventListener('click', (ev) => {
      const btn = ev.target.closest('.sb-icon-btn');
      if (!btn) return;
      const sceneCard = btn.closest('.sb-scene');
      if (!sceneCard) return;
      const sIdx = Number(sceneCard.dataset.sceneIdx);
      const act = btn.dataset.act;
      const scenes = sb.scenes;
      if (act === 'up' && sIdx > 0) {
        [scenes[sIdx - 1], scenes[sIdx]] = [scenes[sIdx], scenes[sIdx - 1]];
      } else if (act === 'down' && sIdx < scenes.length - 1) {
        [scenes[sIdx], scenes[sIdx + 1]] = [scenes[sIdx + 1], scenes[sIdx]];
      } else if (act === 'delete') {
        if (scenes.length <= 1) return;
        scenes.splice(sIdx, 1);
      } else {
        return;
      }
      // Renumber scenes 1..N for human display
      scenes.forEach((s, i) => { s.scene_number = i + 1; });
      // Re-render scene list only
      const bodyEl = $('#storyboardBody');
      const sbScenes = $('#sbScenes');
      sbScenes.innerHTML = scenes.map(renderSceneCard).join('');
      bindSceneCardHandlers();
      updateTotalDuration();
    });
  }

  function updateTotalDuration() {
    const sb = currentStoryboard;
    if (!sb) return;
    const total = (sb.scenes || []).reduce((acc, s) => acc + (Number(s.duration) || 0), 0);
    const el = $('#sbDurTotal');
    if (el) el.textContent = total.toFixed(1);
    if (sb.meta) sb.meta.target_duration_seconds = Math.round(total * 10) / 10;
  }

  async function commitCustomGeneration() {
    if (!sessionId || !currentStoryboard) return;
    const genBtn = $('#sbGenerate');
    if (genBtn) { genBtn.disabled = true; genBtn.textContent = 'Saving…'; }
    try {
      // Persist current edits first so the server has the latest version on session.
      await apiFetch('/api/studio-chat/storyboard', {
        method: 'PATCH',
        body: JSON.stringify({ session_id: sessionId, storyboard: currentStoryboard }),
      });
      if (genBtn) genBtn.textContent = 'Starting…';
      const res = await apiFetch('/api/studio-chat/commit-custom', {
        method: 'POST',
        body: JSON.stringify({ session_id: sessionId, simulation: false }),
      });
      jobId = res.job_id;
      appendSystemEvent('Custom storyboard accepted — generation started. I\'ll surface each piece as it lands.', 'rocket_launch');
      renderProgressCard();
      attachJobStream(jobId);
    } catch (e) {
      const parsed = parseStructuredError(e && e.message);
      if (parsed && parsed.error === 'invalid_storyboard') {
        appendMessage('error', `Storyboard has issues: ${(parsed.validation_errors || []).join('; ')}`);
      } else {
        appendMessage('error', `Couldn't start generation: ${e.message}`);
      }
      if (genBtn) { genBtn.disabled = false; genBtn.textContent = 'Approve & generate'; }
    }
  }

  // ────────────────────────────────────────────────────────────────
  // SSE / job polling bridge
  // ────────────────────────────────────────────────────────────────
  function closeStreaming() {
    if (sseConnection) {
      try { sseConnection.close(); } catch (_) {}
      sseConnection = null;
    }
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function attachJobStream(jid) {
    closeStreaming();
    // Poll for status (updates intermediates + progress)
    pollTimer = setInterval(() => pollJob(jid), 2500);
    pollJob(jid);

    // Open SSE for events (step messages)
    const apiKey = getApiKey();
    if (!apiKey) return;
    const url = `/api/jobs/${encodeURIComponent(jid)}/events?token=${encodeURIComponent(apiKey)}`;
    try {
      const es = new EventSource(url);
      sseConnection = es;
      es.addEventListener('message', (ev) => {
        try {
          const data = JSON.parse(ev.data);
          handleSseEvent(data);
        } catch (_) { /* ignore */ }
      });
      es.addEventListener('done', () => {
        closeStreaming();
        // last poll to grab final state
        pollJob(jid);
      });
      es.addEventListener('error', () => {
        // Polling continues regardless
      });
    } catch (e) {
      console.warn('SSE failed, polling only:', e);
    }
  }

  function handleSseEvent(data) {
    if (!data) return;
    const message = data.message || data.step || '';
    const type = data.event_type || 'info';
    if (type === 'complete' || type === 'start') {
      appendSystemEvent(message || 'Step complete', type === 'complete' ? 'check_circle' : 'play_arrow');
    } else if (type === 'error') {
      appendSystemEvent(`⚠ ${message}`, 'error');
    } else if (type === 'abort') {
      appendSystemEvent(`Stopped: ${message}`, 'cancel');
    } else if (data.asset_url && data.asset_type) {
      // a new asset just landed — refresh the panel
      pollJob(jobId);
    }
    if (typeof data.cost_usd === 'number') {
      els.costChip.hidden = false;
      els.costChipText.textContent = `$${data.cost_usd.toFixed(2)}`;
    }
  }

  async function pollJob(jid) {
    if (!jid) return;
    try {
      const data = await apiFetch(`/api/jobs/${encodeURIComponent(jid)}`);
      lastJobSnapshot = data;
      renderProgressCard();
      if (typeof data.cost_usd === 'number') {
        els.costChip.hidden = false;
        els.costChipText.textContent = `$${data.cost_usd.toFixed(2)}`;
      }
      const terminal = ['completed', 'failed', 'aborted'].includes((data.status || '').toLowerCase());
      if (terminal) {
        closeStreaming();
        if (data.status === 'completed') {
          appendSystemEvent('Done — your video is ready on the right.', 'check_circle');
        } else if (data.status === 'failed') {
          appendSystemEvent(`Job failed: ${data.error || 'unknown error'}`, 'error');
        }
      }
    } catch (e) {
      // transient failures are fine; we'll try again on the next tick
      console.debug('pollJob error:', e.message);
    }
  }

  // ────────────────────────────────────────────────────────────────
  // Composer + attachments
  // ────────────────────────────────────────────────────────────────
  function autoSizeInput() {
    const t = els.input;
    t.style.height = 'auto';
    t.style.height = Math.min(160, t.scrollHeight) + 'px';
    els.sendBtn.disabled = !(t.value.trim().length || pendingAttachments.length);
  }

  function renderAttachments() {
    if (!pendingAttachments.length) {
      els.attachments.hidden = true;
      els.attachments.innerHTML = '';
      autoSizeInput();
      return;
    }
    els.attachments.hidden = false;
    els.attachments.innerHTML = pendingAttachments.map((a, i) => `
      <div class="chat-attachment-thumb" style="background-image:url('${escapeHtml(a.url)}')">
        <button class="remove" data-idx="${i}" type="button" title="Remove">
          <span class="material-symbols-outlined">close</span>
        </button>
      </div>
    `).join('');
    els.attachments.querySelectorAll('.remove').forEach(btn => {
      btn.addEventListener('click', () => {
        const idx = parseInt(btn.dataset.idx, 10);
        pendingAttachments.splice(idx, 1);
        renderAttachments();
      });
    });
    autoSizeInput();
  }

  // ────────────────────────────────────────────────────────────────
  // Event wiring
  // ────────────────────────────────────────────────────────────────
  function wireEvents() {
    els.composer.addEventListener('submit', (e) => {
      e.preventDefault();
      const text = els.input.value;
      if (!text.trim() && pendingAttachments.length === 0) return;
      sendMessage(text);
    });
    els.input.addEventListener('input', autoSizeInput);
    els.input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        els.composer.dispatchEvent(new Event('submit', { cancelable: true }));
      }
    });
    els.attachBtn.addEventListener('click', () => els.attachInput.click());
    els.attachInput.addEventListener('change', async (ev) => {
      const files = Array.from(ev.target.files || []);
      ev.target.value = '';
      for (const f of files) {
        try {
          const res = await apiUpload(f);
          pendingAttachments.push({ url: res.url, kind: f.type.startsWith('video/') ? 'video' : 'image', name: f.name });
          renderAttachments();
        } catch (e) {
          appendMessage('error', `Upload failed: ${e.message}`);
        }
      }
    });

    els.resetBtn.addEventListener('click', () => {
      if (confirm('Start a new chat? Current conversation will be cleared.')) {
        startNewSession();
      }
    });

    // Mode switch buttons (Concierge / Director)
    $$('.chat-mode-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const mode = btn.dataset.mode;
        if (mode) setChatMode(mode);
      });
    });

    els.apiKeySaveBtn.addEventListener('click', async () => {
      const key = (els.apiKeyInput.value || '').trim();
      if (!key) return;
      setApiKey(key);
      els.apiKeyInput.value = '';
      await startNewSession();
    });
    els.apiKeyInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') els.apiKeySaveBtn.click();
    });

    if (els.signinForm) {
      els.signinForm.addEventListener('submit', handleSignin);
    } else {
      console.warn('[chat] signinForm missing — sign-in will not work');
    }
    // Belt-and-braces: also handle the button click directly.
    if (els.signinSubmit) {
      els.signinSubmit.addEventListener('click', (ev) => {
        // The form submit will fire too; this just ensures we don't lose the click
        // if anything intercepts the form. preventDefault avoids double-fire.
        if (els.signinForm) return; // form handler will catch it
        handleSignin(ev);
      });
    }
    if (els.signinToggleBtn) {
      els.signinToggleBtn.addEventListener('click', () => {
        setSigninMode(signinMode === 'signup' ? 'signin' : 'signup');
        if (els.signinError) els.signinError.hidden = true;
      });
    }
    // Also allow Enter in the password field to submit
    if (els.signinPassword) {
      els.signinPassword.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter') {
          ev.preventDefault();
          handleSignin(ev);
        }
      });
    }
  }

  // ────────────────────────────────────────────────────────────────
  // Boot
  // ────────────────────────────────────────────────────────────────
  async function boot() {
    wireEvents();
    autoSizeInput();
    await autoConfigureAuth();
    refreshAuthBanner();
    setSigninMode('signin');

    const signedIn = await isSignedInToCloud();
    if (cloudAuthEnabled && !signedIn) {
      // Block until the user signs in (Supabase email + password).
      showSigninOverlay(true);
      return;
    }
    if (getApiKey()) {
      await startNewSession();
    } else {
      appendMessage('assistant', 'Paste your API key in the bar above and I\'ll get started.');
    }
  }

  document.addEventListener('DOMContentLoaded', boot);
})();

/**
 * Supabase Auth + user_sessions / user_videos (RLS). Used by Video Studio gallery and cloud saves.
 */
var StudioAuth = (function () {
  var _client = null;
  var _authEnabled = false;
  /** When true, user must sign in before using the wizard (full-screen gate). */
  var _loginGateRequired = false;
  var _listeners = [];
  /** Last /api/config response (set even when client init fails). */
  var _lastConfig = null;
  /** Remove visibility/pageshow listeners from a previous Supabase client (init may run again). */
  var _tabVisibilityCleanup = null;

  /**
   * Browsers throttle timers in background tabs; GoTrue may miss refresh windows and emit SIGNED_OUT.
   * When the tab is visible again, restart auto-refresh and refresh the session if close to expiry.
   */
  function _wireTabVisibilityRecovery(client) {
    if (typeof window === 'undefined' || typeof document === 'undefined' || !client) return;
    if (_tabVisibilityCleanup) {
      try {
        _tabVisibilityCleanup();
      } catch (e0) {}
      _tabVisibilityCleanup = null;
    }
    var debounceTimer = null;
    function recoverWhenVisible() {
      if (document.visibilityState !== 'visible') return;
      if (debounceTimer) clearTimeout(debounceTimer);
      debounceTimer = setTimeout(function () {
        debounceTimer = null;
        if (!client || !client.auth) return;
        try {
          if (typeof client.auth.startAutoRefresh === 'function') {
            client.auth.startAutoRefresh();
          }
        } catch (e1) {}
        client.auth
          .getSession()
          .then(function (r) {
            var s = r && r.data && r.data.session;
            if (!s) {
              return client.auth.refreshSession().catch(function () {});
            }
            var expAt = s.expires_at;
            // Refresh if token expires within the next 5 minutes
            if (expAt != null && Number(expAt) * 1000 < Date.now() + 300000) {
              return client.auth.refreshSession().catch(function () {});
            }
          })
          .catch(function () {});
      }, 500);
    }
    function onVis() {
      recoverWhenVisible();
    }
    function onPageShow(ev) {
      if (ev && ev.persisted) recoverWhenVisible();
    }
    function onFocus() {
      recoverWhenVisible();
    }
    document.addEventListener('visibilitychange', onVis);
    window.addEventListener('pageshow', onPageShow);
    window.addEventListener('focus', onFocus);
    _tabVisibilityCleanup = function () {
      document.removeEventListener('visibilitychange', onVis);
      window.removeEventListener('pageshow', onPageShow);
      window.removeEventListener('focus', onFocus);
      if (debounceTimer) clearTimeout(debounceTimer);
    };
    recoverWhenVisible();
  }

  function getBaseUrl() {
    if (typeof StudioAPI !== 'undefined' && StudioAPI.getBaseUrl) {
      return StudioAPI.getBaseUrl();
    }
    return (typeof window !== 'undefined' && window.location && window.location.origin) || '';
  }

  /**
   * Fetch /api/config and create Supabase browser client.
   * @returns {Promise<boolean>} true if Supabase client is ready (cloud sessions / gallery)
   */
  async function init() {
    if (_tabVisibilityCleanup) {
      try {
        _tabVisibilityCleanup();
      } catch (eClean) {}
      _tabVisibilityCleanup = null;
    }
    _client = null;
    _authEnabled = false;
    _loginGateRequired = false;
    _lastConfig = null;
    if (typeof StudioWaitingOverlay !== 'undefined' && StudioWaitingOverlay.push) {
      StudioWaitingOverlay.push('connecting');
    }
    try {
      try {
        var res = await fetch(getBaseUrl() + '/api/config');
        var cfg = await res.json();
        _lastConfig = Object.assign({}, cfg || {});
        var pubKey =
          (cfg && (cfg.supabase_anon_key || cfg.supabase_publishable_key)) || '';
        if (!cfg || !cfg.studio_cloud_available || !cfg.supabase_url || !pubKey) {
          return false;
        }
        var lib = typeof window !== 'undefined' ? window.supabase : null;
        var createClientFn =
          lib && (lib.createClient || (lib.default && lib.default.createClient));
        if (typeof createClientFn !== 'function') {
          console.warn('StudioAuth: @supabase/supabase-js not loaded');
          return false;
        }
        try {
          _client = createClientFn(cfg.supabase_url, pubKey, {
            auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: true }
          });
        } catch (clientErr) {
          console.warn('StudioAuth: createClient failed:', clientErr);
          _lastConfig._clientInitFailed = true;
          _lastConfig._clientInitError = String(
            clientErr && clientErr.message ? clientErr.message : clientErr
          );
          return false;
        }
        _authEnabled = true;
        _loginGateRequired = !!cfg.studio_auth_enabled;
        _wireTabVisibilityRecovery(_client);
        return true;
      } catch (e) {
        console.warn('StudioAuth init failed:', e);
        _lastConfig = { _fetchFailed: true, _fetchError: String(e && e.message ? e.message : e) };
        return false;
      }
    } finally {
      if (typeof StudioWaitingOverlay !== 'undefined' && StudioWaitingOverlay.pop) {
        StudioWaitingOverlay.pop();
      }
    }
  }

  function getLastConfig() {
    return _lastConfig;
  }

  /** True when server exposes Supabase URL + anon key (login UI can work). */
  function isCloudConfigured() {
    var c = _lastConfig;
    if (!c || c._fetchFailed) return false;
    var k = c.supabase_anon_key || c.supabase_publishable_key;
    return !!(c.studio_cloud_available && c.supabase_url && k);
  }

  function isAuthEnabled() {
    return _authEnabled && !!_client;
  }

  function isLoginGateRequired() {
    return _loginGateRequired && !!_client;
  }

  function getClient() {
    return _client;
  }

  async function getSession() {
    if (!_client) return null;
    var r = await _client.auth.getSession();
    return r.data && r.data.session ? r.data.session : null;
  }

  async function getUser() {
    if (!_client) return null;
    var r = await _client.auth.getUser();
    return r.data && r.data.user ? r.data.user : null;
  }

  async function getAccessToken() {
    if (!_client) return null;
    var s = await getSession();
    if (!s || !s.access_token) {
      try {
        var rr = await _client.auth.refreshSession();
        s = rr.data && rr.data.session ? rr.data.session : null;
      } catch (e) {
        s = null;
      }
    }
    return s && s.access_token ? s.access_token : null;
  }

  async function signUp(email, password) {
    if (!_client) throw new Error('Auth not configured');
    return _client.auth.signUp({ email: email.trim(), password: password });
  }

  async function signIn(email, password) {
    if (!_client) throw new Error('Auth not configured');
    return _client.auth.signInWithPassword({ email: email.trim(), password: password });
  }

  async function signOut() {
    if (!_client) return;
    await _client.auth.signOut();
  }

  function onAuthStateChange(cb) {
    if (!_client) return function () {};
    var sub = _client.auth.onAuthStateChange(function (event, session) {
      cb(event, session);
    });
    return function () {
      if (sub && sub.data && sub.data.subscription) sub.data.subscription.unsubscribe();
    };
  }

  function _sessionNameFromPayload(payload) {
    try {
      var p = (payload && payload.formSnapshot && payload.formSnapshot.prompt) || '';
      p = String(p).trim().slice(0, 60);
      if (p) return p;
    } catch (e) {}
    var d = new Date();
    return 'Session ' + d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
  }

  /**
   * Upsert user_sessions row. Pass existingServerSessionId to update.
   * @returns {Promise<string|null>} server session UUID
   */
  async function saveSessionToServer(payload, existingServerSessionId, nameOverride) {
    if (!_client) return null;
    var user = await getUser();
    if (!user) return null;
    var name = nameOverride || _sessionNameFromPayload(payload);
    var vt = null;
    try {
      vt = payload.formSnapshot && payload.formSnapshot.video_type;
    } catch (e) {}
    var row = {
      name: name,
      payload: payload,
      video_type: vt || null,
      updated_at: new Date().toISOString()
    };
    if (existingServerSessionId) {
      var up = await _client
        .from('user_sessions')
        .update(row)
        .eq('id', existingServerSessionId)
        .eq('user_id', user.id)
        .select('id')
        .maybeSingle();
      if (up.error) throw up.error;
      return existingServerSessionId;
    }
    var ins = await _client
      .from('user_sessions')
      .insert({
        user_id: user.id,
        name: row.name,
        payload: row.payload,
        video_type: row.video_type,
        updated_at: row.updated_at
      })
      .select('id')
      .single();
    if (ins.error) throw ins.error;
    return ins.data && ins.data.id ? ins.data.id : null;
  }

  async function loadSessionsFromServer() {
    if (!_client) return [];
    var user = await getUser();
    if (!user) return [];
    var r = await _client
      .from('user_sessions')
      .select('id,name,payload,video_type,created_at,updated_at')
      .eq('user_id', user.id)
      .order('updated_at', { ascending: false })
      .limit(50);
    if (r.error) {
      console.warn('loadSessionsFromServer:', r.error);
      return [];
    }
    return r.data || [];
  }

  async function deleteSessionFromServer(sessionId) {
    if (!_client || !sessionId) return;
    var user = await getUser();
    if (!user) return;
    await _client.from('user_sessions').delete().eq('id', sessionId).eq('user_id', user.id);
  }

  async function deleteAllSessionsFromServer() {
    if (!_client) return;
    var user = await getUser();
    if (!user) return;
    await _client.from('user_sessions').delete().eq('user_id', user.id);
  }

  async function loadMyVideos() {
    if (!_client) return [];
    var user = await getUser();
    if (!user) return [];
    var r = await _client
      .from('user_videos')
      .select('*')
      .eq('user_id', user.id)
      .order('created_at', { ascending: false })
      .limit(100);
    if (r.error) {
      console.warn('loadMyVideos:', r.error);
      return [];
    }
    return r.data || [];
  }

  async function deleteMyVideo(videoId) {
    if (!_client || !videoId) return;
    var user = await getUser();
    if (!user) return;
    await _client.from('user_videos').delete().eq('id', videoId).eq('user_id', user.id);
  }

  async function loadSessionPayloadById(sessionId) {
    if (!_client || !sessionId) return null;
    var user = await getUser();
    if (!user) return null;
    var r = await _client
      .from('user_sessions')
      .select('id,name,payload,video_type')
      .eq('id', sessionId)
      .eq('user_id', user.id)
      .maybeSingle();
    if (r.error || !r.data) return null;
    return r.data;
  }

  return {
    init: init,
    getLastConfig: getLastConfig,
    isCloudConfigured: isCloudConfigured,
    isAuthEnabled: isAuthEnabled,
    isLoginGateRequired: isLoginGateRequired,
    getClient: getClient,
    getSession: getSession,
    getUser: getUser,
    getAccessToken: getAccessToken,
    signUp: signUp,
    signIn: signIn,
    signOut: signOut,
    onAuthStateChange: onAuthStateChange,
    saveSessionToServer: saveSessionToServer,
    loadSessionsFromServer: loadSessionsFromServer,
    deleteSessionFromServer: deleteSessionFromServer,
    deleteAllSessionsFromServer: deleteAllSessionsFromServer,
    loadMyVideos: loadMyVideos,
    deleteMyVideo: deleteMyVideo,
    loadSessionPayloadById: loadSessionPayloadById
  };
})();

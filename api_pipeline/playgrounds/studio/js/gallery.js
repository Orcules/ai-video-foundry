/**
 * Video Studio: gallery after login + auth form wiring.
 */
var StudioGallery = (function () {
  function el(id) {
    return document.getElementById(id);
  }

  function showGallery() {
    var gv = el('studioGalleryView');
    var wv = el('studioWizardView');
    if (gv) gv.style.display = 'block';
    if (wv) wv.style.display = 'none';
    refresh();
    StudioAuth.getUser().then(function (u) {
      var em = el('studioUserEmail');
      if (em) em.textContent = u && u.email ? u.email : '';
    });
  }

  function openWizard() {
    var gv = el('studioGalleryView');
    var wv = el('studioWizardView');
    if (gv) gv.style.display = 'none';
    if (wv) wv.style.display = 'block';
    var back = el('btnBackToGallery');
    if (back) {
      back.style.display =
        window._studioWizardOpenedFromGallery ? 'inline-flex' : 'none';
    }
  }

  function escapeHtml(s) {
    if (s == null) return '';
    var d = document.createElement('div');
    d.textContent = String(s);
    return d.innerHTML;
  }

  var _refreshing = false;

  async function refresh() {
    if (_refreshing) return;
    _refreshing = true;
    var grid = el('studioGalleryGrid');
    var empty = el('studioGalleryEmpty');
    if (!grid) { _refreshing = false; return; }
    if (typeof StudioWaitingOverlay !== 'undefined' && StudioWaitingOverlay.push) {
      StudioWaitingOverlay.push('gallery');
    }
    try {
      var videos = await StudioAuth.loadMyVideos();
      grid.innerHTML = '';
      if (!videos.length) {
        if (empty) empty.style.display = 'block';
        return;
      }
      if (empty) empty.style.display = 'none';
      videos.forEach(function (v) {
        var card = document.createElement('div');
        card.className = 'studio-gallery-card';
        var thumb = (v.thumbnail_url && String(v.thumbnail_url).trim()) || '';
        var title = escapeHtml(v.title || 'Video');
        var vt = escapeHtml(v.video_type || '');
        var dateStr = '';
        try { dateStr = v.created_at ? new Date(v.created_at).toLocaleString() : ''; } catch (e) {}
        var vid = escapeHtml(v.id);
        var vurl = v.video_url ? escapeHtml(v.video_url) : '';

        card.innerHTML =
          '<div class="studio-gallery-card-thumb">' +
          (thumb ? '<img src="' + escapeHtml(thumb) + '" alt="" loading="lazy"/>' : '') +
          (vt ? '<div class="studio-gallery-card-status type-badge">' + vt + '</div>' : '') +
          (vurl
            ? '<div class="studio-gallery-card-play">' +
              '<button type="button" class="studio-gallery-card-play-btn" data-play="' + vid + '" title="Play">' +
              '<span class="material-symbols-outlined">play_arrow</span></button></div>'
            : '') +
          '</div>' +
          '<div class="studio-gallery-card-info">' +
          '<div class="studio-gallery-card-title">' + title + '</div>' +
          '<div class="studio-gallery-card-date">' + escapeHtml(dateStr) + '</div>' +
          '<div class="studio-gallery-card-actions">' +
          '<button type="button" class="studio-gallery-icon-btn primary" data-edit="' + vid + '">' +
          '<span class="material-symbols-outlined">edit</span>Edit</button>' +
          (vurl
            ? '<a class="studio-gallery-icon-btn" href="' + vurl + '" download target="_blank" rel="noopener" title="Download">' +
              '<span class="material-symbols-outlined">download</span></a>'
            : '') +
          (vurl
            ? '<button type="button" class="studio-gallery-icon-btn" data-play="' + vid + '" title="Play">' +
              '<span class="material-symbols-outlined">play_circle</span></button>'
            : '') +
          '<button type="button" class="studio-gallery-icon-btn danger" data-del="' + vid + '" title="Remove">' +
          '<span class="material-symbols-outlined">delete</span></button>' +
          '</div>' +
          '<div id="playwrap-' + vid + '" style="display:none;margin-top:0.75rem;">' +
          '<video controls style="width:100%;border-radius:0.5rem;" src="' + (vurl || '') + '"></video>' +
          '</div>' +
          '</div>';

        grid.appendChild(card);
      });

      grid.querySelectorAll('[data-play]').forEach(function (btn) {
        btn.addEventListener('click', function () {
          var id = btn.getAttribute('data-play');
          var w = el('playwrap-' + id);
          if (!w) return;
          var show = w.style.display === 'none';
          w.style.display = show ? 'block' : 'none';
          var vid = w.querySelector('video');
          if (vid && show) try { vid.play(); } catch (e) {}
        });
      });

      grid.querySelectorAll('[data-edit]').forEach(function (btn) {
      btn.addEventListener('click', async function () {
        if (typeof StudioWaitingOverlay !== 'undefined' && StudioWaitingOverlay.push) {
          StudioWaitingOverlay.push('sync');
        }
        try {
        var id = btn.getAttribute('data-edit');
        var videos = await StudioAuth.loadMyVideos();
        var v = videos.find(function (x) { return x.id === id; });
        if (!v) {
          alert('Video not found.');
          return;
        }
        if (!v.session_id) {
          alert('This video has no linked session. Start a new video to edit.');
          return;
        }
        var row = await StudioAuth.loadSessionPayloadById(v.session_id);
        if (!row || !row.payload) {
          alert('Session not found. It may have been deleted.');
          return;
        }
        if (typeof window.__studioApplyRestoredSession === 'function') {
          window._studioServerSessionId = v.session_id;
          window.__studioApplyRestoredSession(row.payload);
          window._studioWizardOpenedFromGallery = true;
          openWizard();
        }
        } finally {
          if (typeof StudioWaitingOverlay !== 'undefined' && StudioWaitingOverlay.pop) {
            StudioWaitingOverlay.pop();
          }
        }
      });
      });

      grid.querySelectorAll('[data-del]').forEach(function (btn) {
      btn.addEventListener('click', async function () {
        if (!confirm('Remove this video from your gallery?')) return;
        await StudioAuth.deleteMyVideo(btn.getAttribute('data-del'));
        refresh();
      });
      });
    } finally {
      _refreshing = false;
      if (typeof StudioWaitingOverlay !== 'undefined' && StudioWaitingOverlay.pop) {
        StudioWaitingOverlay.pop();
      }
    }
  }

  function setupAuthForms() {
    var overlay = el('studioAuthOverlay');
    if (!overlay) return;
    var dismiss = el('authOverlayDismiss');
    if (dismiss) {
      dismiss.style.display =
        typeof StudioAuth !== 'undefined' && StudioAuth.isLoginGateRequired()
          ? 'none'
          : 'block';
      dismiss.addEventListener('click', function () {
        if (StudioAuth.isLoginGateRequired()) return;
        overlay.style.display = 'none';
        overlay.setAttribute('aria-hidden', 'true');
      });
    }
    var tabIn = el('authTabSignIn');
    var tabReg = el('authTabRegister');
    var formIn = el('authFormSignIn');
    var formReg = el('authFormRegister');
    var err = el('authError');

    function showErr(msg) {
      if (err) {
        err.textContent = msg || '';
        err.style.display = msg ? 'block' : 'none';
      }
    }

    /** Map Supabase Auth errors to actionable Studio hints (English UI copy). */
    function formatAuthError(raw, context) {
      var s = (raw == null ? '' : String(raw)).trim();
      if (!s) return context === 'signin' ? 'Sign in failed' : 'Registration failed';
      if (/email not confirmed|not confirmed|email_not_confirmed/i.test(s)) {
        return (
          s +
          ' — In Supabase Dashboard: Authentication → Providers → Email → turn off Confirm email (save). ' +
          'For accounts already created: Authentication → Users → open the user → confirm email, or sign up again.'
        );
      }
      return s;
    }

    function setMode(reg) {
      if (formIn) formIn.style.display = reg ? 'none' : 'block';
      if (formReg) formReg.style.display = reg ? 'block' : 'none';
      if (tabIn) tabIn.classList.toggle('active', !reg);
      if (tabReg) tabReg.classList.toggle('active', !!reg);
      showErr('');
    }

    if (tabIn)
      tabIn.addEventListener('click', function () {
        setMode(false);
      });
    if (tabReg)
      tabReg.addEventListener('click', function () {
        setMode(true);
      });

    if (formIn) {
      formIn.addEventListener('submit', async function (ev) {
        ev.preventDefault();
        showErr('');
        var email = (el('authEmailIn') && el('authEmailIn').value) || '';
        var pw = (el('authPasswordIn') && el('authPasswordIn').value) || '';
        try {
          var r = await StudioAuth.signIn(email, pw);
          if (r.error) {
            showErr(formatAuthError(r.error.message, 'signin'));
            return;
          }
          overlay.style.display = 'none';
          overlay.setAttribute('aria-hidden', 'true');
          if (StudioAuth.isLoginGateRequired()) {
            showGallery();
          } else if (window._studioOpenGalleryAfterAuth) {
            showGallery();
            window._studioOpenGalleryAfterAuth = false;
          } else if (typeof window.__studioUpdateAccountStrip === 'function') {
            window.__studioUpdateAccountStrip();
          }
        } catch (e) {
          showErr((e && e.message) || String(e));
        }
      });
    }

    if (formReg) {
      formReg.addEventListener('submit', async function (ev) {
        ev.preventDefault();
        showErr('');
        var email = (el('authEmailReg') && el('authEmailReg').value) || '';
        var pw = (el('authPasswordReg') && el('authPasswordReg').value) || '';
        if ((pw || '').length < 6) {
          showErr('Password must be at least 6 characters.');
          return;
        }
        try {
          var r = await StudioAuth.signUp(email, pw);
          if (r.error) {
            showErr(formatAuthError(r.error.message, 'signup'));
            return;
          }
          if (r.data && r.data.user && !r.data.session) {
            showErr(
              'Account created but there is no session yet. If email confirmation is enabled in Supabase, check your inbox; ' +
                'otherwise turn off Confirm email under Authentication → Providers → Email, then use Sign in.'
            );
            setMode(false);
            return;
          }
          overlay.style.display = 'none';
          overlay.setAttribute('aria-hidden', 'true');
          if (StudioAuth.isLoginGateRequired()) {
            showGallery();
          } else if (window._studioOpenGalleryAfterAuth) {
            showGallery();
            window._studioOpenGalleryAfterAuth = false;
          } else if (typeof window.__studioUpdateAccountStrip === 'function') {
            window.__studioUpdateAccountStrip();
          }
        } catch (e) {
          showErr((e && e.message) || String(e));
        }
      });
    }
  }

  function wireGalleryChrome() {
    var newBtn = el('btnGalleryNewVideo');
    if (newBtn) {
      newBtn.addEventListener('click', function () {
        if (typeof window.__studioResetForNewVideo === 'function') {
          window.__studioResetForNewVideo();
        }
        window._studioWizardOpenedFromGallery = true;
        openWizard();
      });
    }
    var so = el('btnGallerySignOut');
    if (so) {
      so.addEventListener('click', async function () {
        await StudioAuth.signOut();
        var overlay = el('studioAuthOverlay');
        var gv = el('studioGalleryView');
        var wv = el('studioWizardView');
        if (StudioAuth.isLoginGateRequired()) {
          if (overlay) overlay.style.display = 'flex';
          if (gv) gv.style.display = 'none';
          if (wv) wv.style.display = 'none';
        } else {
          if (overlay) {
            overlay.style.display = 'none';
            overlay.setAttribute('aria-hidden', 'true');
          }
          if (gv) gv.style.display = 'none';
          if (wv) wv.style.display = 'block';
          window._studioWizardOpenedFromGallery = false;
          if (typeof window.__studioUpdateAccountStrip === 'function') {
            window.__studioUpdateAccountStrip();
          }
        }
      });
    }
    var back = el('btnBackToGallery');
    if (back) {
      back.addEventListener('click', function () {
        var busy = document.querySelector('.studio-step7-running') ||
          document.querySelector('#finalAssemblyLive[style*="block"]');
        if (busy) {
          if (!confirm('A job is still running. Leave the wizard? You can return via Previous sessions.')) return;
        }
        showGallery();
      });
    }
  }

  return {
    show: showGallery,
    openWizard: openWizard,
    refresh: refresh,
    setupAuthForms: setupAuthForms,
    wireGalleryChrome: wireGalleryChrome
  };
})();

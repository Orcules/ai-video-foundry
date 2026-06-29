/**
 * Main controller: step navigation, dropdowns, upload zones, mode toggle, generate + SSE.
 */
(function () {
  const TOTAL_STEPS = 15; /* legacy max for non–UGC Real; use studioTotalSteps() */
  /** Bump when wizard step indices change (e.g. removing a step) so saved sessions remap correctly. */
  const WIZARD_STEP_SCHEMA = 9;
  /** Only persist, list, auto-restore, and cloud-sync sessions once the wizard reached step 2+ (Style & duration). */
  const MIN_WIZARD_STEP_FOR_SESSION = 2;

  function migrateStoredWizardStep(rawStep, session) {
    var step = Math.max(1, Math.min(20, parseInt(rawStep, 10) || 1));
    var sch = session && session.wizardStepSchema ? session.wizardStepSchema : 0;
    if (sch < 2) {
      if (step >= 6) step = Math.min(20, step - 1);
    }
    if (sch < 3) {
      if (step >= 7) step = Math.min(20, step + 1);
    }
    var vt = session && session.videoType;
    if (!vt && session && session.formSnapshot) vt = session.formSnapshot.video_type;
    if (sch < 4 && vt === 'ugc-real' && step >= 6) step += 1;
    /* Schema 5: UGC Real — character gate moved after Preferences (old 6→8, steps ≥8 shift +1). */
    if (sch < 5 && vt === 'ugc-real') {
      if (step === 6) step = 8;
      else if (step >= 8) step = Math.min(20, step + 1);
    }
    /* Schema 6: UGC Real skips Scene assets / Scene prompts / Scene images (old 12–14); Final=12, Subs=13. */
    if (sch < 6 && vt === 'ugc-real') {
      if (step >= 12 && step <= 14) step = 12;
      else if (step === 15) step = 12;
      else if (step === 16) step = 13;
    }
    /* Schema 8: Non–UGC Real — character gate is wizard step 7 after Preferences; VO=9…Subs=15 (old 8–14 shift +1). */
    if (sch < 8 && vt !== 'ugc-real' && step >= 8) {
      step = Math.min(20, step + 1);
    }
    /* Schema 9: Product video — character gate moves after Scene assets; old step 7 → 12; steps ≥12 shift +1 (prompts…subs). */
    if (sch < 9 && vt === 'product video') {
      if (step === 7) step = 12;
      else if (step >= 12) step = Math.min(20, step + 1);
    }
    var maxS = vt === 'ugc-real' ? 13 : vt === 'product video' ? 16 : 15;
    return Math.max(1, Math.min(maxS, step));
  }

  function isStudioProductVideo() {
    try {
      return (
        typeof StudioSteps !== 'undefined' &&
        StudioSteps.getVideoType &&
        StudioSteps.getVideoType() === 'product video'
      );
    } catch (e) {
      return false;
    }
  }

  function studioTotalSteps() {
    if (isUgcRealFlow()) return 13;
    return isStudioProductVideo() ? 16 : 15;
  }

  /**
   * Influencer / personal-brand: wizard step 7 = character after Preferences.
   * Product video: character is later (see studioProductCharacterGateStep); this returns null so prefs → VO.
   */
  function studioNonUgcCharacterStep() {
    if (isUgcRealFlow()) return null;
    if (isStudioProductVideo()) return null;
    return 7;
  }

  /** Product video only: wizard step 12 = character approval after Scene assets, before Scene prompts. */
  function studioProductCharacterGateStep() {
    return isStudioProductVideo() && !isUgcRealFlow() ? 12 : null;
  }

  /** UGC Real: wizard step 7 = Preferences (offer TEXT 1–3). Step 6 is unused (jump 5 → 7). */
  function studioPrefsStep() {
    return isUgcRealFlow() ? 7 : 6;
  }

  /** UGC Real: wizard step 8 = character approval (after Preferences). */
  function studioUgcCharGateStep() {
    return isUgcRealFlow() ? 8 : null;
  }

  function studioUgcNineStep() {
    return isUgcRealFlow() ? 9 : 7;
  }

  function studioVoStep() {
    return isUgcRealFlow() ? 10 : 9;
  }

  function studioMusicStep() {
    return isUgcRealFlow() ? 11 : 10;
  }

  function studioAssetsStep() {
    return isUgcRealFlow() ? null : 11;
  }

  function studioPromptsStep() {
    if (isUgcRealFlow()) return null;
    return isStudioProductVideo() ? 13 : 12;
  }

  function studioImagesStep() {
    if (isUgcRealFlow()) return null;
    return isStudioProductVideo() ? 14 : 13;
  }

  function studioFinalStep() {
    return isUgcRealFlow() ? 12 : isStudioProductVideo() ? 15 : 14;
  }

  function studioSubsStep() {
    return isUgcRealFlow() ? 13 : isStudioProductVideo() ? 16 : 15;
  }

  /** First wizard step where Studio polls the job as “phase 3” range (UGC Real: Final onward; others: Scene assets onward). */
  function studioWizardPhase3PollMinStep() {
    return isUgcRealFlow() ? studioFinalStep() : studioAssetsStep();
  }

  /** Video type may not be on the DOM yet during session restore — use saved session fields. */
  function sessionLooksUgcReal(sess) {
    var vt = sess && sess.videoType;
    if (!vt && sess && sess.formSnapshot) vt = sess.formSnapshot.video_type;
    return vt === 'ugc-real';
  }

  function isCountableStudioSession(data) {
    if (!data || typeof data !== 'object') return false;
    return migrateStoredWizardStep(data.currentStep, data) >= MIN_WIZARD_STEP_FOR_SESSION;
  }

  /** After approving offer text on step 7, jump to character gate so the user can review the portrait while nine-cell builds. */
  function ugcRealOfferApproveGoToCharacterGate() {
    if (!isUgcRealFlow()) return;
    var gate = studioUgcCharGateStep();
    if (gate == null) return;
    if (currentStep === studioPrefsStep()) {
      goToStep(gate);
    }
  }

  /** PATCH body before resuming UGC Real after step_parse (edited offer fields → DB + input_params). */
  function buildUgcRealOfferResumePatchBody() {
    var t1 = (document.getElementById('text1') || {}).value || '';
    var t2 = (document.getElementById('text2') || {}).value || '';
    var t3 = (document.getElementById('text3') || {}).value || '';
    var parsed =
      typeof StudioSteps !== 'undefined' && StudioSteps.parseUgcOfferText3
        ? StudioSteps.parseUgcOfferText3(t3)
        : { key_benefits: t3, cta_text: '' };
    var kb = String(parsed.key_benefits || '').trim();
    var cta = String(parsed.cta_text || '').trim();
    var text3Display = kb + (cta ? '\n\nCTA: ' + cta : '');
    var prev = (collectedIntermediates && collectedIntermediates.ugc_real_intake) || {};
    var intake = Object.assign({}, prev, {
      target_audience: t1.trim(),
      main_problem: t2.trim(),
      key_benefits: kb,
      cta_text: cta || (prev.cta_text != null ? String(prev.cta_text) : '')
    });
    return {
      intermediates: {
        parsed_texts: {
          text_1: t1.trim(),
          text_2: t2.trim(),
          text_3: text3Display
        },
        ugc_real_intake: intake
      },
      input_params_patch: {
        text_1: t1.trim(),
        text_2: t2.trim(),
        text_3: text3Display,
        key_benefits: kb,
        cta_text: cta
      }
    };
  }

  /** After PATCH+resume from step_parse (Approve or char-gate background). */
  let _ugcRealOfferResumeIssued = false;
  let _ugcRealOfferResumeInFlight = false;

  /**
   * While the user is on the character gate, start offer analysis → nine-cell (same PATCH+resume as
   * "Approve offer text & continue") so the storyboard is ready or in-flight when they continue.
   */
  function ugcRealMaybeStartNineCellBackgroundResume() {
    if (!isUgcRealFlow() || !phase1JobId) return;
    if (_ugcRealOfferResumeIssued || _ugcRealOfferResumeInFlight) return;
    if (!step6AllPreferenceTextsFilled()) return;
    var jid = String(phase1JobId).trim();
    _ugcRealOfferResumeInFlight = true;
    StudioAPI.getJob(jid)
      .then(function (pre) {
        if (!pre) return null;
        if (pre.status === 'processing' || pre.status === 'completed') {
          _ugcRealOfferResumeIssued = true;
          return null;
        }
        if (pre.status !== 'paused') return null;
        var cs = (pre.current_step || '').trim();
        if (cs !== 'step_parse') {
          _ugcRealOfferResumeIssued = true;
          return null;
        }
        return StudioAPI.patchIntermediates(jid, buildUgcRealOfferResumePatchBody(), { skipWaitingOverlay: true }).then(
          function () {
            return StudioAPI.resumeJob(jid, { stop_after_scene_animations: true });
          }
        );
      })
      .then(function (resumeDone) {
        if (resumeDone !== null && resumeDone !== undefined) _ugcRealOfferResumeIssued = true;
      })
      .catch(function (e) {
        console.warn('ugcRealMaybeStartNineCellBackgroundResume', e);
      })
      .finally(function () {
        _ugcRealOfferResumeInFlight = false;
        try {
          pollJob();
        } catch (e2) {}
      });
  }

  /** Escape text for safe innerHTML (UGC Real plan / VO lines). */
  function ugcEscHtml(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function ugcDisplayText(s) {
    var t = s != null ? String(s).trim() : '';
    return t ? ugcEscHtml(t) : '—';
  }

  /**
   * True when UGC Real is in master-grid generation, grid cut, or grid-review (avoid a second /resume for the same segment).
   */
  function ugcRealGridWorkActive(job) {
    if (!job || typeof job !== 'object') return false;
    var im = job.intermediates || {};
    var cs = String(job.current_step || '').trim();
    var gu = im.grid_image_url != null ? String(im.grid_image_url).trim() : '';
    var hasGrid = !!(gu || (Array.isArray(im.scene_grids) && im.scene_grids.length));
    return hasGrid || cs === 'step_3' || cs === 'step_4' || cs === 'step_5';
  }

  /**
   * True when intermediates include at least one usable grid image URL (master, cut cells, or legacy scene_grids).
   */
  function ugcRealHasHttpGridImage(im) {
    if (!im || typeof im !== 'object') return false;
    var u = im.grid_image_url != null ? String(im.grid_image_url).trim() : '';
    if (u.indexOf('http') === 0) return true;
    var sg = im.scene_grids;
    if (Array.isArray(sg)) {
      for (var si = 0; si < sg.length; si++) {
        var s = sg[si];
        if (typeof s === 'string' && s.indexOf('http') === 0) return true;
      }
    }
    var cells = Array.isArray(im.grid_cells) ? im.grid_cells : [];
    for (var i = 0; i < cells.length; i++) {
      var c = cells[i];
      if (c && c.image_url && String(c.image_url).indexOf('http') === 0) return true;
    }
    return false;
  }

  /**
   * Client guard for POST /resume from grid review (matches server: step_3–5 + real image URLs; status paused or processing for UGC sync).
   */
  function ugcRealApproveGridsPreflight(jb) {
    jb = jb || {};
    var im = jb.intermediates || {};
    var st = String(jb.status || '');
    var cs = String(jb.current_step || '').trim();
    var vt = String(jb.video_type || '').trim().toLowerCase();
    if (vt !== 'ugc-real') {
      return { ok: false, title: '', message: 'Not a UGC Real job.' };
    }
    if (st !== 'paused' && st !== 'processing') {
      return {
        ok: false,
        title: 'Status must be Paused or Processing at grid review (currently ' + st + ').',
        message:
          'Approve grids only works when the job is Paused or Processing at grid review.\n\nCurrent status: ' +
          st +
          (cs ? ' · step: ' + cs : '') +
          '.',
      };
    }
    var hasImg = ugcRealHasHttpGridImage(im);
    var atGrid = cs === 'step_3' || cs === 'step_4' || cs === 'step_5';
    if (!atGrid) {
      var early =
        cs === 'step_1' || cs === 'step_2'
          ? 'If you only see **text** prompts per cell (storyboard): open **Nine-cell storyboard** and click **Continue to Phase 2**. After character approval and Style DNA, the server builds the 3×3 **image**; this button does not start that step.'
          : 'Server step: ' +
            (cs || 'unknown') +
            '. Use this button only after the 3×3 master grid / cell images exist (grid review, step_5).';
      return {
        ok: false,
        title: 'Wrong step for Approve grids (' + (cs || '?') + ').',
        message: 'You are not at grid **image** review yet.\n\n' + early,
      };
    }
    if (!hasImg) {
      return {
        ok: false,
        title: 'No http(s) grid image URLs yet — see API Log for step_3 / step_4.',
        message:
          'No master or cell image URL in this job yet.\n\n' +
          'Wait for Nano Banana / grid cut to finish, or check **API Log** (PIPE) if step_3 failed.',
      };
    }
    return {
      ok: true,
      title: 'Resume: per-cell VO, lip-sync, and nine animations (then Scene images).',
      message: '',
    };
  }

  function refreshUgcRealApproveGridsButton(jref, im) {
    var btn = document.getElementById('btnUgcRealApproveGrids');
    if (!btn) return;
    if (!isUgcRealFlow() || typeof currentStep !== 'number' || currentStep !== studioVoStep()) {
      btn.disabled = false;
      btn.removeAttribute('title');
      return;
    }
    var jb = {
      status: jref && jref.status,
      current_step: jref && jref.current_step,
      video_type: jref && jref.video_type,
      intermediates: im != null ? im : (jref && jref.intermediates) || {},
    };
    var pf = ugcRealApproveGridsPreflight(jb);
    btn.title = pf.title || '';
    btn.disabled = !pf.ok;
  }

  /**
   * Poll until job is paused (storyboard / review) or grid work has started, or timeout.
   * @returns {Promise<{kind:'paused', job: object}|{kind:'grid_running', job: object}>}
   */
  function ugcPollUntilPausedOrGridRunning(jobId, maxMs, intervalMs) {
    var deadline = Date.now() + maxMs;
    function tick() {
      return StudioAPI.getJob(jobId).then(function (j) {
        if (j.status === 'paused') return { kind: 'paused', job: j };
        if (ugcRealGridWorkActive(j)) return { kind: 'grid_running', job: j };
        if (j.status === 'failed' || j.status === 'completed' || j.status === 'aborted') {
          throw new Error('Job ended as ' + j.status + ' — cannot continue.');
        }
        if (Date.now() >= deadline) {
          throw new Error(
            'Timed out waiting for the job to pause. If the master grid / Nano Banana step is still running, open the VO step (after nine-cell) and wait — do not press Continue again until the job is Paused after the nine-cell storyboard.'
          );
        }
        return new Promise(function (resolve) {
          setTimeout(function () {
            resolve(tick());
          }, intervalMs);
        });
      });
    }
    return tick();
  }

  /**
   * Re-fetch job immediately before POST /resume so we never resume while status flipped to processing (race with grid/Kie).
   */
  function ugcRealResumeFromNineCellReview(jobId) {
    return StudioAPI.getJob(jobId).then(function (j) {
      if (ugcRealGridWorkActive(j)) {
        var busy =
          j.status === 'processing'
            ? 'The pipeline is already generating the master 3×3 grid (Nano Banana / Kie). Open the VO step and wait for the preview — do not click Continue again while status is Processing.'
            : 'The master grid step already finished. Go to the VO step (grid review lives there).';
        var err = new Error(busy);
        err._ugc_real_skip = true;
        err._ugc_real_open_step8 = true;
        throw err;
      }
      if (j.status === 'paused') {
        var cs = String(j.current_step || '').trim();
        if (cs === 'step_5' || cs === 'step_8') {
          var e2 = new Error(
            'You are past the nine-cell review. Go to the VO step (grid review). To regenerate the grid, use Regenerate or Restart from grid.'
          );
          e2._ugc_real_skip = true;
          e2._ugc_real_open_step8 = true;
          throw e2;
        }
        return StudioAPI.resumeJob(jobId, { stop_after_scene_animations: true });
      }
      if (j.status === 'processing') {
        var csP = String(j.current_step || '').trim();
        if (csP === 'step_1' || csP === 'step_2') {
          return ugcPollUntilPausedOrGridRunning(jobId, 180000, 2000).then(function (r) {
            if (r.kind === 'grid_running') {
              var e3 = new Error(
                'The master grid / Nano Banana step is already running. Open the VO step and watch progress — avoid clicking Continue again until the job pauses.'
              );
              e3._ugc_real_skip = true;
              e3._ugc_real_open_step8 = true;
              throw e3;
            }
            var j2 = r.job;
            var cs2 = String(j2.current_step || '').trim();
            if (cs2 === 'step_5' || cs2 === 'step_8') {
              var e4 = new Error('The job moved past storyboard review. Go to the VO step (grid review).');
              e4._ugc_real_skip = true;
              e4._ugc_real_open_step8 = true;
              throw e4;
            }
            return StudioAPI.resumeJob(jobId, { stop_after_scene_animations: true });
          });
        }
        throw new Error(
          'Job is still processing (' +
            (csP || 'unknown step') +
            '). Wait until it pauses after the nine-cell storyboard, then try Continue. If the grid is already generating, open the VO step.'
        );
      }
      throw new Error('Cannot continue: job status is ' + j.status + '.');
    });
  }

  const SESSION_STORAGE_KEY = 'studio_session';
  const SESSION_LIST_KEY = 'studio_session_list';
  const MAX_SESSION_LIST = 20;
  let currentStep = 1;
  let currentJobId = null;
  let phase1JobId = null;
  /** Phase 1 job id for which we already started non–UGC Phase 2 (VO script) from the character step. */
  let _nonUgcVoPhase2KickoffForPhase1 = null;
  let phase2JobId = null;
  let phase3JobId = null;
  let sseSource = null;
  /** True while sseSource is open and healthy — suppresses redundant HTTP polls. */
  let _sseActive = false;
  let collectedIntermediates = {};
  /** Last job ``output`` from a successful poll — persisted in session so final video links survive restore. */
  let _studioLastJobOutput = {};
  /** True when a saved job id returned 404 (wrong server, account, or deleted job). */
  let _studioLinkedJobsMissing = false;
  let currentSceneImages = [];
  let pollIntervalId = null;
  /** Last interval ms used for pollJob — so we can speed up while waiting on the character gate. */
  let _studioPollMs = 2000;
  let _phase1AutoStarted = false;
  let _musicAutoStarted = false;
  let _scenePromptsJobStarted = false;
  let _voicesCache = {};
  /** Generated voice IDs from the last /api/voice-design call, keyed by "lang|gender|desc" */
  let _voiceDesignCache = null;
  let _voiceDesignCacheKey = '';
  /** voice_id currently selected from the designed-voice previews (null = none chosen yet) */
  let _designedVoiceId = null;
  /** Bumped on each voice-design request so late responses cannot wipe newer UI (race with refreshStep7VoiceBlock / context changes). */
  let _voiceDesignRequestGen = 0;
  /** Same cacheKey as an in-flight /api/voice-design — avoids duplicate calls when prefetch + VO step both trigger. */
  let _voiceDesignInflightKey = null;
  /** After a failed design call, skip re-fetching until this timestamp (ms) unless user clicks Redesign (force). */
  let _voiceDesignCooldownUntil = 0;
  let _lastSceneImagesRendered = null;
  let _lastSceneVideosRendered = null;
  /** Monotonic per-slot merge so a flaky/empty GET job never wipes tiles already shown (Scene images + Final clips). */
  let _studioAccumulatedSceneImages = null;
  let _studioAccumulatedSceneVideos = null;
  /** Per scene index: keep showing a freshly generated URL until job intermediates match the same stripped URL (polling cannot revert the slot). */
  let _studioPinnedSceneImageByIndex = Object.create(null);
  let _lastScenePromptsJson = null;
  let _studioFinalAssemblyStarted = false;
  /** Phase-3 job id that received scene_images patch (or last Generate scene prompts). Used for Animate — avoids stale phase3JobId from races. */
  let _phase3AnimateJobId = null;
  /** After Approve grids & continue: show post-grid activity until server reports step_6+ or pauses at step_8. */
  let _ugcRealPostGridResumePending = false;
  /** Prevents duplicate parallel "Generate all scene images" runs (double-click = extra Kie/Vertex calls). */
  let _sceneImagesBatchInFlight = false;
  /** Merged local + cloud rows for Previous sessions panel (set by updateSessionListUI). */
  let _sessionListMerged = [];
  let _cloudSaveTimer = null;
  /** Last successful auto-generated portrait URL (cache + dedupe; also applied to character zone when no upload). */
  let _characterReviewPendingUrl = null;
  /** Set to true after user clicks Approve on auto-generated portrait; cleared on new generation/step. */
  let _characterApproved = false;
  /** In-flight POST /api/generate-character (background prefetch). */
  let _characterPrefetchPromise = null;
  /** Bumped when locale / gender / video type / prompt changes so stale responses are ignored. */
  let _characterPrefetchGen = 0;
  let _characterPrefetchDebounceTimer = null;
  /** Last /api/generate-character prefetch error message (step 7 status panel). */
  let _characterPortraitPrefetchError = null;
  /** Cache key (prompt + video type + country + language) for AI character-brief chips on step 4. */
  let _characterSuggestCacheKey = '';
  let _characterSuggestCacheList = [];
  let _characterSuggestInFlight = false;
  /** character_id -> CharacterRecord from last GET /api/characters load. */
  let _characterLibraryById = {};
  /** In-flight promise for refreshCharacterLibrarySelect — dedups concurrent calls. */
  let _characterLibraryInFlight = null;
  /** Timestamp (Date.now()) when the user clicked Animate all / Retry all — drives the startup-indicator elapsed counter. */
  let _animateAllClickedAt = null;
  /** Set to true once the user successfully generates VO audio on step 9 — gates Approve and continue. */
  let _voGeneratedForApprove = false;
  let _ugcRealScenePlan = null;
  let _ugcRealSceneGrids = [];
  let _ugcRealGridManifests = [];
  let _ugcRealFrameClassifications = [];
  /** Phase 1 job id last bound to _ugcReal* — when phase1JobId changes, caches must reset (avoid mixing sessions). */
  let _studioUgcPhase1BinderId = null;
  /** Last polled Phase 1 job status — used to block resume when job is failed/completed. */
  let _phase1LastPolledStatus = null;
  /** Last successful GET /api/jobs/{id} body — avoids Nine-cell step flashing empty job → “Connecting” forever on navigation. */
  let _lastPollJobSnapshot = null;
  /** Nine-cell activity panel: detect flat progress so we can explain step-based % and show a busy bar. */
  let _ugcNineCellPanelJobId = null;
  let _ugcNineCellLastPollSig = '';
  let _ugcNineCellFlatPollCount = 0;
  /** Cached product copy for Scene images step (restored when leaving UGC Real mode). */
  let _studioProductSceneImagesDescHtml = null;
  var STUDIO_PRODUCT_SCENE_IMAGES_DESC_HTML =
    'The browser calls <strong>your API</strong> (<code>/api/generate-scene-image</code>) — not Kie directly — so DevTools will not show requests to <code>kie.ai</code>. If you see <strong>0 / N</strong> and no placeholders, you skipped generation: click <strong>Generate all scene images</strong> below (or go back to step 10 and use <strong>Generate images</strong>). Add correction text and use Regenerate / Fix as needed. When satisfied, click <strong>Animate all</strong>. <strong>Timing:</strong> each image is often ~30–90 seconds. Failed slots show a red message.';

  function studioResetUgcRealClientCaches() {
    _studioUgcPhase1BinderId = null;
    _lastPollJobSnapshot = null;
    _ugcNineCellPanelJobId = null;
    _ugcNineCellLastPollSig = '';
    _ugcNineCellFlatPollCount = 0;
    _ugcRealScenePlan = null;
    _ugcRealSceneGrids = [];
    _ugcRealGridManifests = [];
    _ugcRealFrameClassifications = [];
    _ugcRealOfferResumeIssued = false;
    _ugcRealOfferResumeInFlight = false;
    _ugcRealPostGridResumePending = false;
  }

  /**
   * When Phase 1 job id changes, drop grid/storyboard/scene-image client state so we never show
   * another job's nine-cell, master grid, or cell crops under the current session.
   */
  function studioUgcClearPhase1GridClientState() {
    try {
      delete collectedIntermediates.grid_cells;
      delete collectedIntermediates.grid_image_url;
      delete collectedIntermediates.ugc_grid_cut_from_url;
      delete collectedIntermediates.scene_grids;
      delete collectedIntermediates.grid_manifests;
      delete collectedIntermediates.frame_routing;
      delete collectedIntermediates.frame_classifications;
      delete collectedIntermediates.scene_images;
      delete collectedIntermediates.nine_cell_plan;
    } catch (e) {}
    currentSceneImages = [];
    _lastSceneImagesRendered = null;
    resetStudioMonotonicMediaAccumulators();
    try {
      window._sceneImageErrors = [];
    } catch (e2) {}
    try {
      _studioPinnedSceneImageByIndex = {};
    } catch (e3) {}
  }

  function isUgcRealFlow() {
    return typeof StudioSteps !== 'undefined' && StudioSteps.isUgcRealFlow && StudioSteps.isUgcRealFlow();
  }

  /**
   * After nine-cell review, UGC Real usually continues on Phase 1. phase2JobId may exist from other flows;
   * for polling we prefer Phase 1 first so grid/intermediates are not read from a stale job.
   */
  function ugcRealPrimaryPipelineJobId() {
    // UGC Real: grid, stills, and clips are persisted on Phase 1. A stale Phase 2 id from another flow
    // would poll the wrong job (empty intermediates, product-like step_3, 0/1 scene counts).
    var candidates = isUgcRealFlow()
      ? [phase1JobId, phase2JobId, currentJobId]
      : [phase2JobId, phase1JobId, currentJobId];
    for (var i = 0; i < candidates.length; i++) {
      var s = candidates[i] != null ? String(candidates[i]).trim() : '';
      if (s.length >= 8) return s;
    }
    return '';
  }

  /** Build scene_images[] from UGC Real grid_cells (ordered by cell_index 1–9) for the Scene images step. */
  function ugcRealOrderedSceneImagesFromGridCells(gridCells) {
    if (!Array.isArray(gridCells) || !gridCells.length) return [];
    var byIx = {};
    for (var i = 0; i < gridCells.length; i++) {
      var ce = gridCells[i];
      if (!ce || typeof ce !== 'object') continue;
      var ix = parseInt(String(ce.cell_index || ''), 10);
      if (!isFinite(ix) || ix < 1 || ix > 9) continue;
      var u = ce.image_url;
      if (u != null && typeof u === 'string' && u.indexOf('http') === 0) byIx[ix] = u;
    }
    var out = [];
    for (var j = 1; j <= 9; j++) {
      if (byIx[j]) out.push(byIx[j]);
    }
    return out;
  }

  /** Resolve grid_cells row by 1-based cell_index, else fall back to array position. */
  function ugcRealFindGridCellForIndex(gcArr, oneBased) {
    if (!Array.isArray(gcArr) || !gcArr.length) return null;
    for (var i = 0; i < gcArr.length; i++) {
      var g = gcArr[i];
      if (!g || typeof g !== 'object') continue;
      if (parseInt(String(g.cell_index || ''), 10) === oneBased) return g;
    }
    var idx = oneBased - 1;
    return gcArr[idx] && typeof gcArr[idx] === 'object' ? gcArr[idx] : null;
  }

  /** Return a new grid_cells array with image_url set for the given zero-based scene index. */
  function ugcRealMergeGridCellImageUrl(gridCells, zeroBasedIndex, imageUrl) {
    var want = zeroBasedIndex + 1;
    var arr = Array.isArray(gridCells) ? gridCells.slice() : [];
    var found = -1;
    for (var i = 0; i < arr.length; i++) {
      var g = arr[i];
      if (!g || typeof g !== 'object') continue;
      if (parseInt(String(g.cell_index || ''), 10) === want) {
        found = i;
        break;
      }
    }
    if (found >= 0) {
      arr[found] = Object.assign({}, arr[found], { cell_index: want, image_url: imageUrl });
      return arr;
    }
    while (arr.length < want) {
      arr.push({ cell_index: arr.length + 1 });
    }
    arr[want - 1] = Object.assign({}, arr[want - 1] || {}, { cell_index: want, image_url: imageUrl });
    return arr;
  }

  /**
   * Final video + scene-images status lines require the polled job id to match. Product uses phase 3 only;
   * UGC Real often animates on the same job as phase 1/2 (no separate phase-3 job).
   */
  function studioScenesFinalPollMatch() {
    var c = String(currentJobId || '').trim();
    if (!c) return false;
    if (isUgcRealFlow()) {
      var u = ugcRealPrimaryPipelineJobId();
      return !!u && c === u;
    }
    var p3 = phase3JobForAnimate();
    return !!(p3 && c === String(p3).trim());
  }

  /**
   * Show/update the animation startup panel (spinner + elapsed time) while the animation pipeline is
   * initialising after the user clicked "Animate all". Hidden automatically when clips start arriving
   * or when the job is no longer processing.
   */
  function refreshAnimationStartupPanel(jobRef, doneCount, sceneCount) {
    var panel = document.getElementById('animationStartupPanel');
    var txt = document.getElementById('animationStartupText');
    var sub = document.getElementById('animationStartupSub');
    if (!panel || !txt || !sub) return;
    var isOnFinal = currentStep === studioFinalStep();
    var isProcessing = jobRef && jobRef.status === 'processing';
    // Hide when not on the final step, when all clips are ready, or when job is not processing
    if (!isOnFinal || !isProcessing || (sceneCount > 0 && doneCount >= sceneCount)) {
      panel.style.display = 'none';
      return;
    }
    var stepHint = ((jobRef && jobRef.current_step) || '').trim();
    var elapsed = '';
    if (_animateAllClickedAt) {
      var ms = Date.now() - _animateAllClickedAt;
      var s = Math.floor(ms / 1000);
      elapsed = s < 60 ? (s + 's') : (Math.floor(s / 60) + 'm ' + (s % 60) + 's');
    }
    // "scene_generation" is the monolith step name during animation; also catch variants
    var isAnimationStep =
      stepHint === 'scene_generation' ||
      stepHint.indexOf('anim') !== -1 ||
      (stepHint.indexOf('scene') !== -1 && stepHint !== 'step_3' && stepHint !== 'step_8');
    panel.style.display = 'flex';
    if (doneCount === 0 && !isAnimationStep) {
      txt.textContent = 'Starting animation pipeline\u2026' + (elapsed ? ' (' + elapsed + ' elapsed)' : '');
      sub.textContent =
        'The server is initialising the animation phase' +
        (stepHint ? ' \u2014 current pipeline step: ' + stepHint : '') +
        '. First Veo requests typically go out within 1\u20134 min of setup. Keep this tab open.';
    } else if (doneCount === 0) {
      txt.textContent = 'Animation requests sent \u2014 waiting for Veo' + (elapsed ? ' (' + elapsed + ' elapsed)' : '');
      sub.textContent =
        'All ' + sceneCount + ' requests have been dispatched to Veo. Each render takes 10\u201325 min. ' +
        'Clips appear in the grid below as each one finishes.';
    } else {
      txt.textContent = doneCount + '\u00a0/\u00a0' + sceneCount + ' clips ready' + (elapsed ? ' (' + elapsed + ' elapsed)' : '');
      sub.textContent =
        (sceneCount - doneCount) + ' still rendering' +
        (stepHint ? ' \u2014 step: ' + stepHint : '') +
        '. Each finished clip appears in the grid below.';
    }
  }

  function ugcRealNormalizeStoryboardPlan(plan) {
    if (!plan || typeof plan !== 'object') return null;
    if (Array.isArray(plan.cells) && plan.cells.length === 9) return plan;
    if (Array.isArray(plan.scenes) && plan.scenes.length === 9) {
      var mappedCells = plan.scenes.map(function (scene, index) {
        scene = scene && typeof scene === 'object' ? scene : {};
        return {
          cell_index: index + 1,
          visual_prompt:
            scene.visual_prompt ||
            scene.image_prompt ||
            scene.scene_image_prompt ||
            scene.scene_prompt ||
            scene.primary_message ||
            '',
          voice_line: scene.voice_line || scene.vo_line || scene.script_line || '',
          lipsync: scene.lipsync != null ? scene.lipsync : !!scene.speaking_required,
          shot_role: scene.shot_role || scene.purpose || 'b_roll',
          duration_seconds: scene.duration_seconds,
        };
      });
      var filledCells = mappedCells.filter(function (cell) {
        return !!(String(cell.visual_prompt || '').trim() && String(cell.voice_line || '').trim());
      }).length;
      if (filledCells >= 7) return { cells: mappedCells };
    }
    return null;
  }

  function ugcRealGetStoryboardPlan(source) {
    if (!source || typeof source !== 'object') return null;
    return ugcRealNormalizeStoryboardPlan(source.nine_cell_plan || source);
  }

  /** True when cached plan has valid nine-cell rows to show in the storyboard card. */
  function ugcRealPlanHasCells() {
    return !!ugcRealGetStoryboardPlan(_ugcRealScenePlan);
  }

  /** Remove client-only cache-buster (?_=ts / &=ts) before PATCH or comparing to server URLs. */
  function stripStudioSceneImageCacheBuster(u) {
    if (!u || typeof u !== 'string') return u;
    return u.replace(/&_=+\d+$/, '').replace(/\?_=+\d+$/, '');
  }

  function studioClearAllSceneImagePins() {
    _studioPinnedSceneImageByIndex = Object.create(null);
  }

  function studioPinSceneImageSlot(index, displayUrl, urlForStrip) {
    if (index == null || index < 0) return;
    var base = urlForStrip != null ? urlForStrip : displayUrl;
    var stripped = stripStudioSceneImageCacheBuster(typeof base === 'string' ? base : '');
    if (!stripped) return;
    _studioPinnedSceneImageByIndex[index] = { stripped: stripped, displayUrl: displayUrl };
  }

  function studioUnpinSceneImageSlot(index) {
    if (index == null || index < 0) return;
    delete _studioPinnedSceneImageByIndex[index];
  }

  /** True if job intermediates contain real TEXT 1–3 (non-empty after trim). */
  function parsedTextsHaveBody(pt) {
    if (!pt || typeof pt !== 'object') return false;
    function nz(x) {
      if (x == null) return false;
      return String(x).trim().length > 0;
    }
    return nz(pt.text_1) || nz(pt.text_2) || nz(pt.text_3);
  }

  /** Step 6: Phase 2 / VO requires all three preference fields (from pipeline or user edit). */
  function step6AllPreferenceTextsFilled() {
    var el1 = document.getElementById('text1');
    var el2 = document.getElementById('text2');
    var el3 = document.getElementById('text3');
    function nz(el) {
      return el && el.value != null && String(el.value).trim().length > 0;
    }
    return nz(el1) && nz(el2) && nz(el3);
  }

  function resetNonUgcVoPhase2KickoffIfPhase1Changed() {
    if (!_nonUgcVoPhase2KickoffForPhase1) return;
    if (!phase1JobId || String(phase1JobId).trim() !== String(_nonUgcVoPhase2KickoffForPhase1).trim()) {
      _nonUgcVoPhase2KickoffForPhase1 = null;
    }
  }

  /** True on influencer/personal-brand step 7 or product step 12 (same Character approval DOM). */
  function studioIsOnNonUgcCharacterGateStep() {
    if (isUgcRealFlow()) return false;
    if (typeof currentStep !== 'number') return false;
    if (studioNonUgcCharacterStep() != null && currentStep === studioNonUgcCharacterStep()) return true;
    if (studioProductCharacterGateStep() != null && currentStep === studioProductCharacterGateStep()) return true;
    return false;
  }

  function studioIsNonUgcCharacterGateStepNum(stepNum) {
    if (isUgcRealFlow()) return false;
    if (typeof stepNum !== 'number') return false;
    if (studioNonUgcCharacterStep() != null && stepNum === studioNonUgcCharacterStep()) return true;
    if (studioProductCharacterGateStep() != null && stepNum === studioProductCharacterGateStep()) return true;
    return false;
  }

  function studioFirstHttpCharacterUrlFromList(values) {
    if (!values) return '';
    var arr = Array.isArray(values) ? values : [values];
    for (var i = 0; i < arr.length; i++) {
      var u = arr[i];
      if (!u || typeof u !== 'string') continue;
      var t = u.trim();
      if (/^https?:\/\//i.test(t)) return t;
    }
    return '';
  }

  /**
   * Portrait URL from job intermediates / output / input_params (Phase 1 often has influencer_image before Studio prefetch returns).
   */
  function studioExtractCharacterPortraitUrlFromJob(jobRef, im, out) {
    im = im || {};
    out = out || {};
    var job = jobRef || {};
    var ip = job.input_params && typeof job.input_params === 'object' ? job.input_params : {};
    var lists = [];
    function pushMany(v) {
      if (v == null) return;
      if (Array.isArray(v)) {
        for (var j = 0; j < v.length; j++) lists.push(v[j]);
      } else lists.push(v);
    }
    pushMany(im.influencer_image);
    pushMany(out.influencer_image);
    pushMany(im.character_url);
    pushMany(ip.character_url);
    pushMany(im.character_urls);
    pushMany(ip.character_urls);
    pushMany(im.bg_removed_character_urls);
    return studioFirstHttpCharacterUrlFromList(lists);
  }

  /**
   * When the pipeline already stored a portrait URL but Studio only tracked prefetch / Phase 2 poll, wire preview + slot.
   */
  function syncNonUgcCharacterPortraitFromJob(im, out, jobRef) {
    if (isUgcRealFlow()) return;
    if (!studioIsOnNonUgcCharacterGateStep()) return;
    var vt =
      typeof StudioSteps !== 'undefined' && StudioSteps.getVideoType ? StudioSteps.getVideoType() : '';
    if (
      vt === 'product video' &&
      StudioSteps.isProductNoOnScreenCharacter &&
      StudioSteps.isProductNoOnScreenCharacter()
    ) {
      return;
    }
    if (StudioSteps.hasUploadedCharacter && StudioSteps.hasUploadedCharacter()) return;
    var discovered = studioExtractCharacterPortraitUrlFromJob(jobRef, im, out);
    if (!discovered) return;
    var p = String(_characterReviewPendingUrl || '').trim();
    var s = String(
      (StudioSteps.getPrimaryCharacterHttpUrl && StudioSteps.getPrimaryCharacterHttpUrl()) || ''
    ).trim();
    if (p === discovered || s === discovered) return;
    if (p && /^https?:\/\//i.test(p)) return;
    if (s && /^https?:\/\//i.test(s)) return;
    _characterPortraitPrefetchError = null;
    _characterReviewPendingUrl = discovered;
    try {
      applyCharacterUrl(discovered);
    } catch (eA) {}
    try {
      showCharPreview(discovered);
    } catch (eSh) {}
    try {
      saveSession();
    } catch (eSv) {}
  }

  function studioDesiredPollIntervalMs() {
    if (isUgcRealFlow()) return 2000;
    if (!studioIsOnNonUgcCharacterGateStep()) return 2000;
    var p = String(_characterReviewPendingUrl || '').trim();
    var s = String(
      (StudioSteps.getPrimaryCharacterHttpUrl && StudioSteps.getPrimaryCharacterHttpUrl()) || ''
    ).trim();
    var hasPreview = (p && /^https?:\/\//i.test(p)) || (s && /^https?:\/\//i.test(s));
    /* Faster poll while portrait POST is in flight or preview missing — job may gain influencer_image soon. */
    if (!hasPreview || _characterPrefetchPromise) return 500;
    return 2000;
  }

  function studioEnsureJobPollInterval() {
    if (!currentJobId) return;
    var want = studioDesiredPollIntervalMs();
    if (pollIntervalId && _studioPollMs === want) return;
    _studioPollMs = want;
    if (pollIntervalId) {
      try {
        clearInterval(pollIntervalId);
      } catch (eClr) {}
    }
    pollIntervalId = setInterval(pollJob, want);
  }

  /**
   * Step 7 (non–UGC character gate): live portrait prefetch status + keep preview visible when URL exists.
   */
  function refreshNonUgcCharacterPortraitStatus() {
    var act = document.getElementById('nonUgcCharPortraitActivity');
    var spin = document.getElementById('nonUgcCharPortraitSpinner');
    var txt = document.getElementById('nonUgcCharPortraitActivityText');
    var sub = document.getElementById('nonUgcCharPortraitActivitySub');
    var retry = document.getElementById('btnNonUgcCharPortraitRetry');
    var noPh = document.getElementById('nonUgcCharNoPortraitHint');
    if (!act || !txt) return;
    if (isUgcRealFlow()) {
      act.style.display = 'none';
      if (spin) spin.style.display = 'none';
      if (retry) retry.style.display = 'none';
      return;
    }
    if (!studioIsOnNonUgcCharacterGateStep()) {
      act.style.display = 'none';
      if (spin) spin.style.display = 'none';
      if (retry) retry.style.display = 'none';
      return;
    }
    var vt =
      typeof StudioSteps !== 'undefined' && StudioSteps.getVideoType ? StudioSteps.getVideoType() : '';
    var prodNo =
      vt === 'product video' &&
      StudioSteps.isProductNoOnScreenCharacter &&
      StudioSteps.isProductNoOnScreenCharacter();
    if (prodNo) {
      act.style.display = 'none';
      if (spin) spin.style.display = 'none';
      if (retry) retry.style.display = 'none';
      return;
    }
    var userUp = StudioSteps.hasUploadedCharacter && StudioSteps.hasUploadedCharacter();
    var slotUrl =
      (StudioSteps.getPrimaryCharacterHttpUrl && StudioSteps.getPrimaryCharacterHttpUrl()) || '';
    var pending = _characterReviewPendingUrl && String(_characterReviewPendingUrl).trim();
    var displayUrl = pending || slotUrl;
    if (!displayUrl) {
      var pvWrap = document.getElementById('step6CharPreview');
      var pvImg = document.getElementById('step6CharImg');
      var rawSrc = pvImg && pvImg.getAttribute ? pvImg.getAttribute('src') : '';
      if (
        pvWrap &&
        pvImg &&
        pvWrap.style.display !== 'none' &&
        rawSrc &&
        /^https?:\/\//i.test(String(rawSrc).trim())
      ) {
        displayUrl = String(rawSrc).trim();
      }
    }
    var loading = !!_characterPrefetchPromise;
    if (_characterApproved && slotUrl) {
      act.style.display = 'block';
      txt.textContent = 'Character approved \u2714';
      if (sub) {
        sub.textContent = 'Portrait applied to your character slot. Continue to the next step or regenerate to change it.';
      }
      if (spin) spin.style.display = 'none';
      if (retry) retry.style.display = 'inline-block';
      if (noPh) noPh.style.display = 'none';
      return;
    }
    if (userUp) {
      act.style.display = 'block';
      txt.textContent = 'Using your uploaded character from step 4.';
      if (sub) {
        sub.textContent = 'No server portrait run is required here. Approve or continue when ready.';
      }
      if (spin) spin.style.display = 'none';
      if (retry) retry.style.display = 'none';
      if (slotUrl) {
        try {
          showCharPreview(slotUrl);
        } catch (eUp) {}
      }
      if (noPh) noPh.style.display = 'none';
      return;
    }
    if (displayUrl) {
      act.style.display = 'block';
      txt.textContent = 'Portrait ready — review below.';
      if (sub) {
        sub.textContent =
          'Linked to your character slot for Phase 2. Regenerate with a correction, or Approve to confirm.';
      }
      if (spin) spin.style.display = 'none';
      if (retry) retry.style.display = 'none';
      try {
        showCharPreview(displayUrl);
      } catch (eSh) {}
      if (noPh) noPh.style.display = 'none';
      return;
    }
    if (loading) {
      act.style.display = 'block';
      if (spin) spin.style.display = 'inline-block';
      txt.textContent = 'Generating portrait — in-flight POST /api/generate-character…';
      if (sub) {
        sub.textContent =
          'Open API Log to watch the request. When the server returns an image URL, the preview appears below automatically.';
      }
      if (retry) retry.style.display = 'none';
      if (noPh) noPh.style.display = 'none';
      return;
    }
    if (_characterPortraitPrefetchError) {
      act.style.display = 'block';
      if (spin) spin.style.display = 'none';
      txt.textContent = 'Portrait generation failed.';
      if (sub) sub.textContent = String(_characterPortraitPrefetchError).slice(0, 360);
      if (retry) retry.style.display = 'inline-block';
      if (noPh) noPh.style.display = 'none';
      return;
    }
    var wantPrefetch = shouldPrefetchAutoCharacter();
    act.style.display = 'block';
    if (spin) spin.style.display = 'none';
    if (wantPrefetch) {
      txt.textContent = 'Waiting for an auto portrait — none has completed yet.';
      if (sub) {
        sub.textContent =
          'Stay on this step or use “Generate portrait again”. You can change the look on step 4 if needed. Phase 2 can still run in the background.';
      }
      if (retry) retry.style.display = 'inline-block';
    } else {
      txt.textContent = 'No auto-portrait for this setup.';
      if (sub) {
        sub.textContent = 'Use step 4 to upload a character or describe the look, then return here.';
      }
      if (retry) retry.style.display = 'none';
    }
    if (noPh) noPh.style.display = wantPrefetch ? 'none' : 'block';
  }

  /**
   * Standard pipelines: start Phase 2 (VO script) in the background while the user stays on the character step.
   * No-op if already started for this phase1 id or phase2 already exists.
   */
  function ensureNonUgcVoPhase2Kickoff() {
    if (isUgcRealFlow()) return;
    resetNonUgcVoPhase2KickoffIfPhase1Changed();
    if (!phase1JobId || !step6AllPreferenceTextsFilled()) return;
    if (phase2JobId) return;
    if (_nonUgcVoPhase2KickoffForPhase1 === String(phase1JobId).trim()) return;
    _nonUgcVoPhase2KickoffForPhase1 = String(phase1JobId).trim();
    var hint = document.getElementById('nonUgcCharVoBackgroundHint');
    if (hint) {
      hint.style.display = 'block';
      hint.textContent =
        'Starting Phase 2 in the background (VO script generation). Approve the character below; open the VO step when you are ready to edit script and voice.';
    }
    if (
      _characterReviewPendingUrl &&
      typeof StudioSteps !== 'undefined' &&
      StudioSteps.hasUploadedCharacter &&
      !StudioSteps.hasUploadedCharacter() &&
      !(StudioSteps.isProductNoOnScreenCharacter && StudioSteps.isProductNoOnScreenCharacter())
    ) {
      try {
        applyCharacterUrl(_characterReviewPendingUrl);
        /* Do not hide preview on the character step — async kickoff runs after paint; hiding here left step 7 blank. */
        if (studioIsOnNonUgcCharacterGateStep()) {
          showCharPreview(_characterReviewPendingUrl);
          try {
            refreshNonUgcCharacterPortraitStatus();
          } catch (eRfKick) {}
        } else {
          hideCharPreview();
        }
      } catch (eA) {}
    }
    promiseWithTimeout(
      uploadPendingZones(),
      240000,
      'File uploads timed out (4 minutes). Check the network, finish or remove pending uploads in step 4/9, then try again.'
    )
      .then(function () {
        var payload = StudioSteps.collectPhase2Payload(phase1JobId);
        return StudioAPI.generate(payload);
      })
      .then(function (res) {
        var jid = res && (res.job_id != null ? res.job_id : res.id);
        if (jid == null || String(jid).trim() === '') {
          _nonUgcVoPhase2KickoffForPhase1 = null;
          if (hint) {
            hint.textContent =
              'Could not start Phase 2 (no job id). Check the API Log or try **Generate VO** on Preferences again.';
            hint.style.display = 'block';
          }
          return;
        }
        phase2JobId = String(jid).trim();
        phase3JobId = null;
        _phase3AnimateJobId = null;
        _scenePromptsJobStarted = false;
        currentJobId = phase2JobId;
        pollJob();
        if (pollIntervalId) clearInterval(pollIntervalId);
        pollIntervalId = setInterval(pollJob, 2000);
        if (sseSource) {
          try {
            sseSource.close();
          } catch (eSse) {}
          sseSource = null;
          _sseActive = false;
        }
        StudioAPI.connectSSE(currentJobId, function (eventType, data) {
          if (data.event_type === 'complete' || data.event_type === 'error' || data.event_type === 'abort') {
            if (pollIntervalId) clearInterval(pollIntervalId);
            pollIntervalId = null;
            _sseActive = false;
          } else if (data.event_type === 'pause') {
            _sseActive = false;
            if (!pollIntervalId) pollIntervalId = setInterval(pollJob, 2000);
          }
          pollJob();
        }).then(function (es) {
          sseSource = es;
          _sseActive = true;
        });
        saveSession();
        if (!_musicAutoStarted) {
          _musicAutoStarted = true;
          StudioAPI.generateMusic(StudioSteps.collectMusicPayload())
            .then(function (data) {
              applyMusicGenerateResult(data);
              if (data.music_url || data.music_description) {
                return patchStudioMusicIntermediates(data);
              }
            })
            .catch(function () {
              _musicAutoStarted = false;
            });
        } else if (phase2JobId && (collectedIntermediates.music_url || collectedIntermediates.music_description)) {
          patchStudioMusicIntermediates({
            music_url: collectedIntermediates.music_url,
            music_description: collectedIntermediates.music_description
          }).catch(function () {});
        }
        if (hint) {
          hint.textContent =
            'Phase 2 is running — the VO script will appear on the next step when the server saves it (this page polls every ~2s).';
        }
      })
      .catch(function (err) {
        _nonUgcVoPhase2KickoffForPhase1 = null;
        if (hint) {
          hint.style.display = 'block';
          hint.textContent =
            'Phase 2 failed to start: ' + (err && err.message ? String(err.message).slice(0, 220) : String(err));
        }
      });
  }

  /** Enable Phase 2 button when Phase 1 job exists and TEXT 1–3 are all non-empty. */
  function updateGenerateVOButtonState() {
    var btn = document.getElementById('btnGenerateVO');
    if (!btn) return;
    if (btn.getAttribute('data-studio-vo-busy') === '1') return;
    var hasP1 = !!(phase1JobId && String(phase1JobId).trim().length >= 8);
    var textsOk = step6AllPreferenceTextsFilled();
    var isUgc = isUgcRealFlow();
    var ugcCells = isUgc && ugcRealPlanHasCells();
    var p1Dead = isUgc && _phase1LastPolledStatus === 'failed';
    var ready = hasP1 && textsOk && !p1Dead;
    btn.disabled = !ready;
    if (p1Dead) {
      btn.textContent = 'Job failed — start new';
      btn.title =
        'The Phase 1 job failed. Go back to step 5 and click Start generation to create a new job, or check the API Log for the error.';
    } else {
      btn.textContent = isUgc ? (ugcCells ? 'Continue to Phase 2' : 'Approve offer text & continue') : 'Generate VO';
      if (!hasP1) {
        btn.title = isUgc
          ? 'Start generation from step 5 first. Phase 1 must fill the three offer fields (or type them yourself), then continue.'
          : 'Start generation from step 5 first (Phase 1). TEXT 1–3 must appear or be typed here before Generate VO.';
      } else if (!textsOk) {
        btn.title = isUgc
          ? 'Wait for Target audience, Main problem, and Key benefits & CTA (or type all three), then continue.'
          : 'Wait for Headline, Key message, and Call to action to fill in (or type all three yourself), then click Generate VO.';
      } else {
        btn.title = isUgc
          ? ugcCells
            ? 'Resume the same job: saves edits, then continues toward the 3×3 master grid (after storyboard approval when required).'
            : 'Saves the three offer fields and resumes the job. The server uses your main prompt (step 3) plus these fields to run offer analysis → creative strategy → nine-cell plan (per-cell image prompt + VO line), then pauses for storyboard review.'
          : 'Start Phase 2: generate the voiceover script from the three texts below.';
      }
    }
  }

  /** Product Studio: job parsed_texts or a filled TEXT 1 field is enough for Suno music. */
  function studioHasTextsForMusic(imParsedTexts) {
    if (parsedTextsHaveBody(imParsedTexts)) return true;
    var t1 = document.getElementById('text1');
    return !!(t1 && t1.value && t1.value.trim().length > 10);
  }

  function updateStep6MusicPrefetchHint(message) {
    var h = document.getElementById('step6MusicPrefetchHint');
    if (!h) return;
    if (!message) {
      h.style.display = 'none';
      h.textContent = '';
      return;
    }
    h.style.display = 'block';
    h.textContent = message;
  }

  /**
   * Preferences (step 6): show a banner immediately so users know Phase 1 LLM work is in progress
   * (not blocked on optional /api/generate-character portrait prefetch).
   */
  function primeStep6PreferencesBanner() {
    var step6Wrap = document.getElementById('step6LiveStatus');
    var step6Text = document.getElementById('step6LiveStatusText');
    var step6Step = document.getElementById('step6LiveStatusStep');
    if (!step6Wrap || !step6Text || !step6Step || currentStep !== studioPrefsStep() || !phase1JobId) return;
    step6Wrap.style.display = 'block';
    step6Wrap.classList.add('studio-step7-running');
    step6Wrap.classList.remove('studio-status-success', 'studio-status-error');
    if (isUgcRealFlow()) {
      step6Text.textContent =
        'UGC Real Phase 1 parses your prompt into the three offer fields first. Approve them with the primary button, then use Next to open the Nine-cell storyboard step when the plan is ready.';
      step6Step.textContent =
        'This page refreshes every ~2s. In the API log, the first step is step_parse (brief parse — not product parse_prompt). Optional portrait errors do not block these fields.';
    } else {
      step6Text.textContent =
        'Phase 1 is running or finishing. TEXT 1–3 are filled by the parse-prompt step (LLM on the server) after the job reaches that step — typically ~15–45s of model time once you see status “processing”.';
      step6Step.textContent =
        'Fetching job status… Open API Log below: look for PIPE lines and step parse_prompt for your Phase 1 job. Portrait POST /api/generate-character errors do not block these texts.';
    }
    step6Step.style.display = 'block';
  }

  /** Merge music into Phase 1 and Phase 2 rows when both exist (avoids races with Generate VO). */
  function patchStudioMusicIntermediates(data) {
    var payload = {
      intermediates: {
        music_url: data.music_url || collectedIntermediates.music_url,
        music_description: data.music_description || collectedIntermediates.music_description
      }
    };
    var tasks = [];
    if (phase1JobId && String(phase1JobId).trim().length >= 8) {
      tasks.push(
        StudioAPI.patchIntermediates(String(phase1JobId).trim(), payload, { skipWaitingOverlay: true })
      );
    }
    if (phase2JobId && String(phase2JobId).trim().length >= 8) {
      tasks.push(
        StudioAPI.patchIntermediates(String(phase2JobId).trim(), payload, { skipWaitingOverlay: true })
      );
    }
    if (!tasks.length) return Promise.resolve();
    return Promise.all(tasks);
  }

  function applyMusicGenerateResult(data) {
    if (data.music_url) {
      collectedIntermediates.music_url = data.music_url;
      collectedIntermediates.music_description = data.music_description || '';
    }
    if (data.music_description) {
      var md = document.getElementById('musicDescription');
      if (md) md.value = data.music_description;
    }
    if (data.music_url) {
      var audioEl = document.getElementById('musicAudio');
      if (audioEl) {
        audioEl.src = data.music_url;
        audioEl.load();
      }
      var mp = document.getElementById('musicPlayer');
      if (mp) mp.style.display = 'flex';
      var ms = document.getElementById('musicStatus');
      if (ms) ms.textContent = 'Music ready.';
      if (currentStep === studioPrefsStep()) {
        updateStep6MusicPrefetchHint(
          'Background music is ready. Use Next to open the Background music step to preview and approve.'
        );
      }
    }
  }

  function phase3JobForAnimate() {
    var b = phase3JobId;
    var a = _phase3AnimateJobId;
    if (b && String(b).length >= 8) return String(b).trim();
    if (a && String(a).length >= 8) return String(a).trim();
    return null;
  }

  /** Ordered list of job ids to try for Animate. phase3JobId first — stale _phase3AnimateJobId caused 404 when tried first. */
  function phase3JobCandidatesForAnimate() {
    var cands = [];
    function add(x) {
      if (!x) return;
      var s = String(x).trim();
      if (s.length < 8) return;
      if (cands.indexOf(s) === -1) cands.push(s);
    }
    add(phase3JobId);
    var cid = currentJobId && String(currentJobId).trim();
    if (cid && (cid === String(phase3JobId || '') || cid === String(_phase3AnimateJobId || ''))) {
      add(cid);
    }
    add(_phase3AnimateJobId);
    return cands;
  }

  /**
   * Monolith product pipeline uses image_prompt + motion_prompt; UGC paths often use first_prompt + second_prompt.
   * Studio renders/edits first_prompt / second_prompt only — copy monolith keys so scene cards are not empty.
   */
  function normalizeStudioScenePrompts(arr) {
    if (!arr || !Array.isArray(arr)) return arr;
    return arr.map(function (scene) {
      if (!scene || typeof scene !== 'object') return scene;
      var o = Object.assign({}, scene);
      var fp = o.first_prompt != null && String(o.first_prompt).trim() ? String(o.first_prompt) : '';
      var ip = o.image_prompt != null && String(o.image_prompt).trim() ? String(o.image_prompt) : '';
      o.first_prompt = fp || ip;
      var sp = o.second_prompt != null && String(o.second_prompt).trim() ? String(o.second_prompt) : '';
      var mp = o.motion_prompt != null && String(o.motion_prompt).trim() ? String(o.motion_prompt) : '';
      o.second_prompt = sp || mp;
      // Product monolith reads image_prompt / motion_prompt; keep keys in sync when patching/resuming.
      o.image_prompt = o.first_prompt;
      o.motion_prompt = o.second_prompt;
      return o;
    });
  }

  function buildUgcRealGridPrompt(scene, manifest, index) {
    scene = scene || {};
    manifest = manifest || {};
    var cells = Array.isArray(manifest.cells) ? manifest.cells : [];
    var lines = cells.map(function (cell) {
      return (
        '- Cell ' +
        (cell.cell_index || '?') +
        ': ' +
        (cell.description || '') +
        ' (shot_type=' +
        (cell.shot_type || 'unknown') +
        ', framing=' +
        (cell.framing || 'auto') +
        ', emotion=' +
        (cell.emotion || 'natural') +
        ')'
      );
    });
    return (
      'Create a 3x3 storyboard grid for UGC ad scene ' + (index + 1) + '.\n' +
      'Scene purpose: ' + (scene.purpose || 'benefit') + '\n' +
      'Primary message: ' + (scene.primary_message || '') + '\n' +
      'Voice line: ' + (scene.voice_line || '') + '\n' +
      'Continuity anchor: ' + (scene.continuity_anchor || scene.location || 'ugc_world') + '\n' +
      'Shot family: ' + (scene.shot_family || scene.purpose || 'ugc_story') + '\n' +
      'Keep the same character identity, outfit baseline, and world continuity across all cells. Natural UGC realism. Strong variety in camera angle, framing, and expression without drifting identity.\n' +
      lines.join('\n')
    );
  }

  function updateUgcRealStepVisibility() {
    var ugc = isUgcRealFlow();
    ['step10assets', 'step11prompts', 'step12images'].forEach(function (sid) {
      var node = document.getElementById(sid);
      if (node) node.style.display = ugc ? 'none' : '';
    });
    var charGate = document.getElementById('step6ugcCharacterGate');
    if (charGate) charGate.style.display = ugc ? '' : 'none';
    var nonUgcCharGate = document.getElementById('stepNonUgcCharacterGate');
    if (nonUgcCharGate) nonUgcCharGate.style.display = ugc ? 'none' : '';
    var nineSec = document.getElementById('step6ugcNineCell');
    if (nineSec) nineSec.style.display = ugc ? '' : 'none';
    var sceneReview = document.getElementById('ugcRealSceneReview');
    var gridReview = document.getElementById('ugcRealGridReview');
    var step6MusicHint = document.getElementById('step6MusicPrefetchHint');
    var step6PortraitHint = document.getElementById('step6PortraitPrefetchHint');
    var step6Preview = document.getElementById('step6CharPreview');
    var step6Live = document.getElementById('step6LiveStatus');
    var text1 = document.getElementById('text1');
    var text2 = document.getElementById('text2');
    var text3 = document.getElementById('text3');
    var btnVo = document.getElementById('btnGenerateVO');
    var step7Live = document.getElementById('step7LiveStatus');
    var step7VoiceBlock = document.getElementById('step7VoiceBlock');
    var step7Approve = document.getElementById('btnStep7ApproveContinue');
    var voWrap = document.getElementById('voAudioPlayerWrap');
    var offerHint = document.getElementById('step6UgcOfferFlowHint');
    if (sceneReview) {
      if (ugc) {
        /* Visibility is set by job polling (storyboard card only when there is a plan or pipeline status). */
      } else {
        sceneReview.style.display = 'none';
      }
    }
    if (gridReview) {
      if (!ugc) gridReview.style.display = 'none';
      else
        gridReview.style.display =
          typeof currentStep === 'number' && currentStep === studioVoStep() ? 'block' : 'none';
    }
    if (offerHint) {
      if (ugc) {
        offerHint.style.display = 'block';
        offerHint.textContent =
          'Edit the three offer fields on this step, then Approve offer text & continue (or use Next to the character step). On the character step, the nine-cell storyboard runs in the background; when it is ready, use Next to open the Nine-cell storyboard. Continue there runs the 3×3 master grid; VO and grid review follow.';
      } else {
        offerHint.style.display = 'none';
        offerHint.textContent = '';
      }
    }
    [step6MusicHint, step6PortraitHint].forEach(function (el) {
      if (!el) return;
      el.style.display = ugc ? 'none' : '';
    });
    if (step6Preview) {
      /* Non–UGC: preview on character gate (influencer step 7 or product step 12). */
      var onNonUgcChar =
        !ugc &&
        typeof currentStep === 'number' &&
        ((studioNonUgcCharacterStep() != null && currentStep === studioNonUgcCharacterStep()) ||
          (studioProductCharacterGateStep() != null && currentStep === studioProductCharacterGateStep()));
      if (ugc || !onNonUgcChar) {
        step6Preview.style.display = 'none';
      }
    }
    function setPrefsTextLabel(textareaId, labelText) {
      var ta = document.getElementById(textareaId);
      if (ta && ta.previousElementSibling && ta.previousElementSibling.classList.contains('studio-label')) {
        ta.previousElementSibling.textContent = labelText;
      }
    }
    if (ugc) {
      var ugcCardTitle = document.getElementById('ugcRealSceneReviewTitle');
      var ugcCardHint = document.getElementById('ugcRealSceneReviewHint');
      var btnRegen = document.getElementById('btnUgcRealRestartScenePlan');
      if (ugcCardTitle) ugcCardTitle.textContent = 'Nine-cell storyboard';
      if (ugcCardHint) {
        ugcCardHint.textContent =
          'Built from your main prompt (step 3) and the three offer fields from the previous step. Each cell lists the image prompt and VO line. If you change those fields after this plan exists, go back and use Regenerate nine-cell plan, or regenerate from here. When ready, use Continue to Phase 2 (3×3 grid).';
      }
      if (btnRegen) btnRegen.textContent = 'Regenerate nine-cell plan';
      setPrefsTextLabel('text1', 'Target audience');
      setPrefsTextLabel('text2', 'Main problem solved');
      setPrefsTextLabel('text3', 'Key benefits & CTA');
      var phUgc =
        'Filled automatically from your prompt after Phase 1 parse; you can edit before Phase 2.';
      var t1 = document.getElementById('text1');
      var t2 = document.getElementById('text2');
      var t3 = document.getElementById('text3');
      if (t1) t1.placeholder = phUgc;
      if (t2) t2.placeholder = phUgc;
      if (t3) t3.placeholder = phUgc;
    } else {
      var ugcCardTitleD = document.getElementById('ugcRealSceneReviewTitle');
      var ugcCardHintD = document.getElementById('ugcRealSceneReviewHint');
      var btnRegenD = document.getElementById('btnUgcRealRestartScenePlan');
      if (ugcCardTitleD) ugcCardTitleD.textContent = 'Planned scenes';
      if (ugcCardHintD) {
        ugcCardHintD.textContent =
          'Populates after Phase 1 finishes scene planning. Review hooks, scene purposes, and voice lines before you continue to grids.';
      }
      if (btnRegenD) btnRegenD.textContent = 'Regenerate scene plan';
      setPrefsTextLabel('text1', 'Headline / Hook (TEXT 1)');
      setPrefsTextLabel('text2', 'Key message / Body (TEXT 2)');
      setPrefsTextLabel('text3', 'Call to action / Closing (TEXT 3)');
      var t1d = document.getElementById('text1');
      var t2d = document.getElementById('text2');
      var t3d = document.getElementById('text3');
      var phDef = 'Generated from your prompt; will appear after you start generation';
      if (t1d) t1d.placeholder = phDef;
      if (t2d) t2d.placeholder = phDef;
      if (t3d) t3d.placeholder = phDef;
    }
    [text1 && text1.parentElement, text2 && text2.parentElement, text3 && text3.parentElement, btnVo && btnVo.parentElement].forEach(function (el) {
      if (!el) return;
      el.style.display = '';
    });
    /* UGC Real: VO + grid review live on the same DOM section as non-UGC VO — keep voice block, job status, and audio visible on the VO step. */
    var onUgcVoStep = ugc && typeof currentStep === 'number' && currentStep === studioVoStep();
    [step7Live, step7VoiceBlock, voWrap].forEach(function (el) {
      if (!el) return;
      if (onUgcVoStep) {
        el.style.display = '';
        return;
      }
      el.style.display = ugc ? 'none' : '';
    });
    if (step7Approve) {
      if (ugc && !onUgcVoStep) step7Approve.style.display = 'none';
      else step7Approve.style.display = '';
    }
    var step12imgs = document.getElementById('step12images');
    if (step12imgs) {
      var sceneImgDesc = step12imgs.querySelector('.studio-step-desc');
      if (sceneImgDesc) {
        if (_studioProductSceneImagesDescHtml == null && !ugc) {
          _studioProductSceneImagesDescHtml = sceneImgDesc.innerHTML;
        }
        if (ugc) {
          sceneImgDesc.innerHTML =
            'For <strong>UGC Real</strong>, stills are usually the <strong>nine grid images</strong> from the server (your API calls providers — the browser does not hit kie.ai). Slots fill from job <code>grid_cells</code> while this page polls. If you return from Final and tiles look empty, wait for the next poll; odd 0/1 counts often meant the wrong job id was polled (fixed: Phase 1 is preferred). Use <strong>Generate all scene images</strong> only for explicit per-slot regeneration; then <strong>Animate all</strong>.';
        } else {
          sceneImgDesc.innerHTML = _studioProductSceneImagesDescHtml || STUDIO_PRODUCT_SCENE_IMAGES_DESC_HTML;
        }
      }
    }
    try {
      updateGenerateVOButtonState();
    } catch (eVoLbl) {}
  }

  function renderUgcRealSceneReview(planData, creativeStrategy, narrativePlan, reviewCtx) {
    reviewCtx = reviewCtx || {};
    var wrap = document.getElementById('ugcRealSceneReview');
    var sum = document.getElementById('ugcRealSceneReviewSummary');
    var list = document.getElementById('ugcRealSceneReviewList');
    var chrome = document.getElementById('ugcRealSceneReviewStoryboardChrome');
    if (!wrap || !sum || !list) return;
    var showCard = !!reviewCtx.showCard;
    var normalizedPlan = ugcRealNormalizeStoryboardPlan(planData);
    var cells = normalizedPlan && Array.isArray(normalizedPlan.cells) ? normalizedPlan.cells : null;
    if (!isUgcRealFlow()) {
      wrap.style.display = 'none';
      if (chrome) chrome.style.display = 'none';
      return;
    }
    if (!showCard) {
      wrap.style.display = 'none';
      if (chrome) chrome.style.display = 'none';
      return;
    }
    var hasCells = !!(cells && cells.length);
    if (!hasCells) {
      wrap.style.display = 'none';
      if (chrome) chrome.style.display = 'none';
      sum.innerHTML = '';
      list.innerHTML = '';
      return;
    }
    wrap.style.display = 'block';
    if (chrome) chrome.style.display = '';
    sum.innerHTML = '';
    var hookLine =
      cells[0] &&
      (ugcRealCellVoiceLine(cells[0], null) || ugcRealCellVisualPrompt(cells[0]));
    [
      { label: 'Cells', value: cells.length },
      { label: 'Hook', value: hookLine || '—' },
      { label: 'Creative angle', value: creativeStrategy && creativeStrategy.creative_angle ? creativeStrategy.creative_angle : '—' }
    ].forEach(function (item) {
      var card = document.createElement('div');
      card.className = 'studio-ugc-review-card';
      card.innerHTML =
        '<div class="studio-ugc-review-kv"><strong>' +
        ugcEscHtml(item.label) +
        ':</strong> ' +
        ugcDisplayText(item.value) +
        '</div>';
      sum.appendChild(card);
    });
    list.innerHTML = '';
    cells.forEach(function (cell, index) {
      var card = document.createElement('div');
      card.className = 'studio-ugc-review-card';
      var role = cell.shot_role || cell.purpose || 'scene';
      var lip = cell.lipsync != null ? cell.lipsync : cell.speaking_required;
      var imgPrompt = ugcRealCellVisualPrompt(cell);
      var voScript = ugcRealCellVoiceLine(cell, null);
      card.innerHTML =
        '<h4>Grid cell ' +
        (index + 1) +
        ' · ' +
        ugcEscHtml(role) +
        '</h4>' +
        '<div class="studio-ugc-block">' +
        '<div class="studio-ugc-block-label">Image generation prompt</div>' +
        '<div class="studio-ugc-block-body studio-ugc-block-prompt">' +
        ugcDisplayText(imgPrompt) +
        '</div></div>' +
        '<div class="studio-ugc-block studio-ugc-block-vo">' +
        '<div class="studio-ugc-block-label">VO script</div>' +
        '<div class="studio-ugc-block-body studio-ugc-block-vo-text">' +
        ugcDisplayText(voScript) +
        '</div></div>' +
        '<div class="studio-ugc-review-meta">' +
        '<div class="studio-ugc-review-kv"><strong>Lip-sync:</strong> ' +
        (lip ? 'Yes (Kling Avatar)' : 'No (I2V animation)') +
        '</div>' +
        '<div class="studio-ugc-review-kv"><strong>Duration:</strong> ' +
        ugcEscHtml(cell.duration_seconds != null ? String(cell.duration_seconds) : '—') +
        's</div>' +
        '</div>';
      list.appendChild(card);
    });
  }

  /** Live UX on Nine-cell step: spinner, job progress, pipeline chips (Phase 1 is working). */
  function refreshUgcNineCellActivityPanel(jobRef, im, hasNineCellPlan) {
    var act = document.getElementById('ugcNineCellActivity');
    if (!act) return;
    if (!isUgcRealFlow() || currentStep !== studioUgcNineStep()) {
      act.style.display = 'none';
      return;
    }
    act.style.display = 'block';
    var titleEl = document.getElementById('ugcNineCellActivityTitle');
    var subEl = document.getElementById('ugcNineCellActivitySub');
    var hintEl = document.getElementById('ugcNineCellActivityHint');
    var track = document.getElementById('ugcNineCellProgressTrack');
    var fill = document.getElementById('ugcNineCellProgressFill');
    var spin = document.getElementById('ugcNineCellSpinner');
    var mini = document.getElementById('ugcNineCellPipelineMini');
    var jid = ugcRealPrimaryPipelineJobId();
    function setSpinner(on) {
      if (!spin) return;
      if (on) {
        spin.classList.remove('studio-spinner-off');
        spin.style.display = '';
      } else {
        spin.classList.add('studio-spinner-off');
        spin.style.display = 'none';
      }
    }
    var planReady = !!hasNineCellPlan;
    if (im && !planReady) planReady = !!ugcRealGetStoryboardPlan(im);
    var chips = [
      { id: 'step_parse', short: 'Brief' },
      { id: 'step_0', short: 'Offer' },
      { id: 'step_0.5', short: 'Strategy' },
      { id: 'step_1', short: '9 cells' },
      { id: 'step_2', short: 'Style DNA' }
    ];
    var order = ['step_parse', 'step_0', 'step_0.5', 'step_1', 'step_2'];
    function rankStep(id) {
      var x = order.indexOf(id);
      return x >= 0 ? x : -1;
    }
    function chipClass(chipId, cs, st, ready) {
      var ri = rankStep(cs);
      var rj = rankStep(chipId);
      if (rj < 0) return '';
      if (ready) {
        if (rj <= rankStep('step_1')) return 'studio-pipeline-done';
        if (chipId === cs && (st === 'processing' || st === 'paused')) return 'studio-pipeline-active';
        return '';
      }
      if (ri < 0) return st === 'processing' || st === 'paused' ? '' : '';
      if (rj < ri) return 'studio-pipeline-done';
      if (chipId === cs) return 'studio-pipeline-active';
      return '';
    }
    if (String(jid || '') !== String(_ugcNineCellPanelJobId || '')) {
      _ugcNineCellPanelJobId = jid || null;
      _ugcNineCellLastPollSig = '';
      _ugcNineCellFlatPollCount = 0;
    }
    if (!jid) {
      setSpinner(false);
      if (titleEl) titleEl.textContent = 'No Phase 1 job linked yet';
      if (subEl) subEl.textContent = 'Go back to step 5 (Models & voice), start generation, finish Preferences and character, then open this step again.';
      if (hintEl) hintEl.textContent = '';
      if (track) {
        track.style.display = 'none';
        track.classList.remove('studio-progress-activity');
        track.title = '';
      }
      if (mini) mini.innerHTML = '';
      return;
    }
    var job = jobRef && typeof jobRef === 'object' ? jobRef : {};
    var st = String(job.status || '').trim() || '';
    var cs = String(job.current_step || '').trim();
    var progRaw = job.progress;
    var prog =
      typeof progRaw === 'number' && isFinite(progRaw) ? Math.max(0, Math.min(100, Math.round(progRaw))) : null;
    var stepHuman = {
      step_parse: 'Parsing your brief into the three offer fields',
      step_0: 'Analyzing the offer',
      'step_0.5': 'Creative strategy (hook & angle)',
      step_1: 'Writing the nine-cell storyboard (9 prompts + VO lines)',
      step_2: 'Style DNA for a consistent grid look',
      step_3: 'Generating the 3×3 master image',
      step_4: 'Cutting the grid into cells',
      step_5: 'Routing cells (lip-sync vs animation)'
    };
    if (mini) {
      mini.innerHTML = '';
      for (var ci = 0; ci < chips.length; ci++) {
        var ch = chips[ci];
        var li = document.createElement('li');
        li.textContent = ch.short;
        li.className = chipClass(ch.id, cs, st, planReady);
        li.title = stepHuman[ch.id] || ch.id;
        mini.appendChild(li);
      }
    }
    if (!st && !cs) {
      setSpinner(true);
      if (titleEl) titleEl.textContent = 'Syncing job ' + String(jid).slice(0, 8) + '…';
      if (subEl) {
        subEl.textContent =
          'Waiting for job status from the server. Polling runs about every 2 seconds. If this message stays more than ~30s, open API Log and check for failed GET /api/jobs, 401, or wrong API base URL.';
      }
      if (hintEl) {
        hintEl.textContent =
          'Open API Log below and filter PIPE to see pipeline steps. A stuck screen usually means the job poll failed (network, auth, or job id).';
      }
      if (track) {
        track.style.display = 'none';
        track.classList.remove('studio-progress-activity');
        track.title = '';
      }
      return;
    }
    if (planReady) {
      setSpinner(st === 'processing' && rankStep(cs) >= rankStep('step_2'));
      if (titleEl) titleEl.textContent = 'Nine-cell plan is ready';
      if (subEl) {
        subEl.textContent =
          st === 'processing' && rankStep(cs) >= rankStep('step_2')
            ? 'The server is still running later steps (e.g. Style DNA / grid) in the background. Your 9 cells are below — review them and use Continue to Phase 2 when ready.'
            : 'Review each cell in the card below, then use Continue to Phase 2 (3×3 master grid) when you are happy.';
      }
      if (hintEl) {
        hintEl.textContent =
          'Job: ' +
          String(jid).slice(0, 8) +
          '… · status: ' +
          (st || '—') +
          (cs ? ' · step: ' + cs : '') +
          (prog != null ? ' · progress: ' + prog + '%' : '');
      }
      if (track && fill) {
        track.style.display = prog != null ? 'block' : 'none';
        if (prog != null) fill.style.width = prog + '%';
        track.classList.remove('studio-progress-activity');
        track.title = '';
      }
      return;
    }
    setSpinner(st === 'processing');
    var pollSig = st + '|' + cs + '|' + (prog != null ? String(prog) : '—');
    if (st === 'processing' && pollSig === _ugcNineCellLastPollSig) {
      _ugcNineCellFlatPollCount++;
    } else {
      _ugcNineCellFlatPollCount = 0;
      _ugcNineCellLastPollSig = pollSig;
    }
    var rCs = rankStep(cs);
    var earlyPhase1Busy =
      st === 'processing' &&
      !planReady &&
      (rCs < 0 || rCs <= rankStep('step_2'));
    if (titleEl) {
      titleEl.textContent =
        st === 'processing'
          ? 'Phase 1 is running — ' + (stepHuman[cs] || 'working on: ' + (cs || 'pipeline'))
          : st === 'paused'
            ? 'Job is paused — ' + (stepHuman[cs] || cs || 'waiting')
            : 'Job status: ' + (st || 'unknown');
    }
    if (subEl) {
      if (st === 'processing') {
        var explainFlat =
          _ugcNineCellFlatPollCount >= 2 || (prog != null && prog <= 12)
            ? ' Server progress % jumps when a whole pipeline step finishes, not during a single LLM call — the bar can sit at the same value for 1–2 minutes while the model works. Open API Log and filter PIPE to see live activity.'
            : '';
        subEl.textContent =
          'LLM steps often take ~30–90 seconds each; the nine-cell plan step can take around 1–3 minutes. Nothing is wrong if this screen updates in chunks.' +
          explainFlat;
      } else if (st === 'paused' && (cs === 'step_2' || cs === 'step_parse')) {
        subEl.textContent =
          'The pipeline is waiting for you on an earlier screen (e.g. approve offer text or character). Go back, complete that step, then return here — cells will fill when step_1 finishes.';
      } else {
        subEl.textContent = 'When the nine-cell plan is saved, the storyboard card below will populate automatically.';
      }
    }
    if (hintEl) {
      var uiClock = new Date().toLocaleTimeString(undefined, {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit'
      });
      hintEl.textContent =
        'Job: ' +
        String(jid).slice(0, 8) +
        '… · polling ~2s · open API Log (PIPE) for live step lines' +
        (prog != null ? ' · progress: ' + prog + '%' : '') +
        ' · last poll ' +
        uiClock;
    }
    if (track && fill) {
      if (prog != null) {
        track.style.display = 'block';
        fill.style.width = prog + '%';
        if (earlyPhase1Busy) {
          track.classList.add('studio-progress-activity');
          track.title =
            'This value is coarse: it advances when each server step completes. Motion on the bar means the page is still polling — work may be running even if % is unchanged.';
        } else {
          track.classList.remove('studio-progress-activity');
          track.title = '';
        }
      } else {
        track.style.display = 'none';
        track.classList.remove('studio-progress-activity');
        track.title = '';
      }
    }
  }

  /** After Approve grids & continue: server runs step_6 (VO audio) → step_7 (lip-sync) → step_8 (nine I2V clips). */
  function refreshUgcPostGridPipelinePanel(jref) {
    var act = document.getElementById('ugcPostGridPipelineActivity');
    if (!act) return;
    if (!isUgcRealFlow()) {
      act.style.display = 'none';
      return;
    }
    if (currentStep < studioVoStep() || currentStep > studioFinalStep()) {
      act.style.display = 'none';
      return;
    }
    var jid = ugcRealPrimaryPipelineJobId();
    if (!jid) {
      act.style.display = 'none';
      return;
    }
    function spinOn(el, on) {
      if (!el) return;
      if (on) {
        el.classList.remove('studio-spinner-off');
        el.style.display = '';
      } else {
        el.classList.add('studio-spinner-off');
        el.style.display = 'none';
      }
    }
    if (!jref || typeof jref !== 'object') {
      if (_ugcRealPostGridResumePending && currentStep >= studioMusicStep()) {
        act.style.display = 'block';
        spinOn(document.getElementById('ugcPostGridSpinner'), true);
        var t0 = document.getElementById('ugcPostGridPipelineActivityTitle');
        var s0 = document.getElementById('ugcPostGridPipelineActivitySub');
        var h0 = document.getElementById('ugcPostGridPipelineActivityHint');
        var m0 = document.getElementById('ugcPostGridPipelineMini');
        if (t0) t0.textContent = 'Resume sent — waiting for the server to start the next segment';
        if (s0)
          s0.textContent =
            'The pipeline will generate per-cell voice audio, lip-sync clips, and animated video for all nine grid cells. Still frames from your master grid sync to Scene images when job data updates.';
        if (h0) h0.textContent = 'Polling every ~2s. Open API Log (PIPE) for step_6 → step_8.';
        if (m0) m0.innerHTML = '';
      } else {
        act.style.display = 'none';
      }
      return;
    }
    if (String(jref.id || '').trim() !== String(jid)) {
      act.style.display = 'none';
      return;
    }
    var st = String(jref.status || '').trim();
    var cs = String(jref.current_step || '').trim();
    var postSteps = { step_6: 1, step_7: 1, step_8: 1 };
    var preSix = [
      'step_parse',
      'step_0',
      'step_0.5',
      'step_1',
      'step_2',
      'step_3',
      'step_4'
    ];
    if (st === 'processing' && cs && preSix.indexOf(cs) >= 0) {
      _ugcRealPostGridResumePending = false;
      act.style.display = 'none';
      return;
    }
    if (st === 'processing' && postSteps[cs]) {
      _ugcRealPostGridResumePending = false;
    }
    if ((st === 'paused' && cs === 'step_8') || st === 'failed' || st === 'aborted' || st === 'completed') {
      _ugcRealPostGridResumePending = false;
      act.style.display = 'none';
      return;
    }
    var show =
      (st === 'processing' && (postSteps[cs] || (_ugcRealPostGridResumePending && (cs === 'step_5' || !cs)))) ||
      (st === 'queued' && _ugcRealPostGridResumePending) ||
      (_ugcRealPostGridResumePending && st === 'processing' && !cs);
    if (!show) {
      act.style.display = 'none';
      return;
    }
    act.style.display = 'block';
    var titleEl = document.getElementById('ugcPostGridPipelineActivityTitle');
    var subEl = document.getElementById('ugcPostGridPipelineActivitySub');
    var hintEl = document.getElementById('ugcPostGridPipelineActivityHint');
    var track = document.getElementById('ugcPostGridProgressTrack');
    var fill = document.getElementById('ugcPostGridProgressFill');
    var spin = document.getElementById('ugcPostGridSpinner');
    var mini = document.getElementById('ugcPostGridPipelineMini');
    spinOn(spin, st === 'processing' || st === 'queued' || _ugcRealPostGridResumePending);
    var progRaw = jref.progress;
    var prog =
      typeof progRaw === 'number' && isFinite(progRaw) ? Math.max(0, Math.min(100, Math.round(progRaw))) : null;
    var stepHuman = {
      step_6: 'Per-cell voice audio (ElevenLabs)',
      step_7: 'Lip-sync video (Kling Avatar Pro)',
      step_8: 'Animated clips for all nine grid cells'
    };
    var chips = [
      { id: 'step_6', short: 'VO audio' },
      { id: 'step_7', short: 'Lip-sync' },
      { id: 'step_8', short: 'Animations' }
    ];
    function rankPost(id) {
      var order = ['step_6', 'step_7', 'step_8'];
      var x = order.indexOf(id);
      return x >= 0 ? x : -1;
    }
    function chipClass(chipId, curStep, jobSt) {
      var ri = rankPost(curStep);
      var rj = rankPost(chipId);
      if (rj < 0) return '';
      if (ri < 0) return jobSt === 'processing' || jobSt === 'queued' ? '' : '';
      if (rj < ri) return 'studio-pipeline-done';
      if (chipId === curStep) return 'studio-pipeline-active';
      return '';
    }
    if (mini) {
      mini.innerHTML = '';
      for (var ci = 0; ci < chips.length; ci++) {
        var ch = chips[ci];
        var li = document.createElement('li');
        li.textContent = ch.short;
        li.className = chipClass(ch.id, cs, st);
        li.title = stepHuman[ch.id] || ch.id;
        mini.appendChild(li);
      }
    }
    if (titleEl) {
      if (!cs && (_ugcRealPostGridResumePending || st === 'queued')) {
        titleEl.textContent = 'Resume accepted — connecting to pipeline progress…';
      } else if (st === 'processing' && cs === 'step_5' && _ugcRealPostGridResumePending) {
        titleEl.textContent = 'Starting the post-grid segment…';
      } else if (st === 'processing' && postSteps[cs]) {
        titleEl.textContent = 'Generating cell media — ' + (stepHuman[cs] || cs);
      } else {
        titleEl.textContent = 'UGC Real pipeline is running';
      }
    }
    if (subEl) {
      subEl.textContent =
        'Nine grid stills appear on Scene images as URLs sync from the server. Moving clips are built in this segment (before Final video). Expect several minutes for lip-sync plus nine animations.';
    }
    if (hintEl) {
      hintEl.textContent =
        'Job ' +
        String(jid).slice(0, 8) +
        '… · status: ' +
        st +
        (cs ? ' · step: ' + cs : '') +
        (prog != null ? ' · progress: ' + prog + '%' : '') +
        ' · API Log (PIPE) shows live steps.';
    }
    if (track && fill) {
      if (prog != null) {
        track.style.display = 'block';
        fill.style.width = prog + '%';
      } else {
        track.style.display = 'none';
      }
    }
  }

  function ugcRealCellVisualPrompt(cell) {
    if (!cell || typeof cell !== 'object') return '';
    return (
      cell.visual_prompt ||
      cell.image_prompt ||
      cell.scene_image_prompt ||
      cell.scene_prompt ||
      cell.primary_message ||
      ''
    );
  }

  function ugcRealCellVoiceLine(cell, rt) {
    if (!cell || typeof cell !== 'object') cell = {};
    return cell.voice_line || cell.vo_line || cell.script_line || (rt && rt.voice_line) || '';
  }

  function renderUgcRealGridReview(planData, gridImageUrl, gridCells, frameRouting) {
    var wrap = document.getElementById('ugcRealGridReview');
    var list = document.getElementById('ugcRealGridReviewList');
    if (!wrap || !list) return;
    if (!isUgcRealFlow()) {
      wrap.style.display = 'none';
      return;
    }
    if (typeof currentStep === 'number' && currentStep !== studioVoStep()) {
      wrap.style.display = 'none';
      return;
    }
    wrap.style.display = 'block';
    list.innerHTML = '';
    var normalizedPlan = ugcRealNormalizeStoryboardPlan(planData);
    var cells = normalizedPlan && Array.isArray(normalizedPlan.cells) ? normalizedPlan.cells : [];

    var gridUrlStr =
      typeof gridImageUrl === 'string'
        ? gridImageUrl.trim()
        : Array.isArray(gridImageUrl) && gridImageUrl.length === 1 && typeof gridImageUrl[0] === 'string'
          ? gridImageUrl[0].trim()
          : '';
    if (gridUrlStr) {
      var imgCard = document.createElement('div');
      imgCard.className = 'studio-ugc-grid-card';
      var gh = document.createElement('h4');
      gh.textContent = '3×3 Grid (single image)';
      imgCard.appendChild(gh);
      var gridImg = document.createElement('img');
      gridImg.src = gridUrlStr;
      gridImg.alt = '3×3 Grid';
      gridImg.loading = 'lazy';
      gridImg.style.maxWidth = '100%';
      gridImg.style.borderRadius = '0.5rem';
      imgCard.appendChild(gridImg);
      list.appendChild(imgCard);
    }

    var routing = Array.isArray(frameRouting) ? frameRouting : [];
    var gcArr = Array.isArray(gridCells) ? gridCells : [];
    var errs = window._sceneImageErrors && window._sceneImageErrors.length ? window._sceneImageErrors : [];
    for (var ci = 0; ci < 9; ci++) {
      var cell = cells[ci] || {};
      var gc = ugcRealFindGridCellForIndex(gcArr, ci + 1) || {};
      var rt = routing[ci] || routing.find(function (r) { return (r.cell_index || 0) - 1 === ci; }) || {};
      var lip = cell.lipsync || rt.lipsync;
      var route = rt.route || (lip ? 'kling_avatar' : 'i2v_animation');
      var roleG = cell.shot_role || rt.shot_role || 'scene';
      var imgPromptG = ugcRealCellVisualPrompt(cell) || ugcRealCellVisualPrompt(gc);
      var voG = ugcRealCellVoiceLine(cell, rt) || ugcRealCellVoiceLine(gc, rt);
      var imgUrl = gc.image_url && typeof gc.image_url === 'string' && gc.image_url.indexOf('http') === 0 ? gc.image_url : '';
      var isFailed = errs[ci] && typeof errs[ci] === 'string';

      var card = document.createElement('div');
      card.className = 'studio-media-card studio-ugc-grid-card';

      var h4 = document.createElement('h4');
      h4.textContent = 'Grid cell ' + (ci + 1) + ' · ' + String(roleG || 'scene');
      card.appendChild(h4);

      if (imgUrl) {
        var imgEl = document.createElement('img');
        imgEl.src = imgUrl;
        imgEl.alt = 'Cell ' + (ci + 1);
        imgEl.loading = 'lazy';
        imgEl.style.maxWidth = '100%';
        imgEl.style.borderRadius = '0.5rem';
        card.appendChild(imgEl);
      } else if (isFailed) {
        var failPh = document.createElement('div');
        failPh.className = 'studio-scene-image-placeholder studio-scene-image-error';
        var em = String(errs[ci]);
        failPh.textContent = em.length > 220 ? em.slice(0, 217) + '…' : em;
        card.appendChild(failPh);
      } else {
        var placeholder = document.createElement('div');
        placeholder.className = 'studio-scene-image-placeholder';
        placeholder.textContent = 'No cell image yet — wait for the pipeline or use Regenerate.';
        card.appendChild(placeholder);
      }

      var blockImg = document.createElement('div');
      blockImg.className = 'studio-ugc-block';
      blockImg.innerHTML =
        '<div class="studio-ugc-block-label">Image generation prompt</div>' +
        '<div class="studio-ugc-block-body studio-ugc-block-prompt">' +
        ugcDisplayText(imgPromptG) +
        '</div>';
      card.appendChild(blockImg);

      var blockVo = document.createElement('div');
      blockVo.className = 'studio-ugc-block studio-ugc-block-vo';
      blockVo.innerHTML =
        '<div class="studio-ugc-block-label">VO script</div>' +
        '<div class="studio-ugc-block-body studio-ugc-block-vo-text">' +
        ugcDisplayText(voG) +
        '</div>';
      card.appendChild(blockVo);

      var kv = document.createElement('div');
      kv.className = 'studio-ugc-review-kv';
      kv.innerHTML = '<strong>Route:</strong> ' + ugcEscHtml(route);
      card.appendChild(kv);

      var input = document.createElement('input');
      input.type = 'text';
      input.className = 'studio-input';
      input.placeholder = 'Correction notes (for Fix this image)';
      card.appendChild(input);

      var btnRegen = document.createElement('button');
      btnRegen.type = 'button';
      btnRegen.className = 'studio-btn studio-btn-ghost';
      btnRegen.textContent = 'Regenerate';
      btnRegen.dataset.sceneIndex = String(ci);
      btnRegen.dataset.action = 'regen';
      card.appendChild(btnRegen);
      var btnFix = document.createElement('button');
      btnFix.type = 'button';
      btnFix.className = 'studio-btn studio-btn-ghost';
      btnFix.textContent = 'Fix this image';
      btnFix.dataset.sceneIndex = String(ci);
      btnFix.dataset.action = 'fix';
      card.appendChild(btnFix);

      list.appendChild(card);
    }
  }

  function buildSessionPayload() {
    var im = {};
    try {
      im = JSON.parse(JSON.stringify(collectedIntermediates || {}));
      if (Array.isArray(currentSceneImages) && currentSceneImages.length) im.scene_images = currentSceneImages.slice();
      var sp = window._scenePromptsForImages;
      if (sp && Array.isArray(sp) && sp.length) im.scene_prompts = sp.slice();
    } catch (e) {}
    var formSnapshot = null;
    try {
      if (typeof StudioSteps !== 'undefined' && StudioSteps.getFormAndUploadSnapshot) {
        formSnapshot = StudioSteps.getFormAndUploadSnapshot();
      }
    } catch (e) {}
    var outSnap = {};
    try {
      if (_studioLastJobOutput && typeof _studioLastJobOutput === 'object') {
        outSnap = JSON.parse(JSON.stringify(_studioLastJobOutput));
      }
    } catch (eOut) {}
    return {
      currentStep: currentStep,
      videoType: typeof StudioSteps !== 'undefined' && StudioSteps.getVideoType ? StudioSteps.getVideoType() : null,
      phase1JobId: phase1JobId,
      phase2JobId: phase2JobId,
      phase3JobId: phase3JobId,
      phase3AnimateJobId: _phase3AnimateJobId || phase3JobId,
      currentJobId: currentJobId,
      currentSceneImages: Array.isArray(currentSceneImages) ? currentSceneImages.slice() : [],
      scenePrompts: window._scenePromptsForImages && Array.isArray(window._scenePromptsForImages) ? window._scenePromptsForImages : [],
      intermediates: im,
      output: outSnap,
      formSnapshot: formSnapshot,
      savedAt: new Date().toISOString(),
      wizardStepSchema: WIZARD_STEP_SCHEMA,
      studioServerSessionId: typeof window !== 'undefined' && window._studioServerSessionId
        ? window._studioServerSessionId
        : null
    };
  }

  function scheduleCloudSessionSave() {
    if (typeof StudioAuth === 'undefined' || !StudioAuth.isAuthEnabled()) return;
    try {
      clearTimeout(_cloudSaveTimer);
    } catch (e) {}
    _cloudSaveTimer = setTimeout(function () {
      StudioAuth.getUser()
        .then(function (u) {
          if (!u) return;
          var p = buildSessionPayload();
          if (!isCountableStudioSession(p)) return;
          return StudioAuth.saveSessionToServer(p, window._studioServerSessionId, null);
        })
        .then(function (id) {
          if (id) window._studioServerSessionId = id;
        })
        .catch(function (e) {
          console.warn('Cloud session save:', e);
        });
    }, 1400);
  }

  function isSameSessionState(a, b) {
    if (!a || !b) return false;
    return a.currentStep === b.currentStep &&
      a.phase1JobId === b.phase1JobId &&
      a.phase2JobId === b.phase2JobId &&
      a.phase3JobId === b.phase3JobId;
  }

  function isStudioJobNotFoundError(err) {
    var m = String((err && err.message) || err || '');
    return m.indexOf('404') !== -1 || m.toLowerCase().indexOf('not found') !== -1;
  }

  /** Remove a job id from phase pointers when GET/retry returns 404. */
  function clearInvalidStudioJobId(missingId) {
    var s = String(missingId || '').trim();
    if (!s) return false;
    var changed = false;
    if (String(phase1JobId || '').trim() === s) {
      phase1JobId = null;
      _ugcRealPostGridResumePending = false;
      changed = true;
    }
    if (String(phase2JobId || '').trim() === s) {
      phase2JobId = null;
      changed = true;
    }
    var wasPhase3 = String(phase3JobId || '').trim() === s || String(_phase3AnimateJobId || '').trim() === s;
    if (String(phase3JobId || '').trim() === s) {
      phase3JobId = null;
      changed = true;
    }
    if (String(_phase3AnimateJobId || '').trim() === s) {
      _phase3AnimateJobId = null;
      changed = true;
    }
    if (String(currentJobId || '').trim() === s) {
      currentJobId =
        String(phase3JobId || phase2JobId || phase1JobId || '').trim() || null;
      changed = true;
    }
    if (wasPhase3 && !phase3JobId && !_phase3AnimateJobId) {
      // Phase 3 job is gone — wipe stale scene data so the user doesn't see old prompts/images
      // that belong to a job that no longer exists on this server.
      window._scenePromptsForImages = [];
      currentSceneImages = [];
      _lastScenePromptsJson = null;
      _lastSceneImagesRendered = null;
      resetStudioMonotonicMediaAccumulators();
      _sceneImagesBatchInFlight = false;
      try {
        var spGrid = document.getElementById('scenePromptsGrid');
        if (spGrid) spGrid.innerHTML = '';
      } catch (eClearGrid) {}
      try {
        if (typeof StudioMedia !== 'undefined') {
          StudioMedia.renderSceneImages([], []);
        }
      } catch (eClearImg) {}
    }
    return changed;
  }

  function updateStudioJobLinkBanner() {
    var banner = document.getElementById('studioJobLinkBanner');
    var txt = document.getElementById('studioJobLinkBannerText');
    if (!banner || !txt) return;
    if (_studioLinkedJobsMissing) {
      banner.style.display = 'block';
      txt.textContent =
        'The pipeline job IDs in this session are not on the server (404). Common causes: different API key or Studio URL, another user account, or the job was removed. Prompts, scene text, and images saved in the session are still here. To run the server again: go to step 9 → Generate scene prompts (creates a new job), then images → Animate. Retry / Resume only work with a live job.';
    } else {
      banner.style.display = 'none';
    }
  }

  /** Download video without navigating away from Studio (avoids <a href> to CDN opening in the same tab). */
  function studioTriggerDownload(url, fileName) {
    if (!url || typeof url !== 'string' || url.indexOf('http') !== 0) return;
    var name = fileName || 'studio-video.mp4';
    var clean = url.split('#')[0];
    fetch(clean, { mode: 'cors', credentials: 'omit' })
      .then(function (res) {
        if (!res.ok) throw new Error(String(res.status));
        return res.blob();
      })
      .then(function (blob) {
        var u = URL.createObjectURL(blob);
        var a = document.createElement('a');
        a.href = u;
        a.download = name;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        setTimeout(function () {
          URL.revokeObjectURL(u);
        }, 4000);
      })
      .catch(function () {
        alert(
          'Could not download automatically (often blocked when the file is on another domain). You stay in Studio: right-click the video player and choose Save video as.'
        );
      });
  }

  function wireStudioDownloadButtons() {
    var ids = ['finalVideoDownload', 'finalVideoNoSubsDownload', 'subtitledVideoDownload'];
    for (var wi = 0; wi < ids.length; wi++) {
      var el = document.getElementById(ids[wi]);
      if (!el) continue;
      if (el.getAttribute('data-studio-dl-wired') === '1') continue;
      el.setAttribute('data-studio-dl-wired', '1');
      el.addEventListener('click', function (ev) {
        ev.preventDefault();
        var u = this.getAttribute('data-download-url');
        var name = this.getAttribute('data-download-name') || 'studio-video.mp4';
        if (u && u.indexOf('http') === 0) studioTriggerDownload(u, name);
      });
    }
  }

  /** Save current state. addToStack: if true, also add this state to "Previous sessions" list (only when user clicks Save session). */
  function saveSession(addToStack) {
    try {
      var payload = buildSessionPayload();
      if (!isCountableStudioSession(payload)) {
        try {
          localStorage.removeItem(SESSION_STORAGE_KEY);
        } catch (eClr) {}
        if (addToStack) {
          updateSessionListUI();
          return;
        }
        return;
      }
      localStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify(payload));
      if (addToStack) {
        var list = loadSessionListPruned();
        if (list.length && isSameSessionState(list[0], payload)) {
          list[0] = payload;
        } else {
          list.unshift(payload);
          list = list.slice(0, MAX_SESSION_LIST);
        }
        try {
          localStorage.setItem(SESSION_LIST_KEY, JSON.stringify(list));
        } catch (e) {}
        updateSessionListUI();
        if (typeof StudioAuth !== 'undefined' && StudioAuth.isAuthEnabled()) {
          StudioAuth.getUser()
            .then(function (u) {
              if (!u) return;
              return StudioAuth.saveSessionToServer(payload, window._studioServerSessionId, null);
            })
            .then(function (id) {
              if (id) window._studioServerSessionId = id;
              updateSessionListUI();
            })
            .catch(function (e) {
              console.warn('Cloud save (manual):', e);
            });
        }
      }
      scheduleCloudSessionSave();
    } catch (e) {
      console.warn('Session save failed:', e);
    }
  }

  function loadSession() {
    try {
      var raw = localStorage.getItem(SESSION_STORAGE_KEY);
      if (!raw) return null;
      var data = JSON.parse(raw);
      if (!data || (data.phase1JobId == null && data.phase2JobId == null && data.phase3JobId == null)) return null;
      if (!isCountableStudioSession(data)) return null;
      return data;
    } catch (e) {
      return null;
    }
  }

  function loadSessionList() {
    try {
      var raw = localStorage.getItem(SESSION_LIST_KEY);
      if (!raw) return [];
      var arr = JSON.parse(raw);
      return Array.isArray(arr) ? arr : [];
    } catch (e) {
      return [];
    }
  }

  /** Local "Previous sessions" list, excluding step-1-only snapshots; rewrites storage when pruning. */
  function loadSessionListPruned() {
    var list = loadSessionList();
    var filtered = list.filter(isCountableStudioSession);
    if (filtered.length !== list.length) {
      try {
        localStorage.setItem(SESSION_LIST_KEY, JSON.stringify(filtered));
      } catch (eW) {}
    }
    return filtered;
  }

  function clearSessionStorage() {
    try {
      localStorage.removeItem(SESSION_STORAGE_KEY);
    } catch (e) {}
  }

  function clearSessionList() {
    try {
      localStorage.removeItem(SESSION_LIST_KEY);
      updateSessionListUI();
    } catch (e) {}
  }

  function clearAllSessions() {
    clearSessionStorage();
    clearSessionList();
  }

  function formatSessionLabel(session) {
    var step = migrateStoredWizardStep(session.currentStep, session);
    var d = session.savedAt ? new Date(session.savedAt) : new Date();
    var dateStr = d.toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
    var timeStr = d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
    return 'Step ' + step + ' — ' + dateStr + ' ' + timeStr;
  }

  var _studioSessionPanelPositionRaf = 0;
  /** Move panel under document.body so position:fixed is not trapped by wizard ancestors (transform on .studio-step etc.). */
  function attachSessionListPanelToBody() {
    var panel = document.getElementById('sessionListPanel');
    if (!panel || panel.parentNode === document.body) return;
    document.body.appendChild(panel);
  }

  function closeSessionListPanel() {
    var panel = document.getElementById('sessionListPanel');
    var dropdown = document.querySelector('.studio-session-dropdown');
    if (!panel) return;
    panel.style.display = 'none';
    if (dropdown && panel.parentNode === document.body) {
      dropdown.appendChild(panel);
    }
  }

  /** Keep Previous sessions above step cards by viewport-fixed coords + high z-index (below auth overlay 10000). */
  function positionStudioSessionListPanel() {
    var btn = document.getElementById('btnPreviousSessions');
    var panel = document.getElementById('sessionListPanel');
    if (!btn || !panel || panel.style.display === 'none') return;
    var r = btn.getBoundingClientRect();
    var vw = window.innerWidth || 800;
    var vh = window.innerHeight || 600;
    var panelW = Math.max(260, r.width);
    var left = r.left;
    if (left + panelW > vw - 8) left = Math.max(8, vw - panelW - 8);
    var top = r.bottom + 4;
    var maxH = Math.max(120, Math.min(320, vh - top - 12));
    panel.style.position = 'fixed';
    panel.style.left = left + 'px';
    panel.style.top = top + 'px';
    panel.style.right = 'auto';
    panel.style.bottom = 'auto';
    panel.style.minWidth = panelW + 'px';
    panel.style.maxHeight = maxH + 'px';
    panel.style.zIndex = '9800';
  }

  function schedulePositionStudioSessionListPanel() {
    if (_studioSessionPanelPositionRaf) cancelAnimationFrame(_studioSessionPanelPositionRaf);
    _studioSessionPanelPositionRaf = requestAnimationFrame(function () {
      _studioSessionPanelPositionRaf = 0;
      positionStudioSessionListPanel();
    });
  }

  function updateSessionListUI() {
    var list = loadSessionListPruned();
    var localEntries = list.map(function (s) {
      return { source: 'local', data: s };
    });
    var panel = document.getElementById('sessionListPanel');
    var countEl = document.getElementById('sessionListCount');

    function renderMerged(merged) {
      _sessionListMerged = merged;
      if (countEl) countEl.textContent = '(' + merged.length + ')';
      if (!panel) return;
      panel.innerHTML = '';
      if (merged.length === 0) {
        panel.appendChild(
          document.createTextNode(
            'No previous sessions. Reach step 2 (Style & duration) or beyond, then use Save session or cloud sync.'
          )
        );
        try {
          if (panel.style.display === 'block') schedulePositionStudioSessionListPanel();
        } catch (eSpp0) {}
        return;
      }
      merged.forEach(function (entry, i) {
        var row = document.createElement('div');
        row.className = 'studio-session-list-item';
        var label = document.createElement('span');
        label.className = 'studio-session-list-label';
        var prefix = entry.source === 'server' ? '\u2601 ' : '';
        var namePart =
          entry.source === 'server' && entry.name
            ? entry.name.slice(0, 48) + ' — '
            : '';
        label.textContent = prefix + namePart + formatSessionLabel(entry.data);
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'studio-btn studio-btn-ghost studio-btn-sm';
        btn.textContent = 'Restore';
        btn.dataset.sessionIndex = String(i);
        row.appendChild(label);
        row.appendChild(btn);
        panel.appendChild(row);
      });
      try {
        if (panel.style.display === 'block') schedulePositionStudioSessionListPanel();
      } catch (eSpp) {}
    }

    if (typeof StudioAuth === 'undefined' || !StudioAuth.isAuthEnabled()) {
      renderMerged(localEntries);
      return;
    }
    StudioAuth.loadSessionsFromServer()
      .then(function (rows) {
        var serverEntries = (rows || [])
          .map(function (r) {
            var p = {};
            try {
              p = JSON.parse(JSON.stringify(r.payload || {}));
            } catch (e) {}
            if (!p.savedAt && r.updated_at) p.savedAt = r.updated_at;
            p.studioServerSessionId = r.id;
            return { source: 'server', serverId: r.id, name: r.name, data: p };
          })
          .filter(function (entry) {
            return isCountableStudioSession(entry.data);
          });
        renderMerged(serverEntries.concat(localEntries));
      })
      .catch(function () {
        renderMerged(localEntries);
      });
  }

  /**
   * Restore form fields + video type visibility before applyIntermediates so pipeline branches
   * (isUgcRealFlow, product vs influencer) match the saved session — otherwise auto goToStep
   * inside applyIntermediates can override the user's saved wizard step.
   */
  function applySessionFormAndVisibility(session) {
    if (!session) return;
    if (session.formSnapshot && typeof StudioSteps !== 'undefined' && StudioSteps.setFormAndUploadSnapshot) {
      try {
        StudioSteps.setFormAndUploadSnapshot(session.formSnapshot);
        if (typeof StudioSteps.syncVideoTypeFromSession === 'function') {
          StudioSteps.syncVideoTypeFromSession(session);
        }
        try {
          StudioSteps.updateVisibilityForVideoType();
        } catch (eVisRestore) {}
        var promptEl = document.getElementById('prompt');
        var promptCountEl = document.getElementById('promptCount');
        if (promptEl && promptCountEl) promptCountEl.textContent = (promptEl.value || '').length;
        var voEl = document.getElementById('voScript');
        var wcEl = document.getElementById('voWordCount');
        if (voEl && wcEl) wcEl.textContent = (voEl.value || '').trim().split(/\s+/).filter(Boolean).length;
        try {
          updateCharacterLibrarySaveVisibility();
        } catch (eLibVis) {}
      } catch (e) {}
    } else if (session && typeof StudioSteps !== 'undefined' && StudioSteps.syncVideoTypeFromSession) {
      try {
        StudioSteps.syncVideoTypeFromSession(session);
        StudioSteps.updateVisibilityForVideoType();
      } catch (eSyncOnly) {}
    }
  }

  function applyRestoredSession(session) {
    if (!session) return;
    resetStudioMonotonicMediaAccumulators();
    try {
      studioResetUgcRealClientCaches();
    } catch (eUgcReset) {}
    studioClearAllSceneImagePins();
    collectedIntermediates = {};
    _studioLinkedJobsMissing = false;
    updateStudioJobLinkBanner();
    try {
      if (session.output && typeof session.output === 'object') {
        _studioLastJobOutput = JSON.parse(JSON.stringify(session.output));
      } else {
        _studioLastJobOutput = {};
      }
    } catch (eOut0) {
      _studioLastJobOutput = {};
    }
    if (session.studioServerSessionId) {
      window._studioServerSessionId = session.studioServerSessionId;
    } else {
      window._studioServerSessionId = null;
    }
    phase1JobId = session.phase1JobId || null;
    phase2JobId = session.phase2JobId || null;
    phase3JobId = session.phase3JobId || null;
    _phase3AnimateJobId = session.phase3AnimateJobId || session.phase3JobId || null;
    if (phase3JobId) {
      _scenePromptsJobStarted = true;
    }
    currentJobId = session.currentJobId || phase3JobId || phase2JobId || phase1JobId || null;
    currentSceneImages = Array.isArray(session.currentSceneImages) ? session.currentSceneImages.slice() : [];
    if (session.scenePrompts && session.scenePrompts.length) {
      window._scenePromptsForImages = normalizeStudioScenePrompts(session.scenePrompts.slice());
    }
    var step = migrateStoredWizardStep(session.currentStep, session);
    currentStep = step;
    var ugcRestore = sessionLooksUgcReal(session);
    var prefsStR = ugcRestore ? 7 : 6;
    var nineStR = ugcRestore ? 9 : 7;
    var charStR = ugcRestore ? null : 7;
    var voStR = ugcRestore ? 10 : 9;
    var assetsStR = ugcRestore ? 12 : 11;
    var subsStR = ugcRestore ? 16 : 15;
    if (step === prefsStR && phase1JobId) {
      currentJobId = String(phase1JobId);
    } else if (charStR != null && step === charStR && phase1JobId && !ugcRestore) {
      currentJobId = phase2JobId ? String(phase2JobId) : String(phase1JobId);
    } else if (step === nineStR && ugcRestore && phase1JobId) {
      currentJobId = String(phase1JobId);
    } else if (step === voStR) {
      if (ugcRestore) {
        if (phase2JobId) {
          currentJobId = String(phase2JobId);
        } else if (phase1JobId) {
          currentJobId = String(phase1JobId);
        }
      } else if (phase2JobId) {
        currentJobId = String(phase2JobId);
      }
    } else if (step === 8 && ugcRestore && phase1JobId) {
      currentJobId = String(phase1JobId);
    } else if (step >= assetsStR && step <= subsStR) {
      if (ugcRestore && phase1JobId) {
        currentJobId = String(phase1JobId);
      } else if (phase3JobId) {
        currentJobId = String(phase3JobId);
      }
    }
    applySessionFormAndVisibility(session);
    var savedIm = session.intermediates || {};
    var savedOut = session.output || {};
    var hasFullSnapshot =
      savedIm &&
      (savedIm.parsed_texts ||
        savedIm.vo_script ||
        savedIm.scene_prompts ||
        savedIm.scene_plan ||
        savedIm.nine_cell_plan ||
        savedIm.grid_image_url ||
        (savedIm.scene_grids && savedIm.scene_grids.length) ||
        (savedIm.grid_manifests && savedIm.grid_manifests.length) ||
        (savedIm.scene_images && savedIm.scene_images.length) ||
        (savedIm.scene_videos && savedIm.scene_videos.length) ||
        (savedIm.music_url && String(savedIm.music_url).trim()) ||
        (savedIm.vo_audio_url && String(savedIm.vo_audio_url).trim()) ||
        (savedIm.concat_url && String(savedIm.concat_url).trim()));
    var hasOutputSnapshot =
      savedOut &&
      typeof savedOut === 'object' &&
      Object.keys(savedOut).some(function (k) {
        var v = savedOut[k];
        return v != null && String(v).trim() !== '';
      });
    if (!hasFullSnapshot && hasOutputSnapshot) {
      hasFullSnapshot = true;
    }
    if (hasFullSnapshot) {
      if (savedIm.scene_images && Array.isArray(savedIm.scene_images)) currentSceneImages = savedIm.scene_images.slice();
      if (savedIm.scene_prompts && Array.isArray(savedIm.scene_prompts)) {
        window._scenePromptsForImages = normalizeStudioScenePrompts(savedIm.scene_prompts.slice());
      }
      try {
        collectedIntermediates = JSON.parse(JSON.stringify(savedIm));
      } catch (e) {}
      applyIntermediates(savedIm, savedOut, { status: 'completed' });
    }
    var jobToPoll = currentJobId;
    function finishRestore() {
      goToStep(step);
      if (currentJobId) {
        pollJob();
        if (pollIntervalId) clearInterval(pollIntervalId);
        pollIntervalId = setInterval(pollJob, 2000);
      }
    }
    if (jobToPoll) {
      StudioAPI.getJob(jobToPoll)
        .then(function (job) {
          var im = job.intermediates || {};
          var out = job.output || {};
          collectedIntermediates = Object.assign({}, collectedIntermediates, im);
          try {
            if (out && typeof out === 'object' && Object.keys(out).length > 0) {
              _studioLastJobOutput = JSON.parse(JSON.stringify(out));
            }
          } catch (eOut1) {}
          _studioLinkedJobsMissing = false;
          updateStudioJobLinkBanner();
          _lastPollJobSnapshot = job;
          applyIntermediates(im, out, job);
          finishRestore();
        })
        .catch(function (err) {
          if (isStudioJobNotFoundError(err)) {
            _studioLinkedJobsMissing = true;
            clearInvalidStudioJobId(jobToPoll);
            try {
              saveSession();
            } catch (eSave) {}
            updateStudioJobLinkBanner();
          }
          finishRestore();
        });
    } else {
      finishRestore();
    }
  }

  function escapeLogHtml(s) {
    if (s == null) return '';
    var d = document.createElement('div');
    d.textContent = String(s);
    return d.innerHTML;
  }

  window.StudioAPILog = {
    entries: [],
    maxEntries: 480,
    append: function (entry) {
      var e = {
        ts: new Date().toISOString(),
        method: entry.method || 'GET',
        path: entry.path || '',
        bodySummary: entry.bodySummary || '',
        status: entry.status != null ? String(entry.status) : '',
        err: entry.err,
        responseSummary: entry.responseSummary || ''
      };
      this.entries.push(e);
      if (this.entries.length > this.maxEntries) this.entries.shift();
      this.render();
    },
    _extOnlyFilter: function () {
      try {
        var cb = document.getElementById('apiLogExtOnly');
        if (cb) return !!cb.checked;
        var v = localStorage.getItem('studio_api_log_ext_only');
        return v === '1';
      } catch (e) {
        return false;
      }
    },
    render: function () {
      var el = document.getElementById('apiLogEntries');
      if (!el) return;
      var entries = this.entries.slice();
      if (this._extOnlyFilter()) {
        entries = entries.filter(function (e) {
          return e.method === 'EXT';
        });
      }
      entries = entries.reverse();
      el.innerHTML = entries.map(function (e) {
        var statusClass = 'studio-log-status';
        if (e.status.length >= 1) {
          if (e.status[0] === '2') statusClass += ' studio-log-status-2xx';
          else if (e.status[0] === '4' || e.status[0] === '5') statusClass += ' studio-log-status-err';
        }
        var bodyMax =
          e.method === 'EXT' ? 900 : e.method === 'PIPE' || e.method === 'LOG' ? 400 : 80;
        var bodyShort = e.bodySummary
          ? e.bodySummary.length > bodyMax
            ? e.bodySummary.slice(0, bodyMax) + '\u2026'
            : e.bodySummary
          : '';
        var methodCls = 'studio-log-method' + (e.method === 'EXT' ? ' studio-log-ext' : '');
        var parts = [
          '<span class="studio-log-ts">[' + escapeLogHtml(e.ts) + ']</span> ',
          '<span class="' + methodCls + '">' + escapeLogHtml(e.method) + '</span> ',
          '<span class="studio-log-path">' + escapeLogHtml(e.path) + '</span>'
        ];
        if (e.status !== '') parts.push(' <span class="studio-log-arrow">\u2192</span> <span class="' + statusClass + '">' + escapeLogHtml(e.status) + '</span>');
        if (bodyShort) parts.push(' <span class="studio-log-meta">| ' + escapeLogHtml(bodyShort) + '</span>');
        if (e.responseSummary) parts.push(' <span class="studio-log-meta">| ' + escapeLogHtml(e.responseSummary) + '</span>');
        if (e.err) parts.push(' <span class="studio-log-err">| ERROR: ' + escapeLogHtml(e.err.message || e.err) + '</span>');
        return '<div class="studio-api-log-line">' + parts.join('') + '</div>';
      }).join('');
    },
    clear: function () {
      this.entries.length = 0;
      this.render();
    }
  };
  var apiLogClearBtn = document.getElementById('apiLogClear');
  if (apiLogClearBtn) apiLogClearBtn.addEventListener('click', function () { window.StudioAPILog.clear(); });
  var apiLogExtOnlyCb = document.getElementById('apiLogExtOnly');
  if (apiLogExtOnlyCb) {
    try {
      var extPref = localStorage.getItem('studio_api_log_ext_only');
      if (extPref === null) {
        localStorage.setItem('studio_api_log_ext_only', '0');
        extPref = '0';
      }
      apiLogExtOnlyCb.checked = extPref === '1';
    } catch (e0) {
      apiLogExtOnlyCb.checked = false;
    }
    apiLogExtOnlyCb.addEventListener('change', function () {
      try {
        localStorage.setItem('studio_api_log_ext_only', apiLogExtOnlyCb.checked ? '1' : '0');
      } catch (e1) {}
      window.StudioAPILog.render();
    });
  }

  function scenePromptsPipelineHint(currentStepName) {
    var s = (currentStepName || '').trim();
    if (!s) {
      return 'The server is running your job; cards below fill when scene prompts are written.';
    }
    // Map real monolith step names to friendly labels with rough timing so users know what's happening.
    var stepHints = {
      'character_description': 'Describing the character (~1s).',
      'parse_prompt': 'Parsing prompt into TEXT 1–4 (~1s).',
      'analyze_media': 'Analyzing reference images and asset videos (~2–8s).',
      'extract_highlights': 'Extracting key talking points for the voiceover (~5–10s).',
      'extract_venue_dna': 'Extracting venue / location details for scene consistency (~5–10s).',
      'vo_generation': 'Writing voiceover script and generating TTS audio (~30–60s — this is the longest pre-step).',
      'music_generation': 'Generating background music in parallel (Suno, ~30–90s).',
      'scene_prompts': 'Writing all scene prompts in one Director+Writer call (~30–90s for many scenes — this is the last step before cards appear).',
      'step_3': 'Scene prompts being finalized — cards appear when this step completes.',
      'step_2': 'Media director / scene structure (runs before the prompt writer).',
      'step_1': 'Early pipeline steps (parse, character, analyze).',
      'step_0': 'Parsing and setup — scene prompts come after director and writer stages.'
    };
    if (stepHints[s]) return stepHints[s];
    if (s.indexOf('parse') !== -1) return stepHints.parse_prompt;
    if (s.indexOf('analyze') !== -1) return stepHints.analyze_media;
    if (s.indexOf('vo') === 0 || s.indexOf('voice') !== -1) return stepHints.vo_generation;
    if (s.indexOf('music') !== -1) return stepHints.music_generation;
    if (s.indexOf('venue') !== -1) return stepHints.extract_venue_dna;
    if (s.indexOf('highlight') !== -1) return stepHints.extract_highlights;
    if (s.indexOf('character') !== -1) return stepHints.character_description;
    if (s.indexOf('scene_prompt') !== -1) return stepHints.scene_prompts;
    return 'Pipeline step: ' + s + '. (No friendly label yet — add one in scenePromptsPipelineHint if this step is common.)';
  }

  function refreshScenePromptsStepUI(jobRef) {
    var act = document.getElementById('scenePromptsActivity');
    var spin = document.getElementById('scenePromptsSpinner');
    var titleEl = document.getElementById('scenePromptsActivityTitle');
    var subEl = document.getElementById('scenePromptsActivitySub');
    var el = document.getElementById('scenePromptsStatus');
    if (!el) return;

    function hideActivity() {
      if (act) {
        act.style.display = 'none';
        act.classList.remove('studio-step7-running');
      }
      if (spin) {
        spin.style.display = 'none';
        spin.classList.add('studio-spinner-off');
      }
    }

    if (isUgcRealFlow()) {
      hideActivity();
      el.textContent = '';
      return;
    }
    if (currentStep !== studioPromptsStep()) {
      hideActivity();
      el.textContent = '';
      return;
    }

    var hasPrompts = !!(window._scenePromptsForImages && window._scenePromptsForImages.length);
    var job =
      jobRef && phase3JobId && String(jobRef.id) === String(phase3JobId)
        ? jobRef
        : _lastPollJobSnapshot && phase3JobId && String(_lastPollJobSnapshot.id) === String(phase3JobId)
          ? _lastPollJobSnapshot
          : null;

    if (hasPrompts) {
      hideActivity();
      el.textContent = window._scenePromptsForImages.length + ' scene prompts ready.';
      return;
    }

    if (!phase3JobId) {
      if (act) {
        act.style.display = 'block';
        act.classList.remove('studio-step7-running');
      }
      if (spin) {
        spin.style.display = 'none';
        spin.classList.add('studio-spinner-off');
      }
      if (titleEl) titleEl.textContent = 'No scene prompts yet';
      if (subEl) {
        subEl.textContent =
          'Go back to Scene assets and click Generate scene prompts. Next on that screen only moves forward — it does not start this job.';
      }
      el.textContent = '';
      return;
    }

    var st = job ? String(job.status || '').trim() : '';
    var failedLike = st === 'failed' || st === 'aborted';

    if (failedLike && job) {
      if (act) {
        act.style.display = 'block';
        act.classList.remove('studio-step7-running');
      }
      if (spin) {
        spin.style.display = 'none';
        spin.classList.add('studio-spinner-off');
      }
      if (titleEl) titleEl.textContent = 'Scene prompts job did not complete';
      if (subEl) {
        subEl.textContent =
          'Status: ' +
          (st || 'unknown') +
          '. Check the API Log or your jobs list, then start again from Scene assets → Generate scene prompts.';
      }
      el.textContent = '';
      return;
    }

    if (st === 'paused' && job) {
      if (act) {
        act.style.display = 'block';
        act.classList.remove('studio-step7-running');
      }
      if (spin) {
        spin.style.display = 'none';
        spin.classList.add('studio-spinner-off');
      }
      var pausedAtPreScene =
        job.current_step === 'vo_generation' ||
        job.current_step === 'music_generation' ||
        job.current_step === 'step_1' ||
        job.current_step === 'step_2';
      if (titleEl) {
        titleEl.textContent = pausedAtPreScene
          ? 'Pipeline interrupted — resume to generate scene prompts'
          : 'Job paused — scene prompts not shown yet';
      }
      if (subEl) {
        var resumeMsg = pausedAtPreScene
          ? 'The pipeline was stopped before reaching the scene-prompts stage (last step: ' +
            (job.current_step || '—') +
            '). Click Resume to continue from where it left off.'
          : 'Last step: ' + (job.current_step || '—') + '. The job is paused on the server.';
        subEl.innerHTML = '';
        subEl.textContent = resumeMsg;
        if (pausedAtPreScene && phase3JobId) {
          var existingResumeBtn = subEl.parentNode && subEl.parentNode.querySelector('.scene-prompts-resume-btn');
          if (!existingResumeBtn) {
            var resumeBtn = document.createElement('button');
            resumeBtn.type = 'button';
            resumeBtn.className = 'studio-btn studio-btn-primary scene-prompts-resume-btn';
            resumeBtn.style.marginTop = '0.75rem';
            resumeBtn.style.display = 'flex';
            resumeBtn.style.alignItems = 'center';
            resumeBtn.style.gap = '0.4rem';
            resumeBtn.innerHTML = '<span class="material-symbols-outlined" style="font-size:1.1em;">play_circle</span>Resume pipeline';
            resumeBtn.addEventListener('click', function () {
              resumeBtn.disabled = true;
              resumeBtn.textContent = 'Resuming…';
              StudioAPI.resumeJob(phase3JobId).catch(function (e) {
                resumeBtn.disabled = false;
                resumeBtn.innerHTML = '<span class="material-symbols-outlined" style="font-size:1.1em;">play_circle</span>Resume pipeline';
                if (subEl) subEl.textContent = 'Resume failed: ' + ((e && e.message) || String(e));
              });
            });
            subEl.parentNode && subEl.parentNode.appendChild(resumeBtn);
          }
        }
      }
      el.textContent = '';
      return;
    }

    if (st === 'completed' && job) {
      if (act) {
        act.style.display = 'block';
        act.classList.remove('studio-step7-running');
      }
      if (spin) {
        spin.style.display = 'none';
        spin.classList.add('studio-spinner-off');
      }
      if (titleEl) titleEl.textContent = 'Job completed but no prompts in this session';
      if (subEl) {
        subEl.textContent =
          'Try refreshing the page or restoring the session. If intermediates are missing, start a new scene-prompts job from Scene assets.';
      }
      el.textContent = '';
      return;
    }

    if (act) {
      act.style.display = 'block';
      act.classList.add('studio-step7-running');
      var oldResumeBtn = act.querySelector('.scene-prompts-resume-btn');
      if (oldResumeBtn) oldResumeBtn.remove();
    }
    if (spin) {
      spin.classList.remove('studio-spinner-off');
      spin.style.display = 'inline-block';
    }
    if (titleEl) {
      titleEl.textContent = job ? 'Generating scene prompts on the server…' : 'Starting scene prompts job…';
    }
    if (subEl) {
      var hint = job ? scenePromptsPipelineHint(job.current_step) : 'Waiting for the first job update from your API.';
      subEl.textContent = hint + ' This screen updates automatically (about every 2 seconds).';
    }
    el.textContent = '';
    var grid = document.getElementById('scenePromptsGrid');
    if (grid && !grid.querySelector('.scene-prompts-skeleton')) {
      var skel = document.createElement('div');
      skel.className = 'scene-prompts-skeleton';
      skel.innerHTML =
        '<div class="scene-prompts-skeleton-row"></div>' +
        '<div class="scene-prompts-skeleton-row"></div>' +
        '<div class="scene-prompts-skeleton-row"></div>';
      grid.appendChild(skel);
    }
  }

  function voicesCacheKey(lang, gender) {
    return (lang || 'en') + '|' + (gender || 'f');
  }

  function fillVoiceSelect(sel, voices) {
    if (!sel) return;
    sel.innerHTML = '';
    (voices || []).forEach(function (v) {
      var opt = document.createElement('option');
      opt.value = v.voice_id;
      opt.textContent = (v.label || v.voice_id) + ' (' + v.voice_id + ')';
      sel.appendChild(opt);
    });
    if (!voices || voices.length === 0) {
      var opt = document.createElement('option');
      opt.value = '';
      opt.textContent = 'No voices returned — use manual voice ID below';
      sel.appendChild(opt);
    }
  }

  function preloadVoicesForStep7() {
    var lang = StudioSteps.getLanguage ? StudioSteps.getLanguage() : 'en';
    var gender = StudioSteps.getGender ? StudioSteps.getGender() : 'f';
    var key = voicesCacheKey(lang, gender);
    if (_voicesCache[key]) return;
    StudioAPI.getVoices(lang, gender).then(function (voices) {
      _voicesCache[key] = voices;
    }).catch(function () {});
  }

  /** Build voice-design cache key from current context. */
  function _voiceDesignCacheKeyFor(lang, gender, desc) {
    return (lang || 'en') + '|' + (gender || 'f') + '|' + (desc || '').slice(0, 80);
  }

  /**
   * Build the character context for voice design from the current Studio state.
   * Returns { portrait_image_prompt, character_description, character_image_url } — any may be absent.
   */
  function _collectVoiceDesignCharacterContext() {
    var portraitP = ((document.getElementById('portraitImagePromptHidden') || {}).value || '').trim();
    var brief = ((document.getElementById('characterBrief') || {}).value || '').trim();
    var snap = (typeof StudioSteps !== 'undefined' && StudioSteps.getFormAndUploadSnapshot)
      ? StudioSteps.getFormAndUploadSnapshot()
      : {};
    var charUrls = snap.character_urls || (snap.character_url ? [snap.character_url] : []);
    if (_characterReviewPendingUrl && !charUrls.length) {
      charUrls = [_characterReviewPendingUrl];
    }
    var imgUrl = charUrls[0] || undefined;
    /* portrait_image_prompt is the raw Nano Banana template (contains === GLOBAL PIPELINE RULES).
     * Only send it when there is no image URL that the server can describe via Gemini.
     * When an image URL exists, the server produces a 20-word Gemini caption — far safer than the raw prompt. */
    var sendPortraitPrompt = !imgUrl && !!portraitP;
    return {
      portrait_image_prompt: sendPortraitPrompt ? portraitP : undefined,
      character_description: brief || undefined,
      character_image_url: imgUrl
    };
  }

  /** Start ElevenLabs voice-design in the background as soon as character context exists (portrait prompt, brief, or image URL). */
  function maybeKickVoiceDesignAfterCharacterReady() {
    var ctx = _collectVoiceDesignCharacterContext();
    var has =
      !!(ctx.portrait_image_prompt && String(ctx.portrait_image_prompt).trim()) ||
      !!(ctx.character_description && String(ctx.character_description).trim()) ||
      !!(ctx.character_image_url && String(ctx.character_image_url).trim());
    if (!has) return;
    function run() {
      try {
        triggerVoiceDesign(false);
      } catch (eKick) {}
    }
    if (typeof StudioAPI.ensureCanCallProtectedApi === 'function') {
      StudioAPI.ensureCanCallProtectedApi()
        .then(function (ok) {
          if (ok) run();
        })
        .catch(function () {});
      return;
    }
    run();
  }

  /** Store Nano Banana portrait prompt from /api/generate-character (for ElevenLabs voice design). */
  function setPortraitImagePromptHidden(text) {
    var el = document.getElementById('portraitImagePromptHidden');
    if (!el) return;
    el.value = text && String(text).trim() ? String(text).trim() : '';
    try {
      if (typeof StudioSteps !== 'undefined' && StudioSteps.refreshStep4CharacterExclusiveHint) {
        StudioSteps.refreshStep4CharacterExclusiveHint();
      }
    } catch (ePh) {}
  }

  /**
   * Render a single preview card for a designed voice.
   * Returns an HTMLElement.
   */
  function _buildVoicePreviewCard(preview, idx, isSelected) {
    var card = document.createElement('div');
    card.className = 'voice-design-card' + (isSelected ? ' voice-design-card--selected' : '');
    card.setAttribute('data-voice-id', preview.generated_voice_id);
    card.style.cssText = 'display:flex;align-items:center;gap:0.5rem;padding:0.5rem 0.75rem;border:1.5px solid ' +
      (isSelected ? 'var(--c-accent,#6c63ff)' : 'var(--c-border,#d5d5d5)') +
      ';border-radius:8px;margin-bottom:0.4rem;background:' +
      (isSelected ? 'var(--c-accent-subtle,#f0eeff)' : 'var(--c-surface,#fff)') +
      ';cursor:pointer;';

    var label = document.createElement('span');
    label.style.cssText = 'flex:1;font-size:0.875rem;font-weight:' + (isSelected ? '600' : '400') + ';';
    label.textContent = 'Voice ' + (idx + 1) + (preview.duration_secs ? ' (' + preview.duration_secs.toFixed(1) + 's)' : '');
    card.appendChild(label);

    var playBtn = document.createElement('button');
    playBtn.type = 'button';
    playBtn.className = 'studio-btn studio-btn-secondary studio-btn-sm';
    playBtn.style.cssText = 'padding:0.2rem 0.6rem;font-size:0.8rem;white-space:nowrap;';
    playBtn.textContent = '▶ Preview';
    card.appendChild(playBtn);

    var selectBtn = document.createElement('button');
    selectBtn.type = 'button';
    selectBtn.className = 'studio-btn ' + (isSelected ? 'studio-btn-primary' : 'studio-btn-secondary') + ' studio-btn-sm';
    selectBtn.style.cssText = 'padding:0.2rem 0.6rem;font-size:0.8rem;white-space:nowrap;';
    /* Check if this voice is already saved (voEffectiveVoiceId contains a real voice_id, not generated) */
    var effectiveId = (document.getElementById('voEffectiveVoiceId') || {}).value || '';
    var isSaved = isSelected && effectiveId && effectiveId !== preview.generated_voice_id;
    selectBtn.textContent = isSaved ? '✓ Saved & selected' : 'Use this';
    card.appendChild(selectBtn);

    /* Play button handler — use base64 audio if present, otherwise stream URL with ?token / ?studio_user_token */
    playBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      var audioWrap = document.getElementById('voAudioPlayerWrap');
      var audioEl = document.getElementById('voAudio');
      if (!audioEl) return;
      if (preview.audio_base_64) {
        audioEl.src = 'data:' + (preview.media_type || 'audio/mpeg') + ';base64,' + preview.audio_base_64;
        if (audioWrap) audioWrap.style.display = 'block';
        audioEl.play().catch(function () {});
      } else {
        StudioAPI.voicePreviewStreamUrlWithAuth(preview.generated_voice_id).then(function (url) {
          audioEl.src = url;
          if (audioWrap) audioWrap.style.display = 'block';
          audioEl.play().catch(function () {});
        }).catch(function (err) {
          alert('Preview stream failed: ' + (err && err.message ? err.message : err));
        });
      }
    });

    /* Select button handler — saves the voice to get a permanent voice_id before applying */
    function selectThisVoice() {
      if (selectBtn.disabled) return;
      /* Mark this card as selected visually right away */
      _designedVoiceId = preview.generated_voice_id;
      selectBtn.textContent = 'Saving…';
      selectBtn.disabled = true;
      card.style.opacity = '0.7';

      StudioAPI.saveDesignedVoice({
        generated_voice_id: preview.generated_voice_id,
        voice_name: 'Studio Custom Voice',
        voice_description:
          (_voiceDesignCache && _voiceDesignCache.voice_description
            ? String(_voiceDesignCache.voice_description)
            : '') || 'Custom AI-designed studio voice for video',
      }).then(function (saved) {
        /* saved.voice_id is the permanent ElevenLabs voice ID */
        _persistEffectiveVoiceId(saved.voice_id);
        _designedVoiceId = preview.generated_voice_id; /* keep for card selection display */
        _renderVoiceDesignPreviews(_voiceDesignCache, _designedVoiceId);
      }).catch(function (err) {
        selectBtn.textContent = 'Save failed — retry';
        selectBtn.disabled = false;
        card.style.opacity = '1';
        /* Still store the generated_voice_id as a hint but flag it as unsaved */
        var errorEl = document.getElementById('voiceDesignError');
        if (errorEl) {
          errorEl.style.display = 'block';
          errorEl.textContent = 'Could not save voice to library: ' + (err && err.message ? err.message : err) + '. Paste a voice ID manually or choose from the list.';
        }
      });
    }
    selectBtn.addEventListener('click', function (e) { e.stopPropagation(); selectThisVoice(); });
    card.addEventListener('click', selectThisVoice);

    return card;
  }

  /** Write the selected voice ID to the hidden #voEffectiveVoiceId input so steps.js can read it. */
  function _persistEffectiveVoiceId(voiceId) {
    var hidden = document.getElementById('voEffectiveVoiceId');
    if (hidden) hidden.value = voiceId || '';
  }

  /** Render all preview cards into #voiceDesignPreviews, visually highlight the selected card.
   * NOTE: _persistEffectiveVoiceId is NOT called here — it is only called after the voice is
   * saved to the ElevenLabs library (in selectThisVoice inside _buildVoicePreviewCard).
   * This prevents a temporary generated_voice_id from being passed to the pipeline as voice_id.
   */
  function _renderVoiceDesignPreviews(data, selectedId) {
    var container = document.getElementById('voiceDesignPreviews');
    if (!container || !data || !data.previews || !data.previews.length) return;
    container.innerHTML = '';
    /* Highlight first card visually if nothing is saved yet, but do NOT persist it */
    var displaySelectedId = selectedId || (data.previews.length > 0 ? data.previews[0].generated_voice_id : null);
    if (!selectedId && displaySelectedId) {
      _designedVoiceId = displaySelectedId;
    }
    data.previews.forEach(function (p, i) {
      container.appendChild(_buildVoicePreviewCard(p, i, p.generated_voice_id === displaySelectedId));
    });
    /* Show redesign button and save hint */
    var redesignBtn = document.getElementById('btnRedesignVoice');
    if (redesignBtn) redesignBtn.style.display = 'inline-block';
    var saveHint = document.getElementById('voiceDesignSaveHint');
    if (saveHint) saveHint.style.display = 'block';
  }

  /**
   * Trigger /api/voice-design and populate the preview cards.
   * Safe to call repeatedly — uses caching; shows loading spinner while running.
   * @param {boolean} [force] - when true bypasses the cache and re-runs the design call.
   */
  function triggerVoiceDesign(force) {
    if (force) {
      _voiceDesignInflightKey = null;
      _voiceDesignCooldownUntil = 0;
    }
    var lang = StudioSteps.getLanguage ? StudioSteps.getLanguage() : 'en';
    var gender = StudioSteps.getGender ? StudioSteps.getGender() : 'f';
    var ctx = _collectVoiceDesignCharacterContext();
    var desc = ctx.portrait_image_prompt || ctx.character_description || ctx.character_image_url || '';
    var cacheKey = _voiceDesignCacheKeyFor(lang, gender, desc);

    var loadingEl = document.getElementById('voiceDesignLoading');
    if (!force && _voiceDesignCooldownUntil && Date.now() < _voiceDesignCooldownUntil) {
      if (loadingEl) loadingEl.style.display = 'none';
      if (_voiceDesignCache && _voiceDesignCache.previews && _voiceDesignCache.previews.length) {
        _renderVoiceDesignPreviews(_voiceDesignCache, _designedVoiceId);
      }
      return;
    }

    var errorEl = document.getElementById('voiceDesignError');
    var container = document.getElementById('voiceDesignPreviews');
    var redesignBtn = document.getElementById('btnRedesignVoice');

    if (!force && _voiceDesignCacheKey === cacheKey && _voiceDesignCache) {
      if (loadingEl) loadingEl.style.display = 'none';
      _renderVoiceDesignPreviews(_voiceDesignCache, _designedVoiceId);
      return;
    }

    if (!force && _voiceDesignInflightKey === cacheKey) {
      if (loadingEl) loadingEl.style.display = 'flex';
      return;
    }
    _voiceDesignInflightKey = cacheKey;

    var myGen = ++_voiceDesignRequestGen;

    if (loadingEl) loadingEl.style.display = 'flex';
    if (errorEl) { errorEl.style.display = 'none'; errorEl.textContent = ''; }
    /* Do not clear preview cards here — a overlapping second request used to empty the DOM then fail, removing voices the user already saw. _renderVoiceDesignPreviews clears when applying a successful payload. */
    if (redesignBtn) redesignBtn.style.display = 'none';
    var saveHintEl = document.getElementById('voiceDesignSaveHint');
    if (saveHintEl) saveHintEl.style.display = 'none';

    var body = { language: lang, gender: gender };
    if (ctx.portrait_image_prompt) body.portrait_image_prompt = ctx.portrait_image_prompt;
    if (ctx.character_description) body.character_description = ctx.character_description;
    if (ctx.character_image_url) body.character_image_url = ctx.character_image_url;

    StudioAPI.designVoice(body)
      .then(function (data) {
      if (myGen !== _voiceDesignRequestGen) return;
      if (loadingEl) loadingEl.style.display = 'none';
      var list = data && data.previews ? data.previews : [];
      if (!list.length) {
        if (_voiceDesignCache && _voiceDesignCache.previews && _voiceDesignCache.previews.length) {
          _renderVoiceDesignPreviews(_voiceDesignCache, _designedVoiceId);
        }
        if (errorEl) {
          errorEl.style.display = 'block';
          errorEl.textContent =
            'Voice design returned no new previews (temporary ElevenLabs issue). Previous suggestions are kept if available — use Redesign to retry, or pick a voice from the list.';
        }
        if (redesignBtn) redesignBtn.style.display = 'inline-block';
        if (saveHintEl && _voiceDesignCache && _voiceDesignCache.previews && _voiceDesignCache.previews.length) {
          saveHintEl.style.display = 'block';
        }
        _voiceDesignCooldownUntil = Date.now() + 60000;
        return;
      }
      _voiceDesignCooldownUntil = 0;
      _voiceDesignCache = data;
      _voiceDesignCacheKey = cacheKey;
      _designedVoiceId = null; /* reset so first card is auto-selected */
      _renderVoiceDesignPreviews(data, null);
    })
      .catch(function (err) {
        if (myGen !== _voiceDesignRequestGen) return;
        _voiceDesignCooldownUntil = Date.now() + 90000;
        if (loadingEl) loadingEl.style.display = 'none';
        if (_voiceDesignCache && _voiceDesignCache.previews && _voiceDesignCache.previews.length) {
          _renderVoiceDesignPreviews(_voiceDesignCache, _designedVoiceId);
          if (errorEl) {
            errorEl.style.display = 'block';
            errorEl.textContent =
              'Latest voice design request failed — keeping the previous suggestions. (' +
              (err && err.message ? err.message : err) +
              ') Use Redesign to retry.';
          }
        } else if (errorEl) {
          errorEl.style.display = 'block';
          errorEl.textContent =
            'Voice design unavailable — use the list or paste a voice ID below. (' +
            (err && err.message ? err.message : err) +
            ')';
        }
        if (redesignBtn) redesignBtn.style.display = 'inline-block';
        if (saveHintEl && _voiceDesignCache && _voiceDesignCache.previews && _voiceDesignCache.previews.length) {
          saveHintEl.style.display = 'block';
        }
      })
      .finally(function () {
        /* Only clear if this response belongs to the latest request (avoids races with Redesign / overlapping calls). */
        if (myGen === _voiceDesignRequestGen && _voiceDesignInflightKey === cacheKey) {
          _voiceDesignInflightKey = null;
        }
      });
  }

  function refreshStep7VoiceBlock() {
    var block = document.getElementById('step7VoiceBlock');
    if (!block) return;
    /* Let users pick a voice as soon as they open the VO step — do not wait for vo_script (often still generating). */
    block.style.display = 'block';

    /* Trigger voice design (cached / in-flight deduped; previews may already be ready from character step) */
    triggerVoiceDesign(false);

    var sel = document.getElementById('voVoiceSelect');
    if (!sel) return;
    var lang = StudioSteps.getLanguage ? StudioSteps.getLanguage() : 'en';
    var gender = StudioSteps.getGender ? StudioSteps.getGender() : 'f';
    var key = voicesCacheKey(lang, gender);
    if (_voicesCache[key]) {
      fillVoiceSelect(sel, _voicesCache[key]);
      return;
    }
    if (sel.options.length > 0 && sel.options[0].value !== '') return;
    sel.innerHTML = '';
    var loadingOpt = document.createElement('option');
    loadingOpt.value = '';
    loadingOpt.textContent = 'Loading voices…';
    loadingOpt.disabled = true;
    sel.appendChild(loadingOpt);
    StudioAPI.getVoices(lang, gender).then(function (voices) {
      _voicesCache[key] = voices;
      if (!sel) return;
      fillVoiceSelect(sel, voices);
    }).catch(function () {
      if (sel) {
        sel.innerHTML = '';
        var opt = document.createElement('option');
        opt.value = '';
        opt.textContent = 'Could not load voices — use manual ID below';
        sel.appendChild(opt);
      }
    });
  }

  function getStepEl(stepNum) {
    if (isUgcRealFlow()) {
      var map = {
        1: 'step1',
        2: 'step2',
        3: 'step3',
        4: 'step4',
        5: 'step6',
        7: 'step7prefs',
        8: 'step6ugcCharacterGate',
        9: 'step6ugcNineCell',
        10: 'step8vo',
        11: 'step9music',
        12: 'step13final',
        13: 'step14subs'
      };
      var elId = map[stepNum];
      return elId ? document.getElementById(elId) : null;
    }
    /* Character gate has no data-step (same DOM for influencer step 7 and product step 12). */
    var nugcCharStep = studioNonUgcCharacterStep();
    if (nugcCharStep != null && stepNum === nugcCharStep) {
      return document.getElementById('stepNonUgcCharacterGate');
    }
    var prodCharStep = studioProductCharacterGateStep();
    if (prodCharStep != null && stepNum === prodCharStep) {
      return document.getElementById('stepNonUgcCharacterGate');
    }
    /* Product: prompts/images/final/subs use wizard indices 13–16 while data-step on DOM stays 12–15. */
    if (isStudioProductVideo()) {
      var mapP = {
        1: 'step1',
        2: 'step2',
        3: 'step3',
        4: 'step4',
        5: 'step6',
        6: 'step7prefs',
        9: 'step8vo',
        10: 'step9music',
        11: 'step10assets',
        13: 'step11prompts',
        14: 'step12images',
        15: 'step13final',
        16: 'step14subs'
      };
      var pid = mapP[stepNum];
      if (pid) return document.getElementById(pid);
      return null;
    }
    return document.querySelector('.studio-step[data-step="' + stepNum + '"]');
  }

  /**
   * Merge scene_videos from intermediates and output. Supabase often stores [] in intermediates while
   * the wrapper mirrors clips in job output — but `im.scene_videos || out.scene_videos` wrongly keeps []
   * because empty arrays are truthy in JavaScript.
   */
  function studioEffectiveSceneVideos(im, out) {
    im = im || {};
    out = out || {};
    var a = Array.isArray(im.scene_videos) ? im.scene_videos : [];
    var b = Array.isArray(out.scene_videos) ? out.scene_videos : [];
    function slotOk(u) {
      return u != null && typeof u === 'string' && String(u).trim().length > 5;
    }
    if (!a.length && !b.length) return [];
    if (!b.length) return a.slice();
    if (!a.length) return b.slice();
    var n = Math.max(a.length, b.length);
    var merged = [];
    for (var i = 0; i < n; i++) {
      var ua = i < a.length ? a[i] : '';
      var ub = i < b.length ? b[i] : '';
      merged.push(slotOk(ua) ? ua : slotOk(ub) ? ub : ua || ub || '');
    }
    return merged;
  }

  function resetStudioMonotonicMediaAccumulators() {
    _studioAccumulatedSceneImages = null;
    _studioAccumulatedSceneVideos = null;
  }

  /**
   * Per-index: keep a string URL once seen; tolerate empty server snapshots; false = failed slot.
   */
  function mergeMonotonicMediaSlots(targetLen, incomingSlots, accPrev) {
    var acc = accPrev && Array.isArray(accPrev) ? accPrev.slice() : [];
    while (acc.length < targetLen) acc.push(undefined);
    if (acc.length > targetLen) acc = acc.slice(0, targetLen);
    for (var i = 0; i < targetLen; i++) {
      var inc = i < incomingSlots.length ? incomingSlots[i] : undefined;
      var prev = acc[i];
      if (inc === false) {
        acc[i] = false;
        continue;
      }
      var gInc = inc && typeof inc === 'string' && inc.trim().length > 5;
      var gPrev = prev && typeof prev === 'string' && prev.trim().length > 5;
      if (gInc) acc[i] = inc;
      else if (gPrev) acc[i] = prev;
      else if (prev === false) acc[i] = false;
      else acc[i] = undefined;
    }
    return acc;
  }

  /** Best-known clip per slot for UI + Approve (server row + monotonic client cache). */
  function studioSceneVideosForProgress(im, out) {
    var eff = studioEffectiveSceneVideos(im, out);
    var acc = _studioAccumulatedSceneVideos;
    if (!acc || !Array.isArray(acc) || acc.length === 0) return eff;
    var n = Math.max(eff.length, acc.length);
    var res = [];
    for (var i = 0; i < n; i++) {
      var e = i < eff.length ? eff[i] : '';
      var a = i < acc.length ? acc[i] : '';
      var ge = e && typeof e === 'string' && e.length > 5;
      var ga = a && typeof a === 'string' && a.length > 5;
      res.push(ge ? e : ga ? a : e || a || '');
    }
    return res;
  }

  /** True when every expected scene has a clip URL (job intermediates and/or video grid). Uses same URL length rule as the animation progress line (> 5). */
  function studioStep12AllClipsReady(im, out) {
    im = im || {};
    out = out || {};
    var sceneVideos = studioSceneVideosForProgress(im, out);
    var imgLen = (currentSceneImages && currentSceneImages.length) || (im.scene_images && im.scene_images.length) || 0;
    var sceneCount = Math.max(imgLen, Array.isArray(sceneVideos) ? sceneVideos.length : 0, 1);
    var doneCount = sceneVideos.filter(function (u) {
      return u && (typeof u === 'string' ? u.length > 5 : true);
    }).length;
    if (sceneCount > 0 && doneCount >= sceneCount) return true;
    var fromGrid = collectSceneVideoUrlsFromGrid();
    if (fromGrid.length < sceneCount) return false;
    return fromGrid.every(function (u) {
      return u && typeof u === 'string' && u.length > 5;
    });
  }

  /** Job paused after all scene animations, before Rendi concat (step_12 gate). */
  function studioAnimationsPausedForReview(job) {
    if (!job || job.status !== 'paused') return false;
    var im = job.intermediates || {};
    if (im.concat_url) return false;
    var out = job.output || {};
    if (out.final_mp4_url || out.final_video_url || im.final_video_url) return false;
    return studioStep12AllClipsReady(im, out);
  }

  function collectSceneVideoUrlsFromGrid() {
    var grid = document.getElementById('sceneVideosGrid');
    if (!grid) return [];
    var cards = grid.querySelectorAll('.studio-media-card');
    var out = [];
    for (var i = 0; i < cards.length; i++) {
      var v = cards[i].querySelector('video');
      var src = v && (v.currentSrc || v.src);
      out.push(src && src.length > 5 ? src : null);
    }
    return out;
  }

  var _pipelineLogJobId = null;
  var _pipelineLogAfter = 0;
  var _lastJobLogsFingerprint = '';
  var _assemblyLogPollCounter = 0;

  function appendJobLogsToApiLog(jobId) {
    if (!jobId || typeof StudioAPI === 'undefined' || !StudioAPI.fetchJobLogs) return;
    StudioAPI.fetchJobLogs(jobId)
      .then(function (data) {
        var logs = (data && data.logs) || [];
        var fp = logs.length + '|' + String(logs[logs.length - 1] || '');
        if (fp === _lastJobLogsFingerprint) return;
        _lastJobLogsFingerprint = fp;
        logs.forEach(function (line) {
          var s = typeof line === 'string' ? line : JSON.stringify(line);
          window.StudioAPILog.append({
            method: 'LOG',
            path: '/api/jobs/…/logs',
            bodySummary: s.slice(0, 600),
            status: 200
          });
        });
      })
      .catch(function () {});
  }

  function appendPipelineEventsToLog(job) {
    var jid = job && job.id;
    if (!jid || typeof StudioAPI === 'undefined' || !StudioAPI.getBaseUrl) return;
    if (_pipelineLogJobId !== String(jid)) {
      _pipelineLogJobId = String(jid);
      _pipelineLogAfter = 0;
    }
    var url =
      StudioAPI.getBaseUrl() +
      '/api/jobs/' +
      encodeURIComponent(jid) +
      '/pipeline-events?after=' +
      _pipelineLogAfter;
    StudioAPI.getAuthHeadersWithStudioUser()
      .then(function (headers) {
        return fetch(url, { headers: headers });
      })
      .then(function (r) {
        if (!r.ok) {
          return r.text().then(function (t) {
            try {
              window.StudioAPILog.append({
                method: 'GET',
                path: '/api/jobs/' + String(jid).slice(0, 8) + '…/pipeline-events',
                bodySummary: 'after=' + _pipelineLogAfter,
                status: r.status,
                responseSummary: (t || '').slice(0, 200)
              });
            } catch (eLog) {}
            return null;
          });
        }
        return r.json();
      })
      .then(function (data) {
        if (!data) return;
        var evs = data.events || [];
        if (data.next_after != null) _pipelineLogAfter = data.next_after;
        else _pipelineLogAfter += evs.length;
        evs.forEach(function (ev) {
          var msg =
            (ev.timestamp || '') +
            ' ' +
            (ev.event_type || 'info') +
            ' | ' +
            (ev.step || '') +
            ': ' +
            (ev.message || '');
          // EXTERNAL_API = HTTP traces from monolith (Vertex/Kie/… via ContextVar).
          // COST = billed usage after each provider call (always emitted from wrapper).
          // Without treating COST as EXT, "External APIs only" hid all real LLM/API activity
          // whenever workers did not forward EXTERNAL_API (ThreadPoolExecutor + ContextVar).
          var step = (ev.step || '');
          var isExt = step === 'EXTERNAL_API' || step === 'COST';
          window.StudioAPILog.append({
            method: isExt ? 'EXT' : 'PIPE',
            path: String(jid).slice(0, 8) + '…',
            bodySummary: msg,
            status: 200,
            responseSummary: ev.progress >= 0 ? 'p' + ev.progress : ''
          });
          try {
            window._studioLastPipelineLine = msg;
          } catch (e0) {}
        });
      })
      .catch(function () {});
  }

  function applyIntermediates(intermediates, output, jobRef) {
    var im = intermediates || {};
    var out = output || {};
    var jref = jobRef || {};
    var jobDone = jref.status === 'completed';
    im.scene_videos = studioEffectiveSceneVideos(im, out);
    if (!im.vo_script && out.vo_script) im.vo_script = out.vo_script;
    if (!im.scene_images && out.scene_images) im.scene_images = out.scene_images;
    if (!im.music_url && out.music_url) im.music_url = out.music_url;
    if (!im.parsed_texts && out.parsed_texts) im.parsed_texts = out.parsed_texts;
    if (!im.music_description && out.music_description) im.music_description = out.music_description;
    if (!im.scene_plan && out.scene_plan) im.scene_plan = out.scene_plan;
    if (!im.nine_cell_plan && out.nine_cell_plan) im.nine_cell_plan = out.nine_cell_plan;
    if (!im.grid_manifests && out.grid_manifests) im.grid_manifests = out.grid_manifests;
    if (!im.scene_grids && out.scene_grids) im.scene_grids = out.scene_grids;
    if (!im.grid_image_url && out.grid_image_url) im.grid_image_url = out.grid_image_url;
    if (!im.ugc_grid_cut_from_url && out.ugc_grid_cut_from_url) im.ugc_grid_cut_from_url = out.ugc_grid_cut_from_url;
    if (!im.frame_classifications && out.frame_classifications) im.frame_classifications = out.frame_classifications;
    if (!im.frame_routing && out.frame_routing) im.frame_routing = out.frame_routing;
    if (!im.ad_context && out.ad_context) im.ad_context = out.ad_context;
    if (!im.scene_video_plan && out.scene_video_plan) im.scene_video_plan = out.scene_video_plan;
    if (!im.style_dna && out.style_dna) im.style_dna = out.style_dna;

    var _ugcPollJobId = String(jref.id || '').trim();
    var _ugcPrimaryJob = isUgcRealFlow() ? ugcRealPrimaryPipelineJobId() : '';
    if (isUgcRealFlow()) {
      var _p1Bind = phase1JobId && String(phase1JobId).trim();
      var _pollIsPhase1 = !!(_p1Bind && _ugcPollJobId === _p1Bind);
      if (_pollIsPhase1) {
        var _prevPhase1Binder = String(_studioUgcPhase1BinderId || '').trim();
        if (_prevPhase1Binder && _prevPhase1Binder !== _p1Bind) {
          studioUgcClearPhase1GridClientState();
          _ugcRealScenePlan = null;
          _ugcRealSceneGrids = [];
          _ugcRealGridManifests = [];
          _ugcRealFrameClassifications = [];
        }
        if (_prevPhase1Binder !== _p1Bind) {
          _studioUgcPhase1BinderId = _p1Bind;
        }
      }
    }
    var _imGridUrlTrim = im.grid_image_url != null ? String(im.grid_image_url).trim() : '';
    var _collGridUrlTrim =
      collectedIntermediates.grid_image_url != null ? String(collectedIntermediates.grid_image_url).trim() : '';
    var _ugcMasterImageChanged =
      !!(_imGridUrlTrim && _collGridUrlTrim && _imGridUrlTrim !== _collGridUrlTrim);
    if (
      isUgcRealFlow() &&
      _ugcPrimaryJob &&
      _ugcPollJobId === _ugcPrimaryJob &&
      (!Array.isArray(im.grid_cells) || im.grid_cells.length === 0) &&
      Array.isArray(collectedIntermediates.grid_cells) &&
      collectedIntermediates.grid_cells.length > 0 &&
      !_ugcMasterImageChanged
    ) {
      im.grid_cells = collectedIntermediates.grid_cells.slice();
    }

    // Drop cell crops that belong to a previous master grid (server provenance from monolith).
    if (isUgcRealFlow() && Array.isArray(im.grid_cells) && im.grid_cells.length > 0) {
      var _cutTrim = im.ugc_grid_cut_from_url != null ? String(im.ugc_grid_cut_from_url).trim() : '';
      if (_imGridUrlTrim && _cutTrim && _cutTrim !== _imGridUrlTrim) {
        im.grid_cells = [];
      }
    }

    if (isUgcRealFlow() && Array.isArray(im.grid_cells) && im.grid_cells.length > 0) {
      var ugcGridImgs = ugcRealOrderedSceneImagesFromGridCells(im.grid_cells);
      if (ugcGridImgs.length) {
        var base = Array.isArray(im.scene_images) ? im.scene_images.slice() : [];
        var nU = ugcGridImgs.length;
        while (base.length < nU) base.push('');
        for (var uix = 0; uix < nU; uix++) {
          var gU = ugcGridImgs[uix];
          var sU = base[uix];
          var sOk = sU && typeof sU === 'string' && sU.indexOf('http') === 0;
          var gOk = gU && typeof gU === 'string' && gU.indexOf('http') === 0;
          if (!sOk && gOk) base[uix] = gU;
        }
        im.scene_images = base;
      }
    }

    if (isUgcRealFlow()) {
      _ugcRealScenePlan = ugcRealGetStoryboardPlan(im) || _ugcRealScenePlan;
      _ugcRealSceneGrids = im.grid_image_url ? [im.grid_image_url] : (Array.isArray(im.scene_grids) ? im.scene_grids.slice() : _ugcRealSceneGrids);
      _ugcRealGridManifests = Array.isArray(im.grid_manifests) ? im.grid_manifests.slice() : _ugcRealGridManifests;
      _ugcRealFrameClassifications = Array.isArray(im.frame_routing) ? im.frame_routing.slice() : (Array.isArray(im.frame_classifications) ? im.frame_classifications.slice() : _ugcRealFrameClassifications);
      if (im.nine_cell_plan) collectedIntermediates.nine_cell_plan = im.nine_cell_plan;
      if (im.grid_manifests) collectedIntermediates.grid_manifests = im.grid_manifests;
      if (im.grid_image_url) collectedIntermediates.grid_image_url = im.grid_image_url;
      if (im.scene_grids) collectedIntermediates.scene_grids = im.scene_grids;
      if (im.ugc_grid_cut_from_url != null && String(im.ugc_grid_cut_from_url).trim()) {
        collectedIntermediates.ugc_grid_cut_from_url = im.ugc_grid_cut_from_url;
      }
      if (im.grid_cells) collectedIntermediates.grid_cells = im.grid_cells;
      if (im.frame_routing) collectedIntermediates.frame_routing = im.frame_routing;
      if (im.frame_classifications) collectedIntermediates.frame_classifications = im.frame_classifications;
      if (im.scene_video_plan) collectedIntermediates.scene_video_plan = im.scene_video_plan;
      if (im.style_dna) collectedIntermediates.style_dna = im.style_dna;
      try {
        delete collectedIntermediates.scene_plan;
      } catch (eScenePlan) {}
      var stepName = (jref.current_step || '').trim();
      var pausedUgc = jref.status === 'paused';
      var planUgcForCells = ugcRealGetStoryboardPlan(im);
      var hasNineCellCells = !!planUgcForCells;
      var showUgcPrefsPipelineStatus =
        currentStep === studioPrefsStep() &&
        (stepName === 'step_parse' ||
          stepName === 'step_1' ||
          stepName === 'step_2' ||
          (!pausedUgc && (stepName === 'step_0' || stepName === 'step_0.5')));
      renderUgcRealSceneReview(planUgcForCells, im.creative_strategy, im.narrative_plan, {
        pipelineStep: stepName,
        showCard: isUgcRealFlow() && currentStep === studioUgcNineStep(),
      });
      renderUgcRealGridReview(planUgcForCells, im.grid_image_url, im.grid_cells, im.frame_routing || im.frame_classifications);
      var ugcSceneStatus = document.getElementById('ugcRealSceneReviewStatus');
      var ugcGridStatus = document.getElementById('ugcRealGridReviewStatus');
      var offerStatusEl = document.getElementById('ugcRealOfferStepStatus');
      if (offerStatusEl) {
        if (isUgcRealFlow() && currentStep === studioPrefsStep() && showUgcPrefsPipelineStatus) {
          offerStatusEl.style.display = 'block';
          if (stepName === 'step_parse' && pausedUgc) {
            offerStatusEl.textContent =
              'Offer text is ready below. Edit if needed, then click Approve offer text & continue. When the nine-cell plan is ready, use Next to open the storyboard step.';
          } else if (stepName === 'step_parse' && !pausedUgc) {
            offerStatusEl.textContent =
              'Parsing your brief into audience, problem, and benefits + CTA. Approve the three fields when they look right — cell-level prompts are created on the next steps.';
          } else if (stepName === 'step_1' || stepName === 'step_2') {
            offerStatusEl.textContent = hasNineCellCells
              ? 'Nine-cell plan is ready. Click Next to open the storyboard step and review each cell.'
              : 'Building the nine-cell plan from your main prompt and the three offer fields…';
          } else {
            offerStatusEl.textContent =
              'Pipeline update: when the storyboard is ready, use Next to open the nine-cell step.';
          }
        } else {
          offerStatusEl.style.display = 'none';
        }
      }
      if (ugcSceneStatus) {
        ugcSceneStatus.style.display = 'none';
      }
      refreshUgcNineCellActivityPanel(jref, im, hasNineCellCells);
      try {
        refreshUgcPostGridPipelinePanel(jref);
      } catch (ePostGrid) {}
      try {
        refreshUgcRealApproveGridsButton(jref, im);
      } catch (eApprBtn) {}
      if (ugcGridStatus) {
        var hasGridPayload =
          !!(im.grid_image_url && String(im.grid_image_url).trim()) ||
          (Array.isArray(im.grid_cells) && im.grid_cells.length > 0) ||
          (Array.isArray(im.frame_routing) && im.frame_routing.length > 0) ||
          (Array.isArray(_ugcRealFrameClassifications) && _ugcRealFrameClassifications.length > 0);
        ugcGridStatus.style.display = stepName === 'step_5' || currentStep === studioVoStep() ? 'block' : 'none';
        if (stepName === 'step_5') {
          ugcGridStatus.textContent =
            'Grid and routing are ready. Approve to start the next segment (VO audio, lip-sync, then nine animated clips). On this step you can Regenerate or Fix each cell image; Background music is next, then Final video.';
        } else if (currentStep === studioVoStep() && hasGridPayload) {
          ugcGridStatus.textContent =
            'Grid data is loaded below (cells + routing). Use Regenerate / Fix on each cell as needed, then continue to Background music → Final video.';
        } else {
          ugcGridStatus.textContent = 'Waiting for UGC Real grids and frame routing…';
        }
      }
      var gatePipe = document.getElementById('ugcCharGatePipelineStatus');
      if (gatePipe && currentStep === studioUgcCharGateStep() && phase1JobId && String(jref.id || '') === String(phase1JobId)) {
        gatePipe.style.display = 'block';
        var stG = jref.status || '';
        if (stG === 'paused' && stepName === 'step_parse') {
          gatePipe.textContent =
            'Phase 1 paused after brief parse — edit offer fields on step 7 if needed, then use Next to reach this screen. Nine-cell planning starts in the background when you open this step.';
        } else if (stG === 'paused' && stepName === 'step_2') {
          gatePipe.textContent =
            'Paused before the 3×3 master grid. Approve the character (Approve & use in pipeline), then resume Phase 1 from Preferences so Nano Banana gets the final portrait URL.';
        } else if (stG === 'processing') {
          gatePipe.textContent =
            'Background: ' +
            (stepName || 'running') +
            ' — offer analysis / nine-cell run while you review the portrait. When the storyboard is ready, use Next to open the Nine-cell step.';
        } else {
          gatePipe.textContent = 'Phase 1: ' + stG + (stepName ? ' (' + stepName + ')' : '') + '.';
        }
      }
      if (jref.status === 'paused' && stepName === 'step_parse' && currentStep < studioPrefsStep()) {
        goToStep(studioPrefsStep());
      } else if (jref.status === 'paused' && (stepName === 'step_1' || stepName === 'step_2') && currentStep < studioPrefsStep()) {
        goToStep(studioPrefsStep());
      } else if (
        jref.status === 'paused' &&
        (stepName === 'step_1' || stepName === 'step_2') &&
        hasNineCellCells &&
        (currentStep === studioUgcCharGateStep() || currentStep === studioUgcNineStep()) &&
        isUgcRealFlow()
      ) {
        goToStep(studioUgcNineStep());
      } else if (jref.status === 'paused' && stepName === 'step_5' && currentStep < studioVoStep()) {
        goToStep(studioVoStep());
      } else if (
        !isUgcRealFlow() &&
        jref.status === 'paused' &&
        stepName === 'step_8' &&
        currentStep >= studioImagesStep() &&
        currentStep < studioFinalStep()
      ) {
        goToStep(studioFinalStep());
      }
    }
    var parsed = im.parsed_texts;
    if (parsed && typeof parsed === 'object') {
      var t1 = parsed.text_1, t2 = parsed.text_2, t3 = parsed.text_3;
      var el1 = document.getElementById('text1'), el2 = document.getElementById('text2'), el3 = document.getElementById('text3');
      function _setIfDiff(el, next) {
        if (!el || next == null) return;
        if (el.value !== next) el.value = next;
      }
      if (el1 && t1 != null) _setIfDiff(el1, typeof t1 === 'string' ? t1 : (Array.isArray(t1) ? t1.join('\n') : JSON.stringify(t1)));
      if (el2 && t2 != null) _setIfDiff(el2, typeof t2 === 'string' ? t2 : (Array.isArray(t2) ? t2.join('\n') : JSON.stringify(t2)));
      if (el3 && t3 != null) _setIfDiff(el3, typeof t3 === 'string' ? t3 : (Array.isArray(t3) ? t3.join('\n') : (typeof t3 === 'object' ? JSON.stringify(t3, null, 2) : String(t3))));
    }
    try {
      updateGenerateVOButtonState();
    } catch (eGV) {}
    var voRaw = im.vo_script;
    var voGenEl = document.getElementById('voScriptGenerating');
    if (voRaw != null && String(voRaw).trim() !== '') {
      if (voGenEl) voGenEl.style.display = 'none';
      var voEl = document.getElementById('voScript');
      var voIncoming = typeof voRaw === 'string' ? voRaw : String(voRaw);
      var voChanged = voEl && voEl.value !== voIncoming;
      if (voEl && voChanged) voEl.value = voIncoming;
      var words = voIncoming.trim().split(/\s+/).filter(Boolean).length;
      var wc = document.getElementById('voWordCount');
      if (wc) wc.textContent = words || 0;
      var durEl = document.getElementById('voDuration');
      if (durEl) durEl.textContent = im.vo_duration != null ? Math.round(im.vo_duration) : Math.round((words || 1) / 2.5);
      try {
        collectedIntermediates.vo_script = voIncoming;
      } catch (eVo) {}
      /* Only refresh voice UI when the script text actually changes — polling every ~2s was re-firing /api/voice-design and caused 502 storms. */
      if (currentStep === studioVoStep() && voEl && voIncoming.trim() && voChanged) refreshStep7VoiceBlock();
    } else if (currentStep === studioVoStep()) {
      /* Script not yet available — show spinner if job is actively running */
      var jbStatus = out && out.status;
      if (voGenEl && (jbStatus === 'processing' || jbStatus === 'pending')) {
        voGenEl.style.display = 'flex';
      }
    }
    if (im.vo_audio_url) {
      _voGeneratedForApprove = true;
      var voAudio = document.getElementById('voAudio');
      var voWrap = document.getElementById('voAudioPlayerWrap');
      var sameVoUrl = voAudio && (voAudio.src === im.vo_audio_url || (voAudio.src && voAudio.src.indexOf(im.vo_audio_url) !== -1));
      var voPlaying = voAudio && !voAudio.paused && voAudio.currentTime > 0;
      if (voAudio && !sameVoUrl && !voPlaying) voAudio.src = im.vo_audio_url;
      if (voWrap) voWrap.style.display = 'block';
    }
    var musicUrl = im.music_url || out.music_url;
    if (musicUrl) {
      var ma = document.getElementById('musicAudio');
      var sameMusicUrl = ma && (ma.src === musicUrl || (ma.src && ma.src.indexOf(musicUrl) !== -1));
      var musicPlaying = ma && !ma.paused && ma.currentTime > 0;
      if (ma && !sameMusicUrl && !musicPlaying) ma.src = musicUrl;
      var mp = document.getElementById('musicPlayer'); if (mp) mp.style.display = 'flex';
      var ms = document.getElementById('musicStatus'); if (ms) ms.textContent = 'Music ready.';
      if (currentStep === studioPrefsStep()) {
        updateStep6MusicPrefetchHint(
          'Background music is ready. Use Next to open the Background music step to preview and approve.'
        );
      }
    }
    if (im.music_description) {
      var md = document.getElementById('musicDescription');
      if (md && md.value !== im.music_description) md.value = im.music_description;
    }
    if (im.scene_prompts && Array.isArray(im.scene_prompts)) {
      im.scene_prompts = normalizeStudioScenePrompts(im.scene_prompts);
      window._scenePromptsForImages = im.scene_prompts;
      var spJson = JSON.stringify(im.scene_prompts);
      var grid = document.getElementById('scenePromptsGrid');
      if (grid && spJson !== _lastScenePromptsJson) {
        _lastScenePromptsJson = spJson;
        grid.innerHTML = '';
        im.scene_prompts.forEach(function (scene, i) {
          var card = document.createElement('div');
          card.className = 'studio-scene-prompt-card';
          var label = document.createElement('label');
          label.className = 'studio-label';
          label.textContent = 'Scene ' + (i + 1);
          card.appendChild(label);
          var firstLabel = document.createElement('span');
          firstLabel.className = 'studio-field-hint';
          firstLabel.textContent = 'First prompt';
          var ta1 = document.createElement('textarea');
          ta1.className = 'studio-textarea';
          ta1.rows = 2;
          ta1.placeholder = 'Scene image description';
          ta1.value = scene.first_prompt || '';
          ta1.dataset.sceneIndex = String(i);
          ta1.dataset.promptType = 'first';
          var secondLabel = document.createElement('span');
          secondLabel.className = 'studio-field-hint';
          secondLabel.textContent = 'Second prompt';
          var ta2 = document.createElement('textarea');
          ta2.className = 'studio-textarea';
          ta2.rows = 2;
          ta2.placeholder = 'Motion / second line';
          ta2.value = scene.second_prompt || '';
          ta2.dataset.sceneIndex = String(i);
          ta2.dataset.promptType = 'second';
          card.appendChild(firstLabel);
          card.appendChild(ta1);
          card.appendChild(secondLabel);
          card.appendChild(ta2);
          grid.appendChild(card);
        });
      }
      refreshScenePromptsStepUI(jobRef);
      try {
        collectedIntermediates.scene_prompts = im.scene_prompts;
      } catch (eSp) {}
    }
    if (isUgcRealFlow()) {
      var _plBridge = ugcRealGetStoryboardPlan(im);
      if (_plBridge && _plBridge.cells && _plBridge.cells.length >= 9) {
        window._scenePromptsForImages = _plBridge.cells.map(function (cell) {
          var fp = ugcRealCellVisualPrompt(cell) || '';
          return { first_prompt: fp, image_prompt: fp, second_prompt: '', motion_prompt: '' };
        });
      }
    }
    var scenePromptsCount = im.scene_prompts && Array.isArray(im.scene_prompts) ? im.scene_prompts.length : 0;
    var localPromptsCount =
      window._scenePromptsForImages && Array.isArray(window._scenePromptsForImages)
        ? window._scenePromptsForImages.length
        : 0;
    var effectivePromptCount = Math.max(scenePromptsCount, localPromptsCount);
    var ugcGridCellCount =
      isUgcRealFlow() && Array.isArray(im.grid_cells) ? im.grid_cells.length : 0;
    var ugcPlanForImageSlots = isUgcRealFlow() ? ugcRealGetStoryboardPlan(im) : null;
    var ugcNinePlanReady =
      !!(ugcPlanForImageSlots && ugcPlanForImageSlots.cells && ugcPlanForImageSlots.cells.length >= 9);
    if (im.scene_images || effectivePromptCount > 0 || ugcGridCellCount > 0 || ugcNinePlanReady) {
      var existing = (im.scene_images && (im.scene_images.slice ? im.scene_images.slice() : im.scene_images)) || [];
      var maxPinnedIndex = -1;
      for (var pik in _studioPinnedSceneImageByIndex) {
        if (!Object.prototype.hasOwnProperty.call(_studioPinnedSceneImageByIndex, pik)) continue;
        var _pi = parseInt(pik, 10);
        if (!isNaN(_pi) && _pi > maxPinnedIndex) maxPinnedIndex = _pi;
      }
      var targetLen = Math.max(
        existing.length,
        effectivePromptCount,
        (currentSceneImages && currentSceneImages.length) || 0,
        maxPinnedIndex + 1,
        ugcGridCellCount
      );
      // Never show more image slots than scene prompts (avoids orphan tiles when server list length drifted).
      // UGC Real: scene_prompts may be a single legacy slot while grid_cells has 9 — do not cap to 1.
      if (effectivePromptCount > 0 && !isUgcRealFlow()) {
        targetLen = Math.min(targetLen, effectivePromptCount);
      }
      if (isUgcRealFlow() && targetLen < 9) {
        var _plan9 = ugcPlanForImageSlots || ugcRealGetStoryboardPlan(im);
        var _nPlan = _plan9 && _plan9.cells ? _plan9.cells.length : 0;
        var _nColGrid =
          Array.isArray(collectedIntermediates.grid_cells) ? collectedIntermediates.grid_cells.length : 0;
        if (ugcGridCellCount >= 9 || _nPlan >= 9 || _nColGrid >= 9 || ugcNinePlanReady) {
          targetLen = Math.max(targetLen, 9);
        }
      }
      function _goodImageUrl(u) {
        return u && typeof u === 'string' && u.length > 5;
      }
      // Server often has no scene_images while job is paused (Studio "Generate images" runs client-side). Merge so polling never wipes local images.
      var shouldMergeSceneSlots =
        (currentSceneImages && currentSceneImages.length) || maxPinnedIndex >= 0;
      if (shouldMergeSceneSlots) {
        var merged = [];
        for (var mi = 0; mi < targetLen; mi++) {
          var fromSrv = existing[mi];
          var fromLoc = currentSceneImages[mi];
          var pin = _studioPinnedSceneImageByIndex[mi];
          var srvStripped =
            fromSrv && typeof fromSrv === 'string' ? stripStudioSceneImageCacheBuster(fromSrv) : '';
          if (pin && pin.stripped && srvStripped !== pin.stripped) {
            merged.push(pin.displayUrl);
            continue;
          }
          if (pin && pin.stripped && srvStripped === pin.stripped) {
            if (_goodImageUrl(fromLoc) && stripStudioSceneImageCacheBuster(fromLoc) === pin.stripped) {
              merged.push(fromLoc);
            } else if (_goodImageUrl(fromSrv)) {
              merged.push(fromSrv);
            } else {
              merged.push(pin.displayUrl);
            }
            delete _studioPinnedSceneImageByIndex[mi];
            continue;
          }
          if (_goodImageUrl(fromLoc)) merged.push(fromLoc);
          else if (_goodImageUrl(fromSrv)) merged.push(fromSrv);
          else if (fromLoc === false) merged.push(false);
          else merged.push(undefined);
        }
        existing = merged;
      }
      var sceneImagesMergedArr = null;
      if (targetLen > 0) {
        var arrMerge = [];
        for (var idxM = 0; idxM < targetLen; idxM++) {
          var evM = existing[idxM];
          arrMerge.push(evM === false ? false : _goodImageUrl(evM) ? evM : undefined);
        }
        _studioAccumulatedSceneImages = mergeMonotonicMediaSlots(targetLen, arrMerge, _studioAccumulatedSceneImages);
        sceneImagesMergedArr = _studioAccumulatedSceneImages.slice();
        collectedIntermediates.scene_images = sceneImagesMergedArr.slice();
        currentSceneImages = sceneImagesMergedArr.slice();
      }
      if (targetLen > 0 && typeof StudioMedia !== 'undefined' && sceneImagesMergedArr) {
        var arr = sceneImagesMergedArr;
        var errArr =
          window._sceneImageErrors && window._sceneImageErrors.length === arr.length
            ? window._sceneImageErrors
            : undefined;
        function _sceneSlotSig(u) {
          if (u === false) return '\0fail';
          if (u && typeof u === 'string') return u;
          return '';
        }
        var same =
          _lastSceneImagesRendered &&
          _lastSceneImagesRendered.length === arr.length &&
          arr.every(function (u, i) {
            return _sceneSlotSig(u) === _sceneSlotSig(_lastSceneImagesRendered[i]);
          });
        if (!same) {
          _lastSceneImagesRendered = arr.slice();
          if (!(isUgcRealFlow() && currentStep === studioVoStep())) {
            StudioMedia.renderSceneImages(arr, errArr);
          }
        }
        var step11Line = document.getElementById('step11ProgressLine');
        var _onSceneImagesStep = currentStep === studioImagesStep();
        var _onUgcVoGrid = isUgcRealFlow() && currentStep === studioVoStep();
        if (step11Line && (_onSceneImagesStep || _onUgcVoGrid)) {
          var doneCount = arr.filter(function (u) {
            return _goodImageUrl(u);
          }).length;
          var failCount = arr.filter(function (u) {
            return u === false;
          }).length;
          var line11 = doneCount + ' / ' + targetLen + ' scene images';
          if (failCount) line11 += ' (' + failCount + ' failed)';
          if (step11Line.textContent !== line11) step11Line.textContent = line11;
          step11Line.style.display = 'block';
        }
      }
    }
    if (im.scene_videos || (currentStep === studioFinalStep() && (im.scene_images || currentSceneImages.length))) {
      var sceneVidsRaw = Array.isArray(im.scene_videos) ? im.scene_videos : [];
      var targetLen = Math.max(
        sceneVidsRaw.length,
        (im.scene_images && im.scene_images.length) || 0,
        (currentSceneImages && currentSceneImages.length) || 0,
        scenePromptsCount || 0,
        1
      );
      var incomingVidSlots = [];
      for (var vi = 0; vi < targetLen; vi++) {
        var vv = vi < sceneVidsRaw.length ? sceneVidsRaw[vi] : undefined;
        if (vv === false) incomingVidSlots.push(false);
        else if (vv && typeof vv === 'string' && vv.length > 5) incomingVidSlots.push(vv);
        else incomingVidSlots.push(undefined);
      }
      _studioAccumulatedSceneVideos = mergeMonotonicMediaSlots(
        targetLen,
        incomingVidSlots,
        _studioAccumulatedSceneVideos
      );
      var arr = _studioAccumulatedSceneVideos.slice();
      collectedIntermediates.scene_videos = arr.slice();
      var sameVid = _lastSceneVideosRendered && _lastSceneVideosRendered.length === arr.length && arr.every(function (u, i) { return (_lastSceneVideosRendered[i] || '') === (u || ''); });
      if (typeof StudioMedia !== 'undefined' && !sameVid) {
        _lastSceneVideosRendered = arr.slice();
        StudioMedia.renderSceneVideos(arr);
      }
      var progressLine = document.getElementById('animationProgressLine');
      var progressExplain = document.getElementById('animationProgressExplainer');
      if (progressLine && targetLen > 0) {
        var done = arr.filter(function (u) { return u && (typeof u === 'string' ? u.length > 5 : true); }).length;
        var lineVid = done + ' / ' + targetLen + ' scene videos ready';
        if (progressLine.textContent !== lineVid) progressLine.textContent = lineVid;
        progressLine.style.display = 'block';
        if (progressExplain && currentStep === studioFinalStep()) {
          if (done === 0 && targetLen > 0) {
            var stepLab = (jref && jref.current_step) ? String(jref.current_step).trim() : '';
            progressExplain.style.display = 'block';
            progressExplain.textContent =
              'Scenes are animated in parallel (Vertex Veo). This counter only increases after each scene fully finishes—not when generation starts—so 0/' +
              targetLen +
              ' for 10–25+ minutes can be normal. "Animating…" on each card is a placeholder until a clip URL arrives. ' +
              (stepLab
                ? 'Server step: ' + stepLab + '. '
                : '') +
              'If it stays 0 for much longer than ~30 min per scene, check your API server logs and Studio API Log (PIPE).';
          } else {
            progressExplain.style.display = 'none';
            progressExplain.textContent = '';
          }
        } else if (progressExplain) {
          progressExplain.style.display = 'none';
          progressExplain.textContent = '';
        }
        try { refreshAnimationStartupPanel(jobRef, done, targetLen); } catch (eAsp) {}
      }
    }
    function _httpUrl(v) {
      if (!v || typeof v !== 'string') return '';
      v = v.trim();
      return v.length > 15 && v.indexOf('http') === 0 ? v : '';
    }
    function _firstUrl(obj, keys) {
      for (var ki = 0; ki < keys.length; ki++) {
        var u = _httpUrl(obj[keys[ki]]);
        if (u) return u;
      }
      return '';
    }
    var subtitledExplicit =
      _firstUrl(out, ['subtitled_video_url', 'subtitled_url']) ||
      _firstUrl(im, ['subtitled_video_url', 'subtitled_url']);
    var finalOut =
      _firstUrl(out, ['final_mp4_url', 'final_video_url']) || _firstUrl(im, ['final_video_url', 'final_mp4_url']);
    var beforeZap =
      _firstUrl(im, ['video_before_subtitles_url']) || _firstUrl(out, ['video_before_subtitles_url']);
    var rendiMix =
      _firstUrl(im, ['rendi_scene_voice_url', 'audio_mix_url']) ||
      _firstUrl(out, ['rendi_scene_voice_url', 'audio_mix_url']);
    var noSubsPlayer = beforeZap || rendiMix;
    var withSubsPlayer = subtitledExplicit;
    if (!withSubsPlayer && finalOut && noSubsPlayer) {
      if (String(finalOut).split('?')[0] !== String(noSubsPlayer).split('?')[0]) {
        withSubsPlayer = finalOut;
      }
    } else if (!withSubsPlayer && finalOut && !noSubsPlayer) {
      withSubsPlayer = finalOut;
    }
    var showNoSubs = !!(
      noSubsPlayer &&
      (!withSubsPlayer || String(noSubsPlayer).split('?')[0] !== String(withSubsPlayer).split('?')[0])
    );
    var showWithSubs = !!withSubsPlayer;
    var finalUrl = withSubsPlayer || noSubsPlayer;
    if (finalUrl) {
      var bust = jobDone ? '?studio=' + Date.now() : '';
      var fp = document.getElementById('finalVideoPlayer');
      var forceVideo = jobDone;
      var withSubsBlock = document.getElementById('finalVideoWithSubsBlock');
      if (withSubsBlock) withSubsBlock.style.display = showWithSubs ? 'block' : 'none';
      if (showWithSubs && fp) {
        var primaryBase = withSubsPlayer;
        var primary = primaryBase + (jobDone && primaryBase.indexOf('?') === -1 ? bust : '');
        var sameFinalUrl =
          fp.src &&
          (fp.src === primary ||
            fp.src.indexOf(String(primaryBase).split('?')[0]) !== -1);
        var finalPlaying = fp && !fp.paused && fp.currentTime > 0;
        if (forceVideo || (!sameFinalUrl && !finalPlaying)) {
          fp.src = primary.indexOf('?studio=') !== -1 ? primary : primaryBase;
          try {
            fp.load();
          } catch (e1) {}
        }
      } else if (fp) {
        try {
          fp.removeAttribute('src');
        } catch (eRm) {}
      }
      var fd = document.getElementById('finalVideoDownload');
      if (fd) {
        if (showWithSubs) {
          fd.setAttribute('data-download-url', withSubsPlayer);
          fd.setAttribute(
            'data-download-name',
            subtitledExplicit ? 'video-with-subtitles.mp4' : 'final-video.mp4'
          );
          fd.textContent = subtitledExplicit ? 'Download (with subtitles)' : 'Download final video';
          fd.style.display = 'inline-block';
        } else {
          fd.style.display = 'none';
        }
      }
      var openWith = document.getElementById('finalVideoOpenTab');
      if (openWith) {
        if (showWithSubs) {
          openWith.href = withSubsPlayer;
          openWith.style.display = 'inline-block';
        } else {
          openWith.style.display = 'none';
        }
      }
      var noSubsBlock = document.getElementById('finalVideoNoSubsBlock');
      var fpNo = document.getElementById('finalVideoNoSubsPlayer');
      var fdNo = document.getElementById('finalVideoNoSubsDownload');
      var openNo = document.getElementById('finalVideoNoSubsOpenTab');
      if (showNoSubs && noSubsPlayer && noSubsBlock && fpNo && fdNo) {
        noSubsBlock.style.display = 'block';
        var primaryNo = noSubsPlayer + (jobDone && noSubsPlayer.indexOf('?') === -1 ? bust : '');
        var sameNo =
          fpNo.src &&
          (fpNo.src === primaryNo ||
            fpNo.src.indexOf(String(noSubsPlayer).split('?')[0]) !== -1);
        if (forceVideo || (!sameNo && !(fpNo && !fpNo.paused && fpNo.currentTime > 0))) {
          fpNo.src = primaryNo.indexOf('?studio=') !== -1 ? primaryNo : noSubsPlayer;
          try {
            fpNo.load();
          } catch (e2) {}
        }
        fdNo.setAttribute('data-download-url', noSubsPlayer);
        fdNo.setAttribute('data-download-name', 'video-before-subtitles.mp4');
        if (openNo) {
          openNo.href = noSubsPlayer;
          openNo.style.display = 'inline-block';
        }
      } else if (noSubsBlock) {
        noSubsBlock.style.display = 'none';
        if (openNo) openNo.style.display = 'none';
      }
      var fr = document.getElementById('finalVideoResult');
      if (fr) fr.style.display = 'block';
      var fph = document.getElementById('finalVideoPlaceholder');
      if (fph) fph.style.display = 'none';
      var fs = document.getElementById('finalStatus');
      if (fs) {
        if (jobDone) {
          if (showWithSubs && showNoSubs) {
            fs.textContent =
              'Done — with subtitles in the first player; VO+music master (before burn-in) in the second.';
          } else if (showWithSubs) {
            fs.textContent = 'Done — video below (open in new tab if the player is blank).';
          } else {
            fs.textContent = 'Done — master without subtitles below (or open in a new tab).';
          }
        } else if (showNoSubs && !showWithSubs) {
          fs.textContent =
            'Master video (no burned-in subtitles) is ready below. If subtitles are on, they appear in the top player when ZapCap/GCS finishes.';
        } else if (showWithSubs && showNoSubs) {
          fs.textContent = 'Final with subtitles is ready; master without burn-in is in the second player.';
        } else if (showWithSubs) {
          fs.textContent = 'Video ready.';
        } else {
          fs.textContent = 'Video ready.';
        }
      }
      var fal = document.getElementById('finalAssemblyLive');
      if (fal) fal.style.display = 'none';
    }
    try {
      syncNonUgcCharacterPortraitFromJob(im, out, jref);
    } catch (eSyncPortrait) {}
  }

  function pollJob() {
    // Skip HTTP poll while SSE stream is live — SSE events trigger pollJob directly.
    if (_sseActive) return;
    if (currentStep === studioUgcNineStep() && isUgcRealFlow() && phase1JobId) {
      currentJobId = String(phase1JobId);
    } else if (currentStep === studioVoStep() && isUgcRealFlow()) {
      var ugcVoPollId = ugcRealPrimaryPipelineJobId();
      if (ugcVoPollId) currentJobId = String(ugcVoPollId);
    } else if (currentStep === studioVoStep() && phase2JobId) {
      currentJobId = String(phase2JobId);
    }
    if (currentStep === studioPrefsStep() && phase1JobId) {
      currentJobId = String(phase1JobId);
    }
    if (currentStep === studioUgcCharGateStep() && isUgcRealFlow() && phase1JobId) {
      currentJobId = String(phase1JobId);
    }
    if (currentStep >= studioWizardPhase3PollMinStep() && currentStep <= studioSubsStep()) {
      if (isUgcRealFlow()) {
        var ugcPollT = ugcRealPrimaryPipelineJobId();
        if (ugcPollT) currentJobId = String(ugcPollT);
      } else {
        var p3PollTarget = phase3JobForAnimate();
        if (p3PollTarget) currentJobId = String(p3PollTarget);
      }
    }
    if (
      isUgcRealFlow() &&
      (currentStep === studioVoStep() || currentStep === studioMusicStep())
    ) {
      var ugcVoMusicPoll = ugcRealPrimaryPipelineJobId();
      if (ugcVoMusicPoll) currentJobId = String(ugcVoMusicPoll);
    }
    if (
      !isUgcRealFlow() &&
      typeof currentStep === 'number' &&
      phase1JobId &&
      ((studioNonUgcCharacterStep() != null && currentStep === studioNonUgcCharacterStep()) ||
        (studioProductCharacterGateStep() != null && currentStep === studioProductCharacterGateStep()))
    ) {
      currentJobId = String(phase1JobId);
    }
    if (!currentJobId) return;
    var polledJobId = currentJobId;
    StudioAPI.getJob(polledJobId).then(function (job) {
      _lastPollJobSnapshot = job;
      var im = job.intermediates || {};
      var out = job.output || {};
      try {
        if (out && typeof out === 'object' && Object.keys(out).length > 0) {
          _studioLastJobOutput = JSON.parse(JSON.stringify(out));
        }
      } catch (eOut2) {}
      if (phase1JobId && String(polledJobId) === String(phase1JobId)) {
        _phase1LastPolledStatus = job.status || null;
        try { updateGenerateVOButtonState(); } catch (eP1s) {}
      }
      if (
        !isUgcRealFlow() &&
        phase2JobId &&
        String(polledJobId || '') === String(phase2JobId).trim() &&
        (job.status === 'failed' || job.status === 'aborted')
      ) {
        phase2JobId = null;
        _nonUgcVoPhase2KickoffForPhase1 = null;
        try {
          saveSession();
        } catch (eP2Fail) {}
        try {
          updateGenerateVOButtonState();
        } catch (eP2Btn) {}
      }
      _studioLinkedJobsMissing = false;
      updateStudioJobLinkBanner();
      if (job && job.id && String(job.id) === String(polledJobId) && currentStep >= studioWizardPhase3PollMinStep() && currentStep <= studioSubsStep()) {
        var polledJid = String(job.id).trim();
        if (isUgcRealFlow()) {
          phase3JobId = polledJid;
          _phase3AnimateJobId = polledJid;
        } else {
          var knownP3 = phase3JobId && String(phase3JobId).trim();
          var knownAnim = _phase3AnimateJobId && String(_phase3AnimateJobId).trim();
          if (!knownP3) {
            phase3JobId = polledJid;
            _phase3AnimateJobId = polledJid;
          } else if (polledJid === knownP3 || polledJid === knownAnim) {
            phase3JobId = knownP3;
            _phase3AnimateJobId = knownP3;
          }
        }
      }
      applyIntermediates(im, out, job);
      try {
        if (
          job.status === 'processing' &&
          !isUgcRealFlow() &&
          currentStep === studioFinalStep()
        ) {
          var stMerge =
            (im.subtitled_video_url && String(im.subtitled_video_url).trim()) ||
            (im.subtitled_url && String(im.subtitled_url).trim()) ||
            '';
          var jSid = String(job.id || '').trim();
          if (
            jSid &&
            stMerge.length > 15 &&
            stMerge.indexOf('http') === 0 &&
            window._studioSubsNavForJobId !== jSid
          ) {
            window._studioSubsNavForJobId = jSid;
            goToStep(studioSubsStep());
          }
        }
      } catch (eSubsNav) {}
      try {
        if (!isUgcRealFlow() && currentStep === studioPromptsStep()) {
          refreshScenePromptsStepUI(job);
        }
      } catch (eSpPoll) {}
      try {
        if (!isUgcRealFlow() && studioIsOnNonUgcCharacterGateStep()) {
          refreshNonUgcCharacterPortraitStatus();
          studioEnsureJobPollInterval();
        }
      } catch (ePortraitPoll) {}
      /* UGC Real: no separate Scene images step — do not auto-jump from VO/Music to Final on step_6/7/8. */
      // Pipeline + outbound provider lines (PIPE / EXT) for every active job — not only phase 3.
      if (job.status === 'processing' || job.status === 'paused') {
        appendPipelineEventsToLog(job);
      }
      if (job.status === 'processing' && _studioFinalAssemblyStarted) {
        _assemblyLogPollCounter++;
        if (_assemblyLogPollCounter % 2 === 0) appendJobLogsToApiLog(job.id);
        var fal = document.getElementById('finalAssemblyLive');
        var falt = document.getElementById('finalAssemblyLiveText');
        if (fal && falt) {
          fal.style.display = 'block';
          var last =
            (typeof window !== 'undefined' && window._studioLastPipelineLine) ||
            '(waiting for next server event…)';
          falt.innerHTML =
            '<strong>Live</strong> — step: <code>' +
            escapeLogHtml(job.current_step || '…') +
            '</code><br><span style="opacity:0.9">' +
            escapeLogHtml(last.slice(0, 320)) +
            '</span><br><small>Open <b>API Log</b> below for PIPE (pipeline) + LOG (server warnings) lines.</small>';
        }
      } else {
        var fal2 = document.getElementById('finalAssemblyLive');
        if (fal2 && job.status !== 'processing') fal2.style.display = 'none';
      }
      if (job.status === 'completed') {
        appendPipelineEventsToLog(job);
        appendJobLogsToApiLog(job.id);
        var doneHasVideo = !!(
          out.final_mp4_url ||
          out.final_video_url ||
          out.subtitled_video_url ||
          im.final_video_url ||
          im.subtitled_video_url ||
          im.subtitled_url ||
          im.video_before_subtitles_url ||
          im.rendi_scene_voice_url ||
          im.audio_mix_url ||
          out.video_before_subtitles_url
        );
        if (doneHasVideo && currentStep < studioSubsStep()) goToStep(studioFinalStep());
      }
      // Product: start background music as soon as TEXT 1–3 exist (step 6), or on the VO step (8) with Phase 2 — vo_script optional
      var vtMusic = typeof StudioSteps !== 'undefined' && StudioSteps.getVideoType ? StudioSteps.getVideoType() : '';
      var _hasTextsForMusic = studioHasTextsForMusic(im.parsed_texts);
      var onStep6Phase1 =
        vtMusic === 'product video' &&
        currentStep === studioPrefsStep() &&
        phase1JobId &&
        String(currentJobId) === String(phase1JobId);
      var onStep7Phase2 =
        currentStep === studioVoStep() && phase2JobId && String(currentJobId) === String(phase2JobId);
      if (
        vtMusic === 'product video' &&
        _hasTextsForMusic &&
        !im.music_url &&
        !_musicAutoStarted &&
        (onStep6Phase1 || onStep7Phase2)
      ) {
        _musicAutoStarted = true;
        if (onStep6Phase1) {
          updateStep6MusicPrefetchHint(
            'Generating background music in the background (Suno). It should be ready before you reach the Background music step.'
          );
        }
        StudioAPI.generateMusic(StudioSteps.collectMusicPayload())
          .then(function (data) {
            applyMusicGenerateResult(data);
            if (data.music_url || data.music_description) {
              return patchStudioMusicIntermediates(data);
            }
          })
          .catch(function () {
            _musicAutoStarted = false;
            if (onStep6Phase1) {
              updateStep6MusicPrefetchHint(
                'Could not start background music yet. You can use Regenerate on the Background music step, or wait and we will retry when you open the VO step.'
              );
            }
          });
      }
      var statusEl = document.getElementById('finalStatus');
      var placeholderText = document.getElementById('finalVideoPlaceholderText');
      if (statusEl) {
        if (job.status === 'completed') {
          var btnGfDone0 = document.getElementById('btnGenerateFinal');
          if (btnGfDone0) {
            btnGfDone0.style.display = 'none';
            btnGfDone0.disabled = false;
            btnGfDone0.removeAttribute('title');
          }
          var frDone = document.getElementById('finalVideoResult');
          if (!frDone || frDone.style.display !== 'block') {
            statusEl.textContent =
              'Job completed. If you do not see the video, open API Log or go to the Final video step.';
          }
        }
        else if (job.status === 'failed') {
          var btnGfFail = document.getElementById('btnGenerateFinal');
          if (btnGfFail) {
            btnGfFail.style.display = 'none';
            btnGfFail.disabled = false;
            btnGfFail.removeAttribute('title');
          }
          statusEl.textContent = 'Job failed: ' + (job.error || 'unknown error');
        }
        else if (job.status === 'processing' && currentStep === studioFinalStep() && studioScenesFinalPollMatch()) {
          var sceneVideos = studioSceneVideosForProgress(im, out);
          var imgLen = (currentSceneImages && currentSceneImages.length) || (im.scene_images && im.scene_images.length) || 0;
          var sceneCount = Math.max(imgLen, Array.isArray(sceneVideos) ? sceneVideos.length : 0, 1);
          var doneCount = sceneVideos.filter(function (u) { return u && (typeof u === 'string' ? u.length > 5 : true); }).length;
          if (doneCount < sceneCount) {
            var stepHint = (job.current_step || '').trim();
            if (doneCount === 0) {
              statusEl.textContent = 'Animating… 0/' + sceneCount + ' — first scene(s) often take 10–25 min (Veo/Vertex). Not stuck unless the job fails.';
              if (placeholderText) {
                placeholderText.textContent = 'Each finished scene appears below as soon as it is ready. Until then: the pipeline is generating video API calls (slow on first clips). ' + (stepHint ? 'Server step: ' + stepHint + '.' : '') + ' Check API Log / server logs if this exceeds ~30 min per scene.';
              }
            } else {
              statusEl.textContent = 'Animating scene ' + (doneCount + 1) + ' of ' + sceneCount + '… ' + stepHint;
              if (placeholderText) placeholderText.textContent = 'Animating images to video… ' + doneCount + '/' + sceneCount + ' scenes done. ' + stepHint;
            }
          } else if (_studioFinalAssemblyStarted) {
            statusEl.textContent = 'Assembling final video (Rendi)… ' + (job.current_step || '').trim();
            if (placeholderText) placeholderText.textContent = 'Concat, VO, music, subtitles. This can take several minutes.';
          } else {
            statusEl.textContent = 'Finishing animation step… ' + (job.current_step || '').trim();
            if (placeholderText) {
              placeholderText.textContent =
                'All clips are in. You can click Approve even if status still shows Processing — the server syncs pause state when needed.';
            }
          }
          var btnGFp = document.getElementById('btnGenerateFinal');
          if (btnGFp) {
            if (_studioFinalAssemblyStarted) {
              btnGFp.style.display = 'none';
              btnGFp.disabled = false;
              btnGFp.removeAttribute('title');
            } else if (studioStep12AllClipsReady(im, out)) {
              btnGFp.style.display = 'inline-block';
              // Approve calls PATCH + resume; server accepts resume when DB is still "processing"
              // if all scene_videos are present (mark_paused may have failed on flaky Supabase).
              btnGFp.disabled = false;
              btnGFp.setAttribute(
                'title',
                'All clips saved. Click to assemble the final video. If the job still shows Processing, the server will sync state.'
              );
            } else {
              btnGFp.style.display = 'none';
              btnGFp.disabled = false;
              btnGFp.removeAttribute('title');
            }
          }
        } else if (job.status === 'processing') statusEl.textContent = 'Processing... step: ' + (job.current_step || '');
        else if (job.status === 'paused') {
          if (currentStep === studioFinalStep() && studioScenesFinalPollMatch() && studioAnimationsPausedForReview(job)) {
            statusEl.textContent =
              'All scene clips are ready. Review each video below, then click to assemble the final video (Rendi concat, VO, music).';
            if (placeholderText) {
              placeholderText.textContent =
                'Use Re-animate if a clip needs a fix. When satisfied, approve to continue — the pipeline was waiting for you before Rendi.';
            }
            var btnGf = document.getElementById('btnGenerateFinal');
            if (btnGf) {
              btnGf.style.display = 'inline-block';
              btnGf.disabled = false;
              btnGf.removeAttribute('title');
              btnGf.textContent = 'Approve animations & assemble final video';
            }
          } else if (currentStep === studioFinalStep() && studioScenesFinalPollMatch()) {
            statusEl.textContent = 'Job is paused. If you just clicked Animate all, wait a few seconds for the status to update.';
          } else statusEl.textContent = 'Paused — review and continue.';
        } else statusEl.textContent = 'Status: ' + job.status;
      }
      var tryWrapAll = document.getElementById('tryAnimateAgainWrap');
      if (tryWrapAll && currentStep === studioFinalStep() && studioScenesFinalPollMatch()) {
        var svAll = studioSceneVideosForProgress(im, out);
        var imgLenAll = (currentSceneImages && currentSceneImages.length) || (im.scene_images && im.scene_images.length) || 0;
        var scAll = Math.max(imgLenAll, Array.isArray(svAll) ? svAll.length : 0, 1);
        var dcAll = svAll.filter(function (u) { return u && (typeof u === 'string' ? u.length > 5 : true); }).length;
        var assemblingNow =
          _studioFinalAssemblyStarted || !!(im.concat_url || out.final_mp4_url || im.final_video_url);
        var hideTryAll =
          job.status === 'completed' ||
          assemblingNow ||
          (job.status === 'processing' && dcAll >= scAll) ||
          (job.status === 'paused' && studioAnimationsPausedForReview(job));
        tryWrapAll.style.display = hideTryAll ? 'none' : 'block';
      }
      var retryAllWrap = document.getElementById('retryAllAnimationsWrap');
      if (retryAllWrap && currentStep === studioFinalStep() && studioScenesFinalPollMatch()) {
        var assemblingR =
          _studioFinalAssemblyStarted || !!(im.concat_url || out.final_mp4_url || im.final_video_url);
        retryAllWrap.style.display = job.status === 'completed' || assemblingR ? 'none' : 'block';
      }
      // Product: same job paused after scene prompts (step_3) while user still on VO step — move to scene prompts UI.
      var vtNav = typeof StudioSteps !== 'undefined' && StudioSteps.getVideoType ? StudioSteps.getVideoType() : '';
      if (
        job.status === 'paused' &&
        im.scene_prompts &&
        Array.isArray(im.scene_prompts) &&
        im.scene_prompts.length > 0 &&
        phase2JobId &&
        String(job.id) === String(phase2JobId) &&
        currentStep === studioVoStep() &&
        vtNav === 'product video'
      ) {
        phase3JobId = String(job.id).trim();
        _phase3AnimateJobId = phase3JobId;
        currentJobId = phase3JobId;
        goToStep(studioAssetsStep());
      }
      // Step 6 (Preferences): show that TEXT 1/2/3 are being generated (Phase 1 job — not portrait prefetch)
      var step6Wrap = document.getElementById('step6LiveStatus');
      var step6Text = document.getElementById('step6LiveStatusText');
      var step6Step = document.getElementById('step6LiveStatusStep');
      var step6SamePhase1 =
        currentStep === studioPrefsStep() &&
        phase1JobId &&
        String(polledJobId || '') === String(phase1JobId);
      if (step6Wrap && step6Text && step6Step && step6SamePhase1) {
        var hasParsed = parsedTextsHaveBody(im.parsed_texts);
        var ugcStep6 = vtNav === 'ugc-real';
        var planUgcS6 = ugcRealGetStoryboardPlan(im);
        var ugcHasStoryboardCellsS6 = !!planUgcS6;
        function ugcPhase1StepHint(cs) {
          if (cs === 'step_parse') return 'Parsing your prompt into audience, problem, benefits & CTA (LLM).';
          if (cs === 'step_0') return 'Offer analysis…';
          if (cs === 'step_0.5') return 'Creative strategy…';
          if (cs === 'step_1') return 'Nine-cell storyboard (9 cells: visual prompt, VO, lip-sync flags). Review the card when the job pauses.';
          if (cs === 'step_2') return 'Style DNA for a consistent 3×3 grid…';
          if (cs === 'queued' || !cs) return 'Pipeline starting — brief parse (step_parse) runs first.';
          return 'Current step: ' + cs + ' — the three fields below appear after step_parse finishes.';
        }
        if (ugcStep6 && (job.status === 'pending' || job.status === 'queued') && !hasParsed) {
          step6Wrap.style.display = 'block';
          step6Wrap.classList.add('studio-step7-running');
          step6Wrap.classList.remove('studio-status-success', 'studio-status-error');
          step6Text.textContent = 'Phase 1 is queued — waiting for a worker…';
          step6Step.textContent =
            'UGC Real runs step_parse first (not product parse_prompt), then offer analysis, creative strategy, and nine-cell planning. This page polls every 2s.';
          step6Step.style.display = 'block';
        } else if (ugcStep6 && job.status === 'processing' && !hasParsed) {
          step6Wrap.style.display = 'block';
          step6Wrap.classList.add('studio-step7-running');
          step6Wrap.classList.remove('studio-status-success', 'studio-status-error');
          var csUgc = (job.current_step || '').trim();
          step6Text.textContent = 'Phase 1 running — filling structured offer text from your prompt…';
          step6Step.textContent = ugcPhase1StepHint(csUgc);
          step6Step.style.display = 'block';
        } else if (!ugcStep6 && (job.status === 'pending' || job.status === 'queued') && !hasParsed) {
          step6Wrap.style.display = 'block';
          step6Wrap.classList.add('studio-step7-running');
          step6Wrap.classList.remove('studio-status-success', 'studio-status-error');
          step6Text.textContent = 'Job is queued — waiting for a worker to pick it up…';
          step6Step.textContent =
            'TEXT 1–3 are produced when the pipeline runs parse_prompt (LLM). Usually ~15–45s after status changes to “processing”. This page polls every 2s.';
          step6Step.style.display = 'block';
        } else if (!ugcStep6 && job.status === 'processing' && !hasParsed) {
          step6Wrap.style.display = 'block';
          step6Wrap.classList.add('studio-step7-running');
          step6Wrap.classList.remove('studio-status-success', 'studio-status-error');
          step6Text.textContent = 'Generating Headline, Key message and Call to action from your prompt (parse_prompt / LLM)…';
          var cs = (job.current_step || '').trim();
          step6Step.textContent =
            cs === 'parse_prompt'
              ? 'Gemini (or configured text model) is parsing your prompt — typically ~15–45s. Watch API Log → PIPE for this job.'
              : cs === 'queued' || !cs
                ? 'Pipeline starting — parse step runs next.'
                : 'Current step: ' + cs + ' — TEXT 1–3 appear after parse_prompt completes.';
          step6Step.style.display = 'block';
        } else if (hasParsed) {
          step6Wrap.style.display = 'block';
          step6Wrap.classList.remove('studio-step7-running');
          step6Wrap.classList.add('studio-status-success');
          step6Text.textContent = ugcStep6
            ? ugcHasStoryboardCellsS6
              ? 'Offer text is ready below. Edit if needed, then use Next to open the Nine-cell storyboard step. There you continue for the 3×3 grid when ready.'
              : 'Offer text is ready below. Edit if needed, then click Approve offer text & continue — the job will use your main prompt plus these three fields to build the nine-cell plan (image prompt + VO per cell). When it is ready, use Next to open the storyboard step.'
            : 'Ready. Review and edit the texts below, then click Generate VO.';
          step6Step.style.display = 'none';
        } else if (job.status === 'failed') {
          step6Wrap.style.display = 'block';
          step6Wrap.classList.remove('studio-step7-running');
          step6Wrap.classList.add('studio-status-error');
          step6Text.textContent = 'Failed: ' + (job.error || 'unknown error');
          step6Step.style.display = 'none';
        } else if (job.status === 'paused') {
          step6Wrap.style.display = 'block';
          step6Wrap.classList.remove('studio-step7-running');
          if (hasParsed) {
            step6Wrap.classList.add('studio-status-success');
            step6Wrap.classList.remove('studio-status-error');
            var csPaused = (job.current_step || '').trim();
            if (ugcStep6 && csPaused === 'step_parse') {
              step6Text.textContent =
                'Phase 1 paused after filling the three fields from your prompt. Edit them if needed, then click Approve offer text & continue to run offer analysis and build the nine-cell storyboard (next pause).';
            } else if (ugcStep6 && csPaused === 'step_2') {
              step6Text.textContent =
                'Phase 1 paused after Style DNA — the next resume generates the master grid in Nano Banana. Approve your character on the Character step (patch the job), then click Approve offer text & continue here to resume.';
            } else if (ugcStep6) {
              step6Text.textContent =
                'Phase 1 paused after planning. Use Next to open the Nine-cell storyboard step, or edit the three offer fields here first.';
            } else {
              step6Text.textContent = 'Ready. Review and edit the texts below, then click Generate VO.';
            }
            step6Step.style.display = 'none';
          } else {
            step6Wrap.classList.remove('studio-status-success');
            step6Wrap.classList.add('studio-status-error');
            step6Text.textContent =
              'Job is paused but TEXT 1–3 are still empty in saved data. Run Start generation again from step 5 (or restart the API). Tip: clear the three text areas completely — spaces-only used to skip the AI parse step (fixed in latest server).';
            step6Step.style.display = 'none';
          }
        } else {
          step6Wrap.style.display = 'block';
          step6Text.textContent = 'Status: ' + job.status;
          step6Step.textContent = job.current_step ? 'Step: ' + job.current_step : '';
          step6Step.style.display = job.current_step ? 'block' : 'none';
        }
      } else if (step6Wrap && currentStep !== 6) {
        step6Wrap.style.display = 'none';
      }
      var step11Status = document.getElementById('step11Status');
      var step11ProgressLine = document.getElementById('step11ProgressLine');
      if (
        step11Status &&
        ((currentStep === studioImagesStep()) || (isUgcRealFlow() && currentStep === studioVoStep())) &&
        studioScenesFinalPollMatch()
      ) {
        var promptsCount = (im.scene_prompts && Array.isArray(im.scene_prompts)) ? im.scene_prompts.length : 0;
        if (job.status === 'failed') {
          step11Status.style.display = 'block';
          step11Status.className = 'studio-status studio-status-error';
          step11Status.textContent =
            'Job failed: ' +
            (job.error || 'unknown error') +
            '. You can try regenerating an image or use New video in the gallery to start again.';
          if (step11ProgressLine) { step11ProgressLine.style.display = 'none'; }
        } else if (job.status === 'processing') {
          var sceneImgs = (im.scene_images || out.scene_images || []);
          var done = sceneImgs.filter(function (u) { return u && (typeof u === 'string' ? u.length > 5 : true); }).length;
          var gcLen = Array.isArray(im.grid_cells) ? im.grid_cells.length : 0;
          var total = Math.max(promptsCount || 0, sceneImgs.length, (currentSceneImages && currentSceneImages.length) || 0, gcLen, 1);
          if (isUgcRealFlow()) {
            var ugcPlanSt = ugcRealGetStoryboardPlan(im);
            var planLen = ugcPlanSt && ugcPlanSt.cells ? ugcPlanSt.cells.length : 0;
            total = Math.max(total, 9, gcLen, planLen);
            var csSt = (job.current_step || '').trim();
            var ugcEarlyPipe =
              csSt === 'step_parse' ||
              csSt === 'step_0' ||
              csSt === 'step_0.5' ||
              csSt === 'step_1' ||
              csSt === 'step_2' ||
              csSt === 'step_3' ||
              csSt === 'step_4' ||
              csSt === 'step_5';
            var ugcClipPipe = csSt === 'step_6' || csSt === 'step_7' || csSt === 'step_8';
            step11Status.style.display = 'block';
            step11Status.className = 'studio-status studio-step7-running';
            if (ugcEarlyPipe) {
              step11Status.textContent =
                'UGC Real: the server is still on an earlier segment (' +
                (csSt || '…') +
                ' — grid / planning / master image), not per-scene /api/generate-scene-image. Stills load from saved grid_cells when the job catches up. Polling every 2s. Wrong counts here usually mean a stale job id — Phase 1 should be polled.';
              if (step11ProgressLine) {
                step11ProgressLine.textContent =
                  'Waiting for pipeline — server step ' + csSt + ' (not Studio batch image API).';
                step11ProgressLine.style.display = 'block';
              }
            } else if (ugcClipPipe) {
              step11Status.textContent =
                'UGC Real: server is generating clips / audio (' +
                csSt +
                '). Scene stills on this page are from the grid; use Final video for animation progress. Updates every 2s.';
              if (step11ProgressLine) {
                step11ProgressLine.textContent = done + ' / ' + total + ' still slots (from job) — check Final for clips.';
                step11ProgressLine.style.display = 'block';
              }
            } else {
              step11Status.textContent =
                'UGC Real: syncing job data… ' +
                done +
                '/' +
                total +
                ' still slots. ' +
                (csSt ? 'Server step: ' + csSt + '. ' : '') +
                'Grid stills are not the same as /api/generate-scene-image (product path). Page updates every 2 seconds.';
              if (step11ProgressLine) {
                step11ProgressLine.textContent = done + ' / ' + total + ' scene stills — updating…';
                step11ProgressLine.style.display = 'block';
              }
            }
          } else {
            total = Math.max(promptsCount || 0, sceneImgs.length, (currentSceneImages && currentSceneImages.length) || 0, 1);
            step11Status.style.display = 'block';
            step11Status.className = 'studio-status studio-step7-running';
            step11Status.textContent =
              'Generating scene images… ' +
              done +
              '/' +
              total +
              ' done. ' +
              (job.current_step ? 'Step: ' + job.current_step : '') +
              ' (each image ~30–90 sec). Page updates every 2 seconds.';
            if (step11ProgressLine) {
              step11ProgressLine.textContent = done + ' / ' + total + ' scene images — generating…';
              step11ProgressLine.style.display = 'block';
            }
          }
        } else if (job.status === 'paused' || job.status === 'completed') {
          step11Status.style.display = 'none';
          var gcPause = Array.isArray(im.grid_cells) ? im.grid_cells.length : 0;
          var ugcPlanPause = isUgcRealFlow() ? ugcRealGetStoryboardPlan(im) : null;
          var ugcHasNinePause = !!(ugcPlanPause && ugcPlanPause.cells && ugcPlanPause.cells.length >= 9);
          if (
            step11ProgressLine &&
            (promptsCount > 0 || gcPause > 0 || (isUgcRealFlow() && ugcHasNinePause))
          ) {
            var sceneImgsPaused = (im.scene_images || out.scene_images || []);
            var donePaused = sceneImgsPaused.filter(function (u) { return u && (typeof u === 'string' ? u.length > 5 : true); }).length;
            var totPause = Math.max(promptsCount, sceneImgsPaused.length, gcPause, isUgcRealFlow() ? 9 : 1);
            step11ProgressLine.textContent = donePaused + ' / ' + totPause + ' scene images';
            step11ProgressLine.style.display = 'block';
          }
        } else {
          step11Status.style.display = 'block';
          step11Status.className = 'studio-status';
          step11Status.textContent = 'Status: ' + job.status + (job.current_step ? ' — ' + job.current_step : '');
          if (step11ProgressLine) step11ProgressLine.style.display = 'block';
        }
      } else if (
        step11Status &&
        currentStep !== studioImagesStep() &&
        !(isUgcRealFlow() && currentStep === studioVoStep())
      ) {
        step11Status.style.display = 'none';
        if (step11ProgressLine) step11ProgressLine.style.display = 'none';
      }
      var liveWrap = document.getElementById('step7LiveStatus');
      var liveText = document.getElementById('step7LiveStatusText');
      var liveStep = document.getElementById('step7LiveStatusStep');
      if (liveWrap && currentStep !== studioVoStep()) {
        liveWrap.style.display = 'none';
      } else if (currentStep === studioVoStep() && liveWrap && liveText && liveStep) {
        var stepLabel = (job.current_step || '').trim();
        var progress = job.progress != null ? job.progress : 0;
        var voInJob = !!(im.vo_script && String(im.vo_script).trim().length > 0);
        var pauseAfterStep = (job.input_params && job.input_params.pause_after_step) || '';
        var ugcVtNav = vtNav === 'influencer' || vtNav === 'personal-brand';
        var hasPhase2Job = phase2JobId && String(phase2JobId).trim().length >= 8;
        if (ugcVtNav && !hasPhase2Job) {
          liveWrap.style.display = 'block';
          liveWrap.classList.remove('studio-step7-running', 'studio-status-success', 'studio-status-error');
          liveText.textContent =
            'For influencer / personal-brand, the VO script is created in **Phase 2** only. Go back to **Preferences (step 6)** and click **Generate VO** after TEXT 1–3 look right. The script appears here within about 15–45 seconds.';
          liveStep.textContent = 'Phase 1 stops after parsing; it does not generate a voiceover script.';
          liveStep.style.display = 'block';
        } else if (job.status === 'processing') {
          liveWrap.style.display = 'block';
          liveWrap.classList.remove('studio-status-success', 'studio-status-error');
          liveWrap.classList.add('studio-step7-running');
          var isEarlyStep = stepLabel === 'parse_prompt' || stepLabel === 'queued' || stepLabel === '' || stepLabel === 'starting';
          if (stepLabel === 'parse_prompt') {
            if (vtNav === 'product video') {
              liveText.textContent = 'Syncing Headline / Key message / CTA (from step 6 when continuing)…';
              liveStep.textContent = 'Usually a few seconds here. The voiceover script step runs next.';
            } else {
              liveText.textContent = 'Syncing your edited TEXT 1–3 into the Phase 2 job…';
              liveStep.textContent = 'Quick step — then talking points and the VO script LLM run.';
            }
          } else if (stepLabel === 'extract_highlights') {
            liveText.textContent = 'Sharpening talking points for your voiceover…';
            liveStep.textContent = 'Quick LLM pass — typically ~10–25 seconds.';
          } else if (stepLabel === 'clean_product_image') {
            liveText.textContent =
              'Cleaning / compositing the product reference image (this step often runs in parallel with the VO script LLM).';
            liveStep.textContent = voInJob
              ? 'Your script should already appear in the box above; you can edit it while this finishes.'
              : 'If Phase 1 already produced a clean product image, this pass should be quick or skipped — otherwise it runs here.';
          } else if (stepLabel === 'character_description') {
            liveText.textContent = 'Describing your on-screen character for the voiceover (one quick vision pass)…';
            liveStep.textContent = 'Needed for influencer / personal-brand Phase 2 when you skipped full media analysis.';
          } else if (stepLabel === 'vo_generation') {
            liveText.textContent = 'Writing your voiceover script…';
            liveStep.textContent = 'Main wait for this screen — typically ~15–45 seconds. Script is editable when it lands.';
          } else if (stepLabel === 'music') {
            liveText.textContent = 'Generating background music track…';
            liveStep.textContent = 'Runs after the script; often ~30–90s. VO text may already appear above while this finishes.';
          } else if (isEarlyStep) {
            liveText.textContent = 'Starting…';
            liveStep.textContent = stepLabel === 'queued' ? 'Job queued.' : 'First step running.';
          } else {
            liveText.textContent = progress >= 15 ? 'Running — ' + progress + '%' : 'Running…';
            liveStep.textContent = stepLabel ? 'Current step: ' + stepLabel : '';
          }
          liveStep.style.display = 'block';
        } else if (job.status === 'paused') {
          liveWrap.style.display = 'block';
          liveWrap.classList.remove('studio-step7-running');
          liveWrap.classList.remove('studio-status-success', 'studio-status-error');
          var ugcGridReviewPause =
            isUgcRealFlow() &&
            !voInJob &&
            (stepLabel === 'step_5' ||
              (Array.isArray(im.grid_cells) && im.grid_cells.length > 0) ||
              (Array.isArray(im.frame_routing) && im.frame_routing.length > 0));
          if (ugcGridReviewPause) {
            liveWrap.classList.add('studio-status-success');
            liveText.textContent =
              'Grid review pause: VO lines are per cell in the card below. The combined VO script is saved here only **after** you click Approve grids & continue (next segment runs TTS). An empty script box above is normal at this pause.';
            liveStep.textContent =
              'current_step: ' + (stepLabel || 'step_5') + ' — when ready, click Approve grids & continue.';
            liveStep.style.display = 'block';
          } else if (voInJob) {
            liveWrap.classList.add('studio-status-success');
            liveText.textContent =
              vtNav === 'product video'
                ? 'Script ready — edit if needed, then click Approve and continue. (Product: pipeline pauses again on step 10 for scene prompts before images.)'
                : 'Script ready — edit if needed, then click Approve and continue. Next: music, then scene prompts and images.';
            liveStep.style.display = 'none';
          } else {
            var p1 = phase1JobId && String(phase1JobId).trim();
            var p2 = phase2JobId && String(phase2JobId).trim();
            var jid = job && job.id ? String(job.id).trim() : '';
            var productVt = vtNav === 'product video';
            var pollingPhase1Only = productVt && !p2 && p1 && jid === p1;
            var phase2PausedBeforeVo =
              productVt &&
              p2 &&
              jid === p2 &&
              (pauseAfterStep === 'step_2' || stepLabel === 'clean_product_image');
            if (pollingPhase1Only) {
              liveWrap.classList.remove('studio-status-success', 'studio-status-error');
              liveText.textContent =
                'You are still polling the Phase 1 job, which always pauses after the clean product image — it does not include a voiceover script. Go back to step 6 and click Generate VO to start Phase 2; the script will show up here after the server saves it.';
              liveStep.textContent = 'Tip: Phase 1 pause_after_step is step_2 (not VO).';
              liveStep.style.display = 'block';
            } else if (phase2PausedBeforeVo) {
              liveWrap.classList.remove('studio-status-success', 'studio-status-error');
              liveText.textContent =
                'This Phase 2 job paused after the clean-product step before voiceover ran (wrong pause point for this screen). Click Approve and continue / Resume to continue, or start a new Generate VO from step 6. After updating the API server, new Phase 2 jobs should use pause_after_step step_2.7.';
              liveStep.textContent =
                'pause_after_step=' + (pauseAfterStep || '—') + ', current_step=' + (stepLabel || '—');
              liveStep.style.display = 'block';
            } else if (productVt && pauseAfterStep === 'step_2.7') {
              liveWrap.classList.remove('studio-status-success', 'studio-status-error');
              liveText.textContent =
                'Job is paused for VO review but vo_script is still empty in job data. Restart the API container / server so the latest intermediates merge runs, check the API log for merge errors, then try Resume or Generate VO again.';
              liveStep.textContent = stepLabel ? ('current_step: ' + stepLabel) : 'current_step: —';
              liveStep.style.display = 'block';
            } else if (vtNav === 'ugc-real') {
              liveWrap.classList.remove('studio-status-success', 'studio-status-error');
              liveText.textContent =
                'UGC Real job is paused. If you are past grid review, check API Log for the pause step. If you expected a full VO script here, resume the job or wait for the next poll — this message is not used for grid review (see green note when step_5 / grid data is present).';
              liveStep.textContent = stepLabel ? ('current_step: ' + stepLabel) : '';
              liveStep.style.display = stepLabel ? 'block' : 'none';
            } else {
              liveText.textContent =
                'Job is paused but no VO script was returned in job data yet. Wait for the next poll, or open the API log. If it stays empty, try Resume from the dashboard or re-run Generate VO from step 6.';
              liveStep.textContent = stepLabel ? ('Last step id: ' + stepLabel) : '';
              liveStep.style.display = stepLabel ? 'block' : 'none';
            }
          }
        } else if (job.status === 'completed' || job.status === 'failed') {
          liveWrap.style.display = 'block';
          liveWrap.classList.remove('studio-step7-running');
          liveWrap.classList.toggle('studio-status-error', job.status === 'failed');
          liveText.textContent = job.status === 'completed' ? 'Job completed.' : ('Failed: ' + (job.error || 'unknown'));
          liveStep.style.display = 'none';
        } else {
          liveWrap.style.display = 'block';
          liveWrap.classList.remove('studio-step7-running');
          liveText.textContent = 'Status: ' + job.status;
          liveStep.textContent = stepLabel ? 'Step: ' + stepLabel : '';
          liveStep.style.display = stepLabel ? 'block' : 'none';
        }
      }
      // Auto-stop interval when job is terminal and no SSE is open.
      if (!_sseActive && pollIntervalId) {
        var ts = job.status;
        if (ts === 'completed' || ts === 'failed' || ts === 'aborted') {
          clearInterval(pollIntervalId);
          pollIntervalId = null;
        }
      }
    }).catch(function (err) {
      if (polledJobId && isStudioJobNotFoundError(err)) {
        _studioLinkedJobsMissing = true;
        if (clearInvalidStudioJobId(polledJobId)) {
          try {
            saveSession();
          } catch (eSave2) {}
        }
        updateStudioJobLinkBanner();
      }
    });
  }

  function refreshSaveCharacterOnApproveVisibility() {
    var wu = document.getElementById('saveCharOnApproveWrapUgc');
    var wn = document.getElementById('saveCharOnApproveWrapNonUgc');
    if (!wu && !wn) return;
    var cloud =
      typeof StudioAuth !== 'undefined' &&
      StudioAuth.isCloudConfigured &&
      StudioAuth.isCloudConfigured();
    var authed = typeof StudioAuth !== 'undefined' && StudioAuth.isAuthEnabled && StudioAuth.isAuthEnabled();
    var hasHttp =
      typeof StudioSteps !== 'undefined' &&
      StudioSteps.hasHttpCharacterUrl &&
      StudioSteps.hasHttpCharacterUrl();
    var show = !!(cloud && authed && (hasHttp || _characterReviewPendingUrl));
    if (wu) wu.style.display = show ? 'block' : 'none';
    if (wn) wn.style.display = show ? 'block' : 'none';
  }

  /** Typed library name, else first line of Character look (same rules as approve + save). */
  function getCharacterLibraryNameForSave() {
    var nameEl = document.getElementById('characterLibraryName');
    var fromField = nameEl && nameEl.value ? String(nameEl.value).trim() : '';
    if (fromField) return fromField;
    try {
      var bf = (document.getElementById('characterBrief') || {}).value;
      if (bf && String(bf).trim()) return String(bf).trim().slice(0, 80);
    } catch (eB) {}
    return '';
  }

  /** Build POST /api/characters body from current slots, pending portrait, and name field. */
  function buildCharacterLibraryCreatePayload() {
    if (typeof StudioSteps === 'undefined' || !StudioSteps.getFormAndUploadSnapshot) return null;
    var urls = [];
    function tryPush(u) {
      if (u == null || typeof u !== 'string') return;
      var t = u.trim();
      if (t.length > 12 && /^https?:\/\//i.test(t) && urls.indexOf(t) === -1) urls.push(t);
    }
    try {
      (StudioSteps.getFormAndUploadSnapshot().character_urls || []).forEach(tryPush);
    } catch (e) {}
    try {
      if (StudioSteps.getPrimaryCharacterHttpUrl) tryPush(StudioSteps.getPrimaryCharacterHttpUrl());
    } catch (e1) {}
    try {
      if (StudioSteps.collectGeneratePayload) {
        var p = StudioSteps.collectGeneratePayload();
        if (p.character_url) tryPush(p.character_url);
        if (p.character_urls && p.character_urls.length) p.character_urls.forEach(tryPush);
      }
    } catch (e2) {}
    tryPush(_characterReviewPendingUrl);
    if (!urls.length) return null;
    var briefFull = '';
    try {
      var bf = (document.getElementById('characterBrief') || {}).value;
      if (bf && String(bf).trim()) briefFull = String(bf).trim();
    } catch (e2) {}
    var portraitPrompt = '';
    try {
      var hp = document.getElementById('portraitImagePromptHidden');
      if (hp && hp.value) portraitPrompt = String(hp.value).trim();
    } catch (e3) {}
    var name = getCharacterLibraryNameForSave() || 'Saved character';
    var pendNorm = _characterReviewPendingUrl ? String(_characterReviewPendingUrl).trim() : '';
    return {
      name: name,
      source_type: pendNorm && urls[0] === pendNorm ? 'generated' : 'uploaded',
      thumbnail: urls[0],
      reference_images: urls,
      default_language: StudioSteps.getLanguage ? StudioSteps.getLanguage() : 'en',
      character_dna: {
        gender: StudioSteps.getGender ? StudioSteps.getGender() : 'f',
        character_brief: briefFull || undefined,
        portrait_image_prompt: portraitPrompt || undefined
      },
      style_json: {
        video_type: StudioSteps.getVideoType ? StudioSteps.getVideoType() : '',
        visual_style: (document.getElementById('style') || {}).value || 'Auto'
      }
    };
  }

  function isSaveCharacterOnApproveChecked() {
    var a = document.getElementById('chkSaveCharacterOnApproveUgc');
    var b = document.getElementById('chkSaveCharacterOnApproveNonUgc');
    return !!((a && a.checked) || (b && b.checked));
  }

  function clearSaveCharacterOnApproveCheckboxes() {
    var a = document.getElementById('chkSaveCharacterOnApproveUgc');
    var b = document.getElementById('chkSaveCharacterOnApproveNonUgc');
    if (a) a.checked = false;
    if (b) b.checked = false;
  }

  /** Show "Save to library" only when the user has a real hosted character URL (not only a local file preview). */
  function updateCharacterLibrarySaveVisibility() {
    var saveWrap = document.getElementById('characterLibrarySaveWrap');
    if (!saveWrap) return;
    var cloud =
      typeof StudioAuth !== 'undefined' &&
      StudioAuth.isCloudConfigured &&
      StudioAuth.isCloudConfigured();
    var authed = typeof StudioAuth !== 'undefined' && StudioAuth.isAuthEnabled && StudioAuth.isAuthEnabled();
    var hasHttp =
      typeof StudioSteps !== 'undefined' &&
      StudioSteps.hasHttpCharacterUrl &&
      StudioSteps.hasHttpCharacterUrl();
    saveWrap.style.display = cloud && authed && hasHttp ? 'block' : 'none';
    refreshSaveCharacterOnApproveVisibility();
  }

  function refreshCharacterLibrarySelect() {
    var wrap = document.getElementById('characterLibraryWrap');
    var saveWrap = document.getElementById('characterLibrarySaveWrap');
    var sel = document.getElementById('characterLibrarySelect');
    var hint = document.getElementById('characterLibraryLoadHint');
    if (!wrap || !sel || typeof StudioAPI === 'undefined' || !StudioAPI.listCharacters) return;
    if (hint) {
      hint.style.display = 'none';
      hint.textContent = '';
    }
    var cloud =
      typeof StudioAuth !== 'undefined' &&
      StudioAuth.isCloudConfigured &&
      StudioAuth.isCloudConfigured();
    var authed = typeof StudioAuth !== 'undefined' && StudioAuth.isAuthEnabled && StudioAuth.isAuthEnabled();
    if (!cloud || !authed) {
      wrap.style.display = 'none';
      if (saveWrap) saveWrap.style.display = 'none';
      return;
    }
    if (_characterLibraryInFlight) return;
    _characterLibraryInFlight = StudioAuth.getUser()
      .then(function (u) {
        if (!u) {
          wrap.style.display = 'none';
          if (saveWrap) saveWrap.style.display = 'none';
          return Promise.reject(new Error('__no_user'));
        }
        wrap.style.display = 'block';
        return StudioAPI.listCharacters();
      })
      .then(function (list) {
        if (!sel) return;
        _characterLibraryById = {};
        sel.innerHTML = '<option value="">— Select saved character —</option>';
        if (!Array.isArray(list)) list = [];
        list.forEach(function (c) {
          if (!c || !c.character_id) return;
          _characterLibraryById[c.character_id] = c;
          var opt = document.createElement('option');
          opt.value = c.character_id;
          opt.textContent = c.name || c.character_id;
          sel.appendChild(opt);
        });
        try {
          var pend =
            typeof window !== 'undefined' && window._studioRestoreCharacterLibraryId
              ? String(window._studioRestoreCharacterLibraryId).trim()
              : '';
          if (pend && _characterLibraryById[pend]) {
            sel.value = pend;
            window._studioRestoreCharacterLibraryId = '';
            var rec0 = _characterLibraryById[pend];
            var urls0 = rec0.reference_images || [];
            var url0 = urls0[0] || rec0.thumbnail;
            if (url0 && StudioSteps.setPrimaryCharacterUrl) {
              StudioSteps.setPrimaryCharacterUrl(url0);
            }
            var dna0 = rec0.character_dna;
            if (dna0 && typeof dna0 === 'object' && dna0.character_brief) {
              var bEl0 = document.getElementById('characterBrief');
              if (bEl0) bEl0.value = String(dna0.character_brief);
            }
            if (dna0 && typeof dna0 === 'object' && dna0.portrait_image_prompt) {
              var hidP = document.getElementById('portraitImagePromptHidden');
              if (hidP) hidP.value = String(dna0.portrait_image_prompt);
            }
          }
        } catch (ePend) {}
        updateCharacterLibrarySaveVisibility();
        try {
          if (typeof StudioSteps !== 'undefined' && StudioSteps.applyStep4CharacterSourceUI) {
            StudioSteps.applyStep4CharacterSourceUI();
          }
        } catch (eApLib) {}
      })
      .catch(function (err) {
        if (err && err.message === '__no_user') return;
        _characterLibraryById = {};
        if (sel) {
          sel.innerHTML = '<option value="">— Select saved character —</option>';
        }
        if (wrap) wrap.style.display = 'block';
        if (hint) {
          hint.style.display = 'block';
          hint.textContent =
            'Could not load saved characters (' +
            (err && err.message ? String(err.message).slice(0, 220) : 'network or server error') +
            '). Check Account sign-in, API URL, and that migration 002_studio_characters.sql ran. Use Refresh to retry.';
        }
        if (saveWrap) updateCharacterLibrarySaveVisibility();
      })
      .finally(function () {
        _characterLibraryInFlight = null;
      });
  }

  function goToStep(stepNum) {
    if (stepNum < 1 || stepNum > studioTotalSteps()) return;
    if (isUgcRealFlow() && stepNum === 6) {
      stepNum = 7;
    }
    if (stepNum === 1) {
      _characterReviewPendingUrl = null;
      _characterApproved = false;
      _voGeneratedForApprove = false;
      _characterPrefetchGen++;
      _characterPrefetchPromise = null;
      try {
        clearTimeout(_characterPrefetchDebounceTimer);
      } catch (e) {}
      _characterPrefetchDebounceTimer = null;
    }
    document.querySelectorAll('.studio-step').forEach(function (el) {
      el.classList.remove('active');
    });
    const el = getStepEl(stepNum);
    if (el) {
      el.classList.add('active', 'studio-step-enter');
    }
    currentStep = stepNum;
    if (stepNum !== 4) {
      try {
        setCharacterPortraitGenStatus('', false);
      } catch (eClrPortraitHint) {}
    }
    if (stepNum === 4) {
      try {
        if (typeof StudioSteps !== 'undefined' && StudioSteps.applyStep4CharacterSourceUI) {
          StudioSteps.applyStep4CharacterSourceUI();
        }
        if (typeof StudioSteps !== 'undefined' && StudioSteps.refreshStep4CharacterExclusiveHint) {
          StudioSteps.refreshStep4CharacterExclusiveHint();
        }
      } catch (eS4h) {}
    }
    if (stepNum !== studioPrefsStep()) {
      updateStep6MusicPrefetchHint('');
    }
    if (stepNum >= studioWizardPhase3PollMinStep() && stepNum <= studioSubsStep()) {
      if (isUgcRealFlow()) {
        var ugcNav = ugcRealPrimaryPipelineJobId();
        if (ugcNav) currentJobId = String(ugcNav);
      } else {
        var p3Nav = phase3JobForAnimate();
        if (p3Nav) currentJobId = String(p3Nav);
      }
    }
    if (stepNum === studioUgcNineStep() && isUgcRealFlow()) {
      if (phase1JobId) currentJobId = String(phase1JobId);
      try {
        var _p1Nav = phase1JobId && String(phase1JobId).trim();
        var _snapJ = _lastPollJobSnapshot;
        var _snapMatch =
          _snapJ && _p1Nav && String(_snapJ.id || '').trim() === _p1Nav;
        var _imNav = {};
        if (_snapMatch && _snapJ.intermediates && typeof _snapJ.intermediates === 'object') {
          try {
            _imNav = JSON.parse(JSON.stringify(_snapJ.intermediates));
          } catch (eImN) {
            _imNav = Object.assign({}, _snapJ.intermediates);
          }
        }
        if (!ugcRealGetStoryboardPlan(_imNav) && collectedIntermediates && collectedIntermediates.nine_cell_plan) {
          _imNav = Object.assign({}, _imNav, { nine_cell_plan: collectedIntermediates.nine_cell_plan });
        }
        var _jobForPanel = _snapMatch ? _snapJ : null;
        if (!_jobForPanel && _p1Nav) {
          _jobForPanel = {
            id: _p1Nav,
            status: 'unknown',
            current_step: 'unknown',
            progress: 0,
            intermediates: _imNav
          };
        }
        var _planNav = ugcRealGetStoryboardPlan(_imNav);
        refreshUgcNineCellActivityPanel(_jobForPanel, _imNav, !!_planNav);
        if (_planNav) {
          _ugcRealScenePlan = _planNav;
          try {
            renderUgcRealSceneReview(_planNav, _imNav.creative_strategy, _imNav.narrative_plan, {
              pipelineStep: (_jobForPanel && String(_jobForPanel.current_step || '').trim()) || '',
              showCard: true
            });
          } catch (eRen) {}
        }
      } catch (eNineNav) {}
      try {
        pollJob();
      } catch (ePollNineNav) {}
    } else if (stepNum === studioVoStep() && isUgcRealFlow()) {
      var ugcVoNavId = ugcRealPrimaryPipelineJobId();
      if (ugcVoNavId) currentJobId = String(ugcVoNavId);
    } else if (stepNum === studioVoStep() && phase2JobId) {
      currentJobId = String(phase2JobId);
    } else if (stepNum === studioPrefsStep() && phase1JobId) {
      currentJobId = String(phase1JobId);
      primeStep6PreferencesBanner();
      try {
        pollJob();
      } catch (ePollPrime) {}
    } else if (studioIsNonUgcCharacterGateStepNum(stepNum) && phase1JobId) {
      currentJobId = String(phase1JobId);
      try {
        ensureNonUgcVoPhase2Kickoff();
      } catch (eKick) {}
      try {
        pollJob();
      } catch (ePollCh) {}
      var vtCh = typeof StudioSteps !== 'undefined' ? StudioSteps.getVideoType() : '';
      var isCharType2 = vtCh === 'influencer' || vtCh === 'personal-brand' || vtCh === 'product video';
      var userUp2 = typeof StudioSteps !== 'undefined' && StudioSteps.hasUploadedCharacter && StudioSteps.hasUploadedCharacter();
      var prodNoChar =
        vtCh === 'product video' &&
        typeof StudioSteps.isProductNoOnScreenCharacter === 'function' &&
        StudioSteps.isProductNoOnScreenCharacter();
      var noPh = document.getElementById('nonUgcCharNoPortraitHint');
      if (noPh) {
        if (isCharType2 && !userUp2 && !_characterReviewPendingUrl && !prodNoChar) {
          noPh.style.display = 'block';
        } else {
          noPh.style.display = 'none';
        }
      }
      if (isCharType2 && !userUp2 && _characterReviewPendingUrl && !prodNoChar) {
        showCharPreview(_characterReviewPendingUrl);
      } else if (!isCharType2 || userUp2 || prodNoChar) {
        hideCharPreview();
      }
      try {
        refreshNonUgcCharacterPortraitStatus();
      } catch (eRfGate) {}
      try {
        maybeKickVoiceDesignAfterCharacterReady();
      } catch (eVd) {}
    } else if (stepNum === studioUgcCharGateStep() && isUgcRealFlow() && phase1JobId) {
      currentJobId = String(phase1JobId);
      primeUgcCharGatePipelineBanner();
      try {
        pollJob();
      } catch (ePollGate) {}
      try {
        ugcRealMaybeStartNineCellBackgroundResume();
      } catch (eBgNine) {}
      try {
        maybeKickVoiceDesignAfterCharacterReady();
      } catch (eVd2) {}
    } else if (stepNum === studioFinalStep() && isUgcRealFlow()) {
      try {
        pollJob();
      } catch (ePollFinalUgc) {}
    } else if (stepNum === studioMusicStep() && isUgcRealFlow() && ugcRealPrimaryPipelineJobId()) {
      try {
        refreshUgcPostGridPipelinePanel(null, null);
      } catch (ePostGridNav) {}
      try {
        pollJob();
      } catch (ePollMusicNav) {}
    }
    if (stepNum === studioUgcCharGateStep() && isUgcRealFlow()) {
      refreshUgcCharGateUI();
      try {
        updateGenerateVOButtonState();
      } catch (eGvGate) {}
    } else if (stepNum === studioPrefsStep()) {
      try {
        updateGenerateVOButtonState();
      } catch (eGv2) {}
      hideCharPreview();
    }
    try {
      updateUgcRealStepVisibility();
      /* Voice block was hidden for all UGC steps in updateUgcRealStepVisibility; re-apply VO step wiring after that. */
      if (stepNum === studioVoStep()) refreshStep7VoiceBlock();
      if (!isUgcRealFlow() && studioIsNonUgcCharacterGateStepNum(stepNum)) {
        try {
          refreshNonUgcCharacterPortraitStatus();
        } catch (eRfAfterVis) {}
      }
    } catch (eUgcVis) {}
    saveSession();
    var _tot = studioTotalSteps();
    var _pf = document.getElementById('progressFill');
    if (_pf) _pf.style.width = (stepNum / _tot * 100) + '%';
    var _psl = document.getElementById('progressStepLabel');
    if (_psl) _psl.textContent = 'Step ' + stepNum + ' of ' + _tot;
    var _ppct = document.getElementById('progressPct');
    if (_ppct) _ppct.textContent = Math.round(stepNum / _tot * 100) + '%';
    try {
      if (currentJobId) studioEnsureJobPollInterval();
    } catch (ePollInt) {}

    if (stepNum === 4) {
      try {
        refreshCharacterLibrarySelect();
      } catch (eLib) {}
      try {
        refreshCharacterBriefAiSuggestions(false);
      } catch (eSug) {}
    }

    // Auto-start Phase 1 when user reaches step 4 (media) or 5 (models) — same job as Preferences later.
    // Product video: wait until at least one product image URL exists after upload so clean_product_image
    // runs here — not later on the VO step.
    if ((stepNum === 4 || stepNum === 5) && !_phase1AutoStarted && !phase1JobId && !isUgcRealFlow()) {
      var errEl = document.getElementById('step4PipelineError');
      if (errEl) {
        errEl.style.display = 'none';
        errEl.textContent = '';
      }
      var payload = StudioSteps.collectGeneratePayload();
      if (payload.prompt && payload.prompt.trim() && payload.duration && !payload.simulation) {
        StudioAPI.ensureCanCallProtectedApi()
          .then(function (ok) {
            if (!ok) {
              if (errEl) {
                errEl.style.display = 'block';
                errEl.textContent =
                  'Pipeline needs auth: open Account → Sign in, or paste API key (sk-tvd-...) in the header. ' +
                  'Cloud-only requires SUPABASE_SERVICE_ROLE_KEY on the server and one active api_tenants row (or STUDIO_FALLBACK_API_KEY). Go to step 5 and use Start generation after signing in.';
              }
              return Promise.reject(new Error('__studio_needs_credentials'));
            }
            return uploadPendingZones();
          })
          .then(function () {
            var afterUp = StudioSteps.collectGeneratePayload();
            if (
              afterUp.video_type === 'product video' &&
              (!afterUp.product_image_urls || !afterUp.product_image_urls.length)
            ) {
              return null;
            }
            _phase1AutoStarted = true;
            var p1 = StudioSteps.collectPhase1Payload();
            return StudioAPI.generate(p1);
          })
          .then(function (res) {
            if (!res || !res.job_id) {
              return;
            }
            if (errEl) {
              errEl.style.display = 'none';
              errEl.textContent = '';
            }
            phase1JobId = res.job_id;
            _phase1LastPolledStatus = null;
            currentJobId = res.job_id;
            collectedIntermediates = {};
            _musicAutoStarted = false;
            if (sseSource) { sseSource.close(); _sseActive = false; }
            pollJob();
            if (pollIntervalId) clearInterval(pollIntervalId);
            pollIntervalId = setInterval(pollJob, 2000);
            StudioAPI.connectSSE(currentJobId, function (eventType, data) {
              if (data.event_type === 'complete' || data.event_type === 'error' || data.event_type === 'abort') {
                if (pollIntervalId) clearInterval(pollIntervalId);
                pollIntervalId = null;
                _sseActive = false;
              } else if (data.event_type === 'pause') {
                _sseActive = false;
                if (!pollIntervalId) pollIntervalId = setInterval(pollJob, 2000);
              }
              pollJob();
            }).then(function (es) {
              sseSource = es;
              _sseActive = true;
            });
            try {
              updateGenerateVOButtonState();
            } catch (eP1Btn) {}
          })
          .catch(function (err) {
            _phase1AutoStarted = false;
            var msg = (err && err.message) ? String(err.message) : String(err);
            if (msg.indexOf('__studio_needs_credentials') >= 0) {
              return;
            }
            console.error('Phase 1 auto-start failed:', err);
            if (errEl) {
              errEl.style.display = 'block';
              if (msg.indexOf('Missing API key') >= 0) {
                errEl.textContent =
                  msg +
                  ' Then refresh this page (Ctrl+Shift+R), open Account → Sign in again if you use cloud login, and click Start generation from step 5.';
              } else {
                errEl.textContent =
                  'Could not start generation in the background: ' +
                  msg +
                  ' — Check Account sign-in, API key field, and server .env (SUPABASE_SERVICE_ROLE_KEY, api_tenants). Retry from step 5.';
              }
            }
          });
      }
    }

    // Product: auto-start background music when entering step 6 (TEXT 1–3) or VO step (8) if not already started
    if (
      !_musicAutoStarted &&
      StudioSteps.getVideoType() === 'product video' &&
      studioHasTextsForMusic(collectedIntermediates.parsed_texts)
    ) {
      var maEl = document.getElementById('musicAudio');
      var hasMusic =
        collectedIntermediates.music_url ||
        (maEl && maEl.src && String(maEl.src).length > 12 && maEl.src.indexOf('blob:') !== 0);
      if (!hasMusic) {
        if (stepNum === studioPrefsStep() && phase1JobId) {
          _musicAutoStarted = true;
          updateStep6MusicPrefetchHint(
            'Generating background music in the background (Suno). It should be ready before you reach the Background music step.'
          );
          StudioAPI.generateMusic(StudioSteps.collectMusicPayload())
            .then(function (data) {
              applyMusicGenerateResult(data);
              if (data.music_url || data.music_description) {
                return patchStudioMusicIntermediates(data);
              }
            })
            .then(function () {
              if (pollIntervalId) pollJob();
            })
            .catch(function () {
              _musicAutoStarted = false;
              updateStep6MusicPrefetchHint(
                'Could not start background music yet. Open the Background music step later and use Regenerate, or wait for the VO step to retry.'
              );
            });
        } else if (
          stepNum === studioVoStep() &&
          ((isUgcRealFlow() && phase1JobId) || phase2JobId)
        ) {
          _musicAutoStarted = true;
          StudioAPI.generateMusic(StudioSteps.collectMusicPayload())
            .then(function (data) {
              applyMusicGenerateResult(data);
              if (data.music_url || data.music_description) {
                return patchStudioMusicIntermediates(data);
              }
            })
            .then(function () {
              if (pollIntervalId) pollJob();
            })
            .catch(function () {
              _musicAutoStarted = false;
            });
        }
      }
    }

    // Phase 3 (scene prompts → pause at step_3) starts only when the user clicks
    // "Generate scene prompts" on step 9 — not when entering the music step. Auto-start here
    // caused full pipelines to run while the user was still on earlier steps.

    if (!isUgcRealFlow() && stepNum === studioAssetsStep()) refreshScenePromptsStepUI(null);
    if (!isUgcRealFlow() && stepNum === studioPromptsStep()) {
      try {
        refreshScenePromptsStepUI(_lastPollJobSnapshot);
      } catch (eSpGo) {}
    }
    if (stepNum === studioVoStep()) refreshStep7VoiceBlock();
    if (stepNum === 5 || stepNum === studioPrefsStep() || stepNum === studioVoStep()) preloadVoicesForStep7();
    /* Non–UGC: portrait POST only from step-4 Next (runCharacterPortraitOnStep4Next). UGC Real: optional prefetch on character gate. */
    if (stepNum === studioVoStep()) {
      var voGenEl2 = document.getElementById('voScriptGenerating');
      var voElNow = document.getElementById('voScript');
      var hasScript = voElNow && String(voElNow.value || '').trim().length > 12;
      if (voGenEl2) voGenEl2.style.display = hasScript ? 'none' : 'flex';
    }

    if (isUgcRealFlow() && stepNum === studioUgcCharGateStep()) {
      maybePrefetchAutoCharacter();
    }
    /* Both flows: start character prefetch early when entering step 4 so Kie generation
     * overlaps with the user filling in the character brief — reduces wait on the approval
     * step from 2–3 min to near-zero when the user spends ≥30 s on step 4. */
    if (stepNum === 4 && !_characterPrefetchPromise && !_characterReviewPendingUrl) {
      try { clearTimeout(_characterPrefetchDebounceTimer); } catch (_e) {}
      _characterPrefetchDebounceTimer = setTimeout(function () {
        _characterPrefetchDebounceTimer = null;
        if (currentStep !== 4 || !shouldPrefetchAutoCharacter()) return;
        StudioAPI.ensureCanCallProtectedApi()
          .then(function (ok) {
            if (!ok) return;
            return ensureCharacterBriefForAutoPortrait().then(function () {
              maybePrefetchAutoCharacter();
            });
          })
          .catch(function () {});
      }, 3000);
    }
  }

  function collectStudioCharacterGenerateBody() {
    var briefRaw = (document.getElementById('characterBrief') || {}).value || '';
    var brief = briefRaw.trim();
    var body = {
      prompt: (document.getElementById('prompt') || {}).value || '',
      video_type: StudioSteps.getVideoType(),
      gender: StudioSteps.getGender(),
      country: (document.getElementById('country') || {}).value || '',
      language: StudioSteps.getLanguage(),
      visual_style: (document.getElementById('style') || {}).value || 'Auto'
    };
    if (brief) body.character_description = brief;
    return body;
  }

  /**
   * If Character look is empty, use the first AI suggestion (cached or freshly fetched).
   * Portrait can still run prompt-only if suggestions fail.
   */
  function ensureCharacterBriefForAutoPortrait() {
    var ta = document.getElementById('characterBrief');
    if (!ta) return Promise.resolve();
    if (ta.value && String(ta.value).trim()) return Promise.resolve();
    if (_characterSuggestCacheList.length > 0) {
      ta.value = _characterSuggestCacheList[0];
      return Promise.resolve();
    }
    var promptVal = ((document.getElementById('prompt') || {}).value || '').trim();
    if (promptVal.length < 3) return Promise.resolve();
    var videoType =
      typeof StudioSteps !== 'undefined' && StudioSteps.getVideoType ? StudioSteps.getVideoType() : 'influencer';
    var countryVal = ((document.getElementById('country') || {}).value || '').trim();
    var langVal =
      typeof StudioSteps !== 'undefined' && StudioSteps.getLanguage ? StudioSteps.getLanguage() : 'en';
    var genderVal =
      typeof StudioSteps !== 'undefined' && StudioSteps.getGender ? StudioSteps.getGender() : 'f';
    return StudioAPI.suggestCharacterBriefs({
      prompt: promptVal,
      video_type: videoType,
      country: countryVal,
      language: langVal,
      gender: genderVal
    })
      .then(function (data) {
        var list = data && data.suggestions ? data.suggestions : [];
        _characterSuggestCacheList = list.slice();
        _characterSuggestCacheKey =
          promptVal + '\n' + videoType + '\n' + countryVal + '\n' + langVal + '\n' + genderVal;
        if (list.length > 0) ta.value = list[0];
        try {
          renderCharacterBriefAiSuggestions(list);
        } catch (eRen) {}
      })
      .catch(function () {
        /* Prompt-only portrait remains valid */
      });
  }

  /**
   * Starts the single portrait POST when leaving step 4 (Next). Returns a Promise for optional tracking;
   * step navigation does not wait — generation continues in the background until the character gate shows the result.
   */
  function runCharacterPortraitOnStep4Next() {
    if (document.getElementById('simulationMode') && document.getElementById('simulationMode').checked) {
      return Promise.resolve();
    }
    if (!shouldPrefetchAutoCharacter()) {
      return Promise.resolve();
    }
    if (_characterPrefetchPromise) {
      return Promise.resolve();
    }
    if (_characterReviewPendingUrl && String(_characterReviewPendingUrl).trim()) {
      return Promise.resolve();
    }
    if (StudioSteps.getPrimaryCharacterHttpUrl && StudioSteps.getPrimaryCharacterHttpUrl()) {
      return Promise.resolve();
    }
    return StudioAPI.ensureCanCallProtectedApi()
      .then(function (ok) {
        if (!ok) return null;
        /* No step-4 status text — user already advanced; character gate shows progress. */
        return ensureCharacterBriefForAutoPortrait().then(function () {
          return maybePrefetchAutoCharacter();
        });
      })
      .catch(function (e) {
        console.warn('Portrait on step-4 Next:', e);
      });
  }

  function shouldPrefetchAutoCharacter() {
    var sim = document.getElementById('simulationMode') && document.getElementById('simulationMode').checked;
    if (sim) return false;
    var srcMode =
      typeof StudioSteps.getStep4CharacterSourceMode === 'function'
        ? StudioSteps.getStep4CharacterSourceMode()
        : '';
    if (srcMode === 'upload' || srcMode === 'library') return false;
    if (
      typeof StudioSteps !== 'undefined' &&
      StudioSteps.isProductNoOnScreenCharacter &&
      StudioSteps.isProductNoOnScreenCharacter()
    ) {
      return false;
    }
    if (typeof StudioSteps !== 'undefined' && StudioSteps.hasUploadedCharacter && StudioSteps.hasUploadedCharacter()) {
      return false;
    }
    if (typeof StudioSteps !== 'undefined' && StudioSteps.hasPendingCharacterFile && StudioSteps.hasPendingCharacterFile()) {
      return false;
    }
    return true;
  }

  /**
   * Show/hide the character preview panel in step 6.
   * When the auto-portrait arrives, show it for approval instead of silently applying.
   */
  function showCharPreview(url) {
    var wrap = document.getElementById('step6CharPreview');
    var img = document.getElementById('step6CharImg');
    if (!wrap || !img) return;
    if (!url) {
      wrap.style.display = 'none';
      var gw0 = document.getElementById('ugcCharGatePreview');
      if (gw0) gw0.style.display = 'none';
      return;
    }
    img.src = url;
    wrap.style.display = 'block';
    var gw = document.getElementById('ugcCharGatePreview');
    var gi = document.getElementById('ugcCharGateImg');
    var nh = document.getElementById('ugcCharGateNoPortraitHint');
    if (gw && gi) {
      gi.src = url;
      gw.style.display = 'block';
      if (nh) nh.style.display = 'none';
    }
    try {
      refreshSaveCharacterOnApproveVisibility();
    } catch (ePrev) {}
  }

  function hideCharPreview() {
    var wrap = document.getElementById('step6CharPreview');
    if (wrap) wrap.style.display = 'none';
    var gw = document.getElementById('ugcCharGatePreview');
    if (gw) gw.style.display = 'none';
    try {
      refreshSaveCharacterOnApproveVisibility();
    } catch (eH) {}
  }

  function patchUgcCharacterToPhase1Job() {
    if (!isUgcRealFlow() || !phase1JobId || typeof StudioSteps === 'undefined' || !StudioSteps.collectGeneratePayload) {
      return Promise.resolve();
    }
    try {
      var p = StudioSteps.collectGeneratePayload();
      var patch = {};
      if (p.character_url) patch.character_url = p.character_url;
      if (p.character_urls && p.character_urls.length) patch.character_urls = p.character_urls;
      if (!Object.keys(patch).length) return Promise.resolve();
      return StudioAPI.patchIntermediates(String(phase1JobId).trim(), { input_params_patch: patch }, { skipWaitingOverlay: true }).catch(
        function (e) {
          console.warn('patchUgcCharacterToPhase1Job', e);
        }
      );
    } catch (e2) {
      return Promise.resolve();
    }
  }

  function getStudioCharacterCorrectionEl() {
    if (isUgcRealFlow()) {
      return document.getElementById('ugcCharGateCorrection') || document.getElementById('step6CharCorrection');
    }
    return document.getElementById('step6CharCorrection');
  }

  /**
   * Product: apply pending portrait and close preview. UGC Real: apply + PATCH Phase 1 input_params with character_url(s).
   */
  function approveCharacterForPipeline() {
    var saveToLibrary = isSaveCharacterOnApproveChecked();
    if (saveToLibrary) {
      var nameTrim = getCharacterLibraryNameForSave();
      if (!nameTrim) {
        alert(
          'To save this character, enter a name on step 4 under "Save character to library" (visible when slot 1 has a hosted image and you are signed in), or uncheck "Also save to character library". You can also type a Character look first — its first line is used as a fallback name.'
        );
        return;
      }
      var cloudChk =
        typeof StudioAuth !== 'undefined' &&
        StudioAuth.isCloudConfigured &&
        StudioAuth.isCloudConfigured();
      var authedChk =
        typeof StudioAuth !== 'undefined' && StudioAuth.isAuthEnabled && StudioAuth.isAuthEnabled();
      if (!cloudChk || !authedChk) {
        alert('Sign in via Account (same browser) so the server can store the character in your library.');
        return;
      }
    }
    if (_characterReviewPendingUrl) {
      applyCharacterUrl(_characterReviewPendingUrl);
    }

    function afterApproveApplied() {
      if (!saveToLibrary) return Promise.resolve();
      return new Promise(function (resolve) {
        window.setTimeout(function () {
          var body = buildCharacterLibraryCreatePayload();
          if (!body) {
            alert(
              'Character approved. Library save was skipped — no hosted http(s) character URL was found. If you just approved, try "Save to library" on step 4 after the slot shows the image URL.'
            );
            resolve();
            return;
          }
          StudioAPI.createCharacter(body)
            .then(function () {
              try {
                refreshCharacterLibrarySelect();
              } catch (eR) {}
              var st = document.getElementById('characterLibrarySaveStatus');
              if (st) {
                st.style.display = 'block';
                st.textContent = 'Also saved to character library.';
              }
              clearSaveCharacterOnApproveCheckboxes();
            })
            .catch(function (err) {
              alert('Character approved, but library save failed: ' + (err.message || err));
            })
            .then(resolve);
        }, 50);
      });
    }

    if (!isUgcRealFlow()) {
      _characterReviewPendingUrl = null;
      _characterApproved = true;
      hideCharPreview();
      return afterApproveApplied();
    }
    try {
      var p = StudioSteps.collectGeneratePayload();
      if (!p.character_url && !(p.character_urls && p.character_urls.length)) {
        alert('Generate or upload a character portrait first, then approve.');
        return Promise.resolve();
      }
    } catch (eCh) {
      alert('Could not read character URL. Try regenerate or re-upload.');
      return Promise.resolve();
    }
    return patchUgcCharacterToPhase1Job().then(function () {
      return afterApproveApplied();
    });
  }

  function setCharRegenerateLoadingUI(loading) {
    // Visible feedback during the 30-90s regenerate so the stale image doesn't look final.
    var imgs = [document.getElementById('step6CharImg'), document.getElementById('ugcCharGateImg')];
    imgs.forEach(function (img) {
      if (!img) return;
      img.style.transition = 'opacity 200ms, filter 200ms';
      img.style.opacity = loading ? '0.35' : '1';
      img.style.filter = loading ? 'grayscale(0.6) blur(1px)' : '';
    });
    ['step6CharPreview', 'ugcCharGatePreview'].forEach(function (wrapId) {
      var wrap = document.getElementById(wrapId);
      if (!wrap) return;
      var inner = wrap.querySelector('.studio-char-preview-inner') || wrap;
      if (getComputedStyle(inner).position === 'static') inner.style.position = 'relative';
      var overlayId = wrapId + '_regenOverlay';
      var overlay = document.getElementById(overlayId);
      if (loading) {
        if (!overlay) {
          overlay = document.createElement('div');
          overlay.id = overlayId;
          overlay.setAttribute('role', 'status');
          overlay.setAttribute('aria-live', 'polite');
          overlay.style.cssText =
            'position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;' +
            'gap:0.5rem;background:rgba(0,0,0,0.45);color:#fff;font-weight:600;border-radius:inherit;' +
            'pointer-events:none;z-index:5;';
          overlay.innerHTML =
            '<span class="studio-spinner" style="width:28px;height:28px;border-width:3px;"></span>' +
            '<div>Generating new portrait…</div>' +
            '<div style="font-weight:400;font-size:0.8125rem;opacity:0.85;">Usually 30–90 seconds</div>';
          inner.appendChild(overlay);
        }
        overlay.style.display = 'flex';
      } else if (overlay) {
        overlay.remove();
      }
    });
  }

  function runCharacterRegenerateFromUI(btnEl) {
    var correctionEl = getStudioCharacterCorrectionEl();
    var correction = correctionEl ? correctionEl.value.trim() : '';
    if (btnEl) {
      btnEl.disabled = true;
      btnEl.textContent = 'Generating…';
    }
    var approveBtn = document.getElementById('btnApproveChar');
    var prevApproveDisabled = approveBtn ? approveBtn.disabled : null;
    if (approveBtn) approveBtn.disabled = true;
    setCharRegenerateLoadingUI(true);
    _characterPrefetchGen++;
    _characterPrefetchPromise = null;
    _characterReviewPendingUrl = null;
    _characterApproved = false;
    var body = collectStudioCharacterGenerateBody();
    if (correction) body.correction_text = correction;
    return StudioAPI.generateCharacter(body)
      .then(function (data) {
        if (data && data.image_url) {
          _characterPortraitPrefetchError = null;
          _characterReviewPendingUrl = data.image_url;
          showCharPreview(data.image_url);
          if (data.portrait_image_prompt) setPortraitImagePromptHidden(data.portrait_image_prompt);
          try {
            maybeKickVoiceDesignAfterCharacterReady();
          } catch (eVk) {}
        } else {
          _characterPortraitPrefetchError = 'Regenerate returned no image_url.';
        }
      })
      .catch(function (err) {
        _characterPortraitPrefetchError =
          (err && err.message ? String(err.message) : String(err || 'Unknown error')).slice(0, 400);
        alert('Character regeneration failed: ' + (err.message || err));
      })
      .finally(function () {
        setCharRegenerateLoadingUI(false);
        if (approveBtn) approveBtn.disabled = prevApproveDisabled === null ? false : prevApproveDisabled;
        if (btnEl) {
          btnEl.disabled = false;
          btnEl.textContent = 'Regenerate';
        }
        try {
          refreshNonUgcCharacterPortraitStatus();
        } catch (eRfRegen) {}
      });
  }

  function refreshUgcCharGateUI() {
    if (!isUgcRealFlow()) return;
    var hint = document.getElementById('ugcCharGateNoPortraitHint');
    var hasUp = typeof StudioSteps !== 'undefined' && StudioSteps.hasUploadedCharacter && StudioSteps.hasUploadedCharacter();
    if (_characterReviewPendingUrl) {
      showCharPreview(_characterReviewPendingUrl);
    } else if (hint) {
      hint.style.display = hasUp ? 'none' : 'block';
    }
  }

  function primeUgcCharGatePipelineBanner() {
    var el = document.getElementById('ugcCharGatePipelineStatus');
    if (!el || !isUgcRealFlow() || currentStep !== 6) return;
    if (!phase1JobId) {
      el.style.display = 'none';
      return;
    }
    el.style.display = 'block';
    el.textContent =
      'Phase 1 is running in the background (Gemini brief parse and offer pipeline). The next step shows the three offer fields as soon as they are ready.';
  }

  function applyCharacterUrl(url) {
    if (
      typeof StudioSteps !== 'undefined' &&
      StudioSteps.setPrimaryCharacterUrl
    ) {
      StudioSteps.setPrimaryCharacterUrl(url);
      try { saveSession(); } catch (e) {}
      try {
        updateCharacterLibrarySaveVisibility();
      } catch (e2) {}
    }
    try {
      maybeKickVoiceDesignAfterCharacterReady();
    } catch (eVk) {}
  }

  function renderCharacterBriefAiSuggestions(suggestions) {
    var row = document.getElementById('characterBriefAiRow');
    if (!row) return;
    row.innerHTML = '';
    if (!suggestions || !suggestions.length) {
      var sp0 = document.createElement('span');
      sp0.className = 'studio-field-hint';
      sp0.textContent = 'No suggestions returned.';
      row.appendChild(sp0);
      return;
    }
    suggestions.forEach(function (text) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'studio-btn studio-btn-ghost studio-btn-sm';
      var shortT = String(text);
      b.textContent = shortT.length > 44 ? shortT.slice(0, 41) + '…' : shortT;
      b.title = shortT;
      b.addEventListener('click', function () {
        var ta = document.getElementById('characterBrief');
        if (ta) ta.value = text;
        try {
          maybeKickVoiceDesignAfterCharacterReady();
        } catch (eChipVk) {}
        try {
          if (typeof StudioSteps !== 'undefined' && StudioSteps.refreshStep4CharacterExclusiveHint) {
            StudioSteps.refreshStep4CharacterExclusiveHint();
          }
        } catch (eChipH) {}
      });
      row.appendChild(b);
    });
  }

  function characterBriefAiSetRowMessage(msg, isErr) {
    var row = document.getElementById('characterBriefAiRow');
    if (!row) return;
    row.innerHTML = '';
    var sp = document.createElement('span');
    sp.className = 'studio-field-hint';
    if (isErr) sp.style.color = 'var(--studio-danger, #e57373)';
    sp.textContent = msg;
    row.appendChild(sp);
  }

  /** Load character-look chips: POST /api/suggest-character-briefs with the Step 3 #prompt text (verbatim). */
  function refreshCharacterBriefAiSuggestions(force) {
    var row = document.getElementById('characterBriefAiRow');
    if (!row) return;
    var briefWrap = document.getElementById('characterBriefWrap');
    if (briefWrap && window.getComputedStyle(briefWrap).display === 'none') return;

    var promptVal = ((document.getElementById('prompt') || {}).value || '').trim();
    var videoType =
      typeof StudioSteps !== 'undefined' && StudioSteps.getVideoType ? StudioSteps.getVideoType() : 'influencer';
    var countryVal = ((document.getElementById('country') || {}).value || '').trim();
    var langVal =
      typeof StudioSteps !== 'undefined' && StudioSteps.getLanguage ? StudioSteps.getLanguage() : 'en';
    var genderVal =
      typeof StudioSteps !== 'undefined' && StudioSteps.getGender ? StudioSteps.getGender() : 'f';
    var key = promptVal + '\n' + videoType + '\n' + countryVal + '\n' + langVal + '\n' + genderVal;

    if (!force && key === _characterSuggestCacheKey && _characterSuggestCacheList.length) {
      renderCharacterBriefAiSuggestions(_characterSuggestCacheList);
      return;
    }

    if (promptVal.length < 3) {
      _characterSuggestCacheKey = '';
      _characterSuggestCacheList = [];
      characterBriefAiSetRowMessage(
        'Add your main script on step 3 (at least a few words) to get AI character ideas.',
        false
      );
      return;
    }

    if (_characterSuggestInFlight && !force) return;
    _characterSuggestInFlight = true;
    characterBriefAiSetRowMessage('Loading suggestions…', false);

    StudioAPI.ensureCanCallProtectedApi()
      .then(function (ok) {
        if (!ok) {
          characterBriefAiSetRowMessage('Sign in or set an API key to load AI suggestions.', true);
          return Promise.reject(new Error('__auth'));
        }
        return StudioAPI.suggestCharacterBriefs({
          prompt: promptVal,
          video_type: videoType,
          country: countryVal,
          language: langVal,
          gender: genderVal
        });
      })
      .then(function (data) {
        var list = data && data.suggestions ? data.suggestions : [];
        _characterSuggestCacheKey = key;
        _characterSuggestCacheList = list.slice();
        renderCharacterBriefAiSuggestions(list);
      })
      .catch(function (err) {
        if (!(err && err.message === '__auth')) {
          characterBriefAiSetRowMessage(
            err && err.message ? String(err.message).slice(0, 300) : 'Suggestions failed.',
            true
          );
        }
      })
      .then(function () {
        _characterSuggestInFlight = false;
      });
  }

  /** Step-3 prompt or locale changed — chips must not show stale Gemini results for an old script. */
  function invalidateCharacterBriefAiSuggestionCache() {
    _characterSuggestCacheKey = '';
    _characterSuggestCacheList = [];
  }

  var CHARACTER_BRIEF_PRESETS = {
    saas:
      'Early-30s founder look: casual smart outfit (simple sweater or tee under open shirt), warm confident smile, tidy modern home office, soft window light, authentic tech-creator vibe, looking at camera.',
    fitness:
      'Athletic creator mid-20s, hair in ponytail or bun, fitted sports top, bright gym or outdoor park, energetic expression, golden hour, relatable fitness UGC aesthetic.',
    beauty:
      'Natural glam, warm skin glow, neat hair, clean vanity or bright bathroom, soft frontal light, trustworthy beauty creator, gentle smile, looking at camera.'
  };

  function setCharacterPortraitGenStatus(msg, isErr) {
    var el = document.getElementById('characterPortraitGenStatus');
    if (!el) return;
    if (!msg) {
      el.style.display = 'none';
      el.textContent = '';
      el.style.color = '';
      return;
    }
    el.style.display = 'block';
    el.textContent = msg;
    el.style.color = isErr ? 'var(--studio-danger, #e57373)' : '';
  }

  function setupCharacterPortraitControls() {
    function wirePreset(id, key) {
      var b = document.getElementById(id);
      if (!b) return;
      b.addEventListener('click', function () {
        var text = CHARACTER_BRIEF_PRESETS[key];
        var ta = document.getElementById('characterBrief');
        if (ta && text) ta.value = text;
        try {
          maybeKickVoiceDesignAfterCharacterReady();
        } catch (ePresetVk) {}
        try {
          if (typeof StudioSteps !== 'undefined' && StudioSteps.refreshStep4CharacterExclusiveHint) {
            StudioSteps.refreshStep4CharacterExclusiveHint();
          }
        } catch (ePresetH) {}
      });
    }
    wirePreset('btnCharacterBriefExSaaS', 'saas');
    wirePreset('btnCharacterBriefExFitness', 'fitness');
    wirePreset('btnCharacterBriefExBeauty', 'beauty');

    var refAi = document.getElementById('btnCharacterBriefAiRefresh');
    if (refAi) {
      refAi.addEventListener('click', function () {
        refreshCharacterBriefAiSuggestions(true);
      });
    }

    var genBtn = document.getElementById('btnGenerateCharacterPortrait');
    if (!genBtn) return;
    genBtn.addEventListener('click', function () {
      var pr = ((document.getElementById('prompt') || {}).value || '').trim();
      if (pr.length < 3) {
        alert('Add your main script on step 3 first — AI suggestions need it.');
        return;
      }
      genBtn.disabled = true;
      setCharacterPortraitGenStatus('Loading AI look lines…', false);
      StudioAPI.ensureCanCallProtectedApi()
        .then(function (ok) {
          if (!ok) {
            setCharacterPortraitGenStatus('Sign in or set API key to load suggestions.', true);
            return Promise.reject(new Error('__auth'));
          }
          return ensureCharacterBriefForAutoPortrait();
        })
        .then(function () {
          setCharacterPortraitGenStatus(
            'Suggestions applied when available. Click Next whenever you are ready — you move on immediately; the portrait runs in the background (no duplicate).',
            false
          );
        })
        .catch(function (err) {
          if (err && err.message === '__auth') return;
          setCharacterPortraitGenStatus(
            err && err.message
              ? String(err.message).slice(0, 240)
              : 'Could not load suggestions. Type a look or use a chip, then click Next.',
            true
          );
        })
        .finally(function () {
          genBtn.disabled = false;
        });
    });
  }

  /**
   * When it completes, show the preview in step 6 for user approval.
   * Triggered from step 5 (after step 4 media) or before Start generation — no full-screen waiting overlay.
   */
  function maybePrefetchAutoCharacter() {
    if (!shouldPrefetchAutoCharacter()) return Promise.resolve(null);
    if (_characterReviewPendingUrl) return Promise.resolve({ image_url: _characterReviewPendingUrl });
    if (_characterPrefetchPromise) return _characterPrefetchPromise;
    _characterPortraitPrefetchError = null;
    var myGen = _characterPrefetchGen;
    var p = StudioAPI.generateCharacter(collectStudioCharacterGenerateBody())
      .then(function (data) {
        if (myGen !== _characterPrefetchGen) return data;
        var ph = document.getElementById('step6PortraitPrefetchHint');
        if (ph) {
          ph.style.display = 'none';
          ph.textContent = '';
        }
        if (data && data.image_url) {
          _characterPortraitPrefetchError = null;
          _characterReviewPendingUrl = data.image_url;
          showCharPreview(data.image_url);
          if (data.portrait_image_prompt) setPortraitImagePromptHidden(data.portrait_image_prompt);
          try {
            maybeKickVoiceDesignAfterCharacterReady();
          } catch (eVkPf) {}
        } else {
          _characterPortraitPrefetchError = 'Server returned no image_url for generate-character.';
        }
        try {
          refreshNonUgcCharacterPortraitStatus();
        } catch (eRf1) {}
        return data;
      })
      .catch(function (err) {
        _characterPortraitPrefetchError =
          (err && err.message ? String(err.message) : String(err || 'Unknown error')).slice(0, 400);
        var ph = document.getElementById('step6PortraitPrefetchHint');
        if (ph && currentStep >= 5) {
          ph.style.display = 'block';
          ph.textContent =
            (isUgcRealFlow()
              ? 'Optional portrait prefetch (/api/generate-character) failed — see API Log. Offer TEXT fields still come from Phase 1 step_parse. '
              : 'Optional portrait prefetch (/api/generate-character) failed — see API Log. This does not stop TEXT 1–3; they come from Phase 1 parse_prompt. ') +
            (err && err.message ? String(err.message).slice(0, 220) : '');
        }
        console.warn('Studio portrait prefetch failed:', err);
        try {
          refreshNonUgcCharacterPortraitStatus();
        } catch (eRf2) {}
        return null;
      })
      .finally(function () {
        if (_characterPrefetchPromise === p) _characterPrefetchPromise = null;
        try {
          refreshNonUgcCharacterPortraitStatus();
        } catch (eRf3) {}
        try {
          pollJob();
        } catch (ePollPf) {}
        try {
          studioEnsureJobPollInterval();
        } catch (ePollPf2) {}
      });
    _characterPrefetchPromise = p;
    return p;
  }

  function restartCharacterPrefetchFromUserInput() {
    _characterPrefetchGen++;
    _characterPrefetchPromise = null;
    _characterReviewPendingUrl = null;
    _characterPortraitPrefetchError = null;
    setPortraitImagePromptHidden('');
    try {
      clearTimeout(_characterPrefetchDebounceTimer);
    } catch (e) {}
    _characterPrefetchDebounceTimer = null;
    /* When called while the user is still on step 4, re-schedule the early prefetch so
     * the new settings (gender / country / style / character look) are reflected in the
     * portrait request without waiting until the user clicks Next. */
    if (currentStep === 4 && shouldPrefetchAutoCharacter()) {
      _characterPrefetchDebounceTimer = setTimeout(function () {
        _characterPrefetchDebounceTimer = null;
        if (currentStep !== 4 || !shouldPrefetchAutoCharacter()) return;
        StudioAPI.ensureCanCallProtectedApi()
          .then(function (ok) {
            if (!ok) return;
            return ensureCharacterBriefForAutoPortrait().then(function () {
              maybePrefetchAutoCharacter();
            });
          })
          .catch(function () {});
      }, 3000);
    }
  }

  function setupVideoTypeCards() {
    document.querySelectorAll('[data-video-type]').forEach(function (card) {
      card.addEventListener('click', function () {
        document.querySelectorAll('[data-video-type].selected').forEach(function (c) { c.classList.remove('selected'); });
        card.classList.add('selected');
        StudioSteps.updateVisibilityForVideoType();
        updateUgcRealStepVisibility();
        invalidateCharacterBriefAiSuggestionCache();
        restartCharacterPrefetchFromUserInput();
      });
    });
    document.querySelector('[data-video-type="product video"]').classList.add('selected');
  }

  function setupStyleCards() {
    const container = document.getElementById('styleCards');
    if (!container) return;
    StudioData.styles.forEach(function (s) {
      const card = document.createElement('div');
      card.className = 'studio-card studio-card-selectable';
      card.dataset.style = s.value;
      card.innerHTML = '<strong>' + (s.label || s.value) + '</strong><p class="studio-field-hint">' + (s.desc || '') + '</p>';
      if (s.value === 'Auto') card.classList.add('selected');
      card.addEventListener('click', function () {
        container.querySelectorAll('.studio-card-selectable.selected').forEach(function (c) { c.classList.remove('selected'); });
        card.classList.add('selected');
        var styleInput = document.getElementById('style');
        if (styleInput) styleInput.value = s.value;
        restartCharacterPrefetchFromUserInput();
      });
      container.appendChild(card);
    });
  }

  function setupDurationButtons() {
    document.querySelectorAll('[data-duration]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        document.querySelectorAll('#durationOptions button').forEach(function (b) { b.classList.remove('active'); });
        btn.classList.add('active');
        var durationInput = document.getElementById('duration');
        if (durationInput) durationInput.value = btn.dataset.duration;
      });
    });
    // Sync hidden input from whichever button is currently marked active
    var activeBtn = document.querySelector('#durationOptions button.active');
    if (activeBtn) {
      var durationInput = document.getElementById('duration');
      if (durationInput) durationInput.value = activeBtn.dataset.duration;
    }
  }

  function setupGenderToggle() {
    document.querySelectorAll('.studio-gender-toggle button').forEach(function (btn) {
      btn.addEventListener('click', function () {
        btn.parentElement.querySelectorAll('button').forEach(function (b) { b.classList.remove('active'); });
        btn.classList.add('active');
        var customVo = document.getElementById('voiceIdCustom');
        if (customVo) customVo.value = '';
        _voicesCache = {};
        updateVoiceDropdown();
        if (typeof refreshStep7VoiceBlock === 'function') refreshStep7VoiceBlock();
        invalidateCharacterBriefAiSuggestionCache();
        restartCharacterPrefetchFromUserInput();
        try {
          if (typeof currentStep === 'number' && currentStep === 4) {
            refreshCharacterBriefAiSuggestions(false);
          }
        } catch (eSugG) {}
      });
    });
  }

  function populateSelect(id, options, valueKey, labelKey) {
    valueKey = valueKey || 'value';
    labelKey = labelKey || 'label';
    var sel = document.getElementById(id);
    if (!sel) return;
    sel.innerHTML = '';
    options.forEach(function (opt) {
      var o = document.createElement('option');
      o.value = opt[valueKey];
      o.textContent = opt[labelKey];
      sel.appendChild(o);
    });
  }

  function updateVoiceDropdown() {
    var lang = StudioSteps.getLanguage();
    var gender = StudioSteps.getGender();
    var voices = [];
    if (StudioData.languageVoices[lang]) {
      var g = StudioData.languageVoices[lang][gender === 'm' ? 'male' : 'female'];
      if (g) voices.push(g);
    }
    if (voices.length === 0) {
      var def = StudioData.defaultVoices[gender === 'm' ? 'male' : 'female'];
      voices.push(def);
    }
    populateSelect('voiceId', voices, 'id', 'label');
  }

  function setupNavButtons() {
    document.querySelectorAll('.studio-nav [data-next]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var sec = btn.closest('.studio-step');
        var sid = sec && sec.id;
        var next = parseInt(btn.dataset.next, 10);
        var prevStep = next - 1;
        if (sid === 'step9music' && btn.hasAttribute('data-next')) {
          goToStep(isUgcRealFlow() ? studioFinalStep() : 11);
          return;
        }
        if (sid === 'step10assets' && btn.hasAttribute('data-next')) {
          goToStep(isUgcRealFlow() ? studioFinalStep() : 12);
          return;
        }
        if (sid === 'step12images' && btn.hasAttribute('data-next')) {
          goToStep(studioFinalStep());
          return;
        }
        /* Wizard step 7 → 9: no numeric step 8; do not run validateStep(8) (product VO gate). */
        if (sid === 'stepNonUgcCharacterGate') {
          goToStep(parseInt(btn.dataset.next, 10));
          return;
        }
        if (sid === 'step4' && next === 5) {
          if (!StudioSteps.validateStep(4)) {
            if (StudioSteps.getVideoType && StudioSteps.getVideoType() !== 'ugc-real' && !StudioSteps.isProductNoOnScreenCharacter()) {
              var step4Msg = StudioSteps.getStep4SourceValidationError && StudioSteps.getStep4SourceValidationError();
              if (step4Msg) {
                alert(step4Msg);
              } else {
                alert('Complete the character step: choose a source and fill the matching fields.');
              }
            } else {
              alert('Please fill required fields.');
            }
            return;
          }
          goToStep(5);
          window.setTimeout(function () {
            try {
              runCharacterPortraitOnStep4Next();
            } catch (eP4) {}
          }, 0);
          return;
        }
        if (!StudioSteps.validateStep(prevStep)) {
          if (prevStep === 3) {
            alert(
              'Please enter a prompt (step 3). For UGC Real, describe the offer, audience, problem, benefits, and CTA — see the hint above the box.'
            );
          } else {
            alert('Please fill required fields.');
          }
          return;
        }
        goToStep(next);
      });
    });
    document.querySelectorAll('.studio-nav [data-prev]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var sec = btn.closest('.studio-step');
        var sid = sec && sec.id;
        if (sid === 'step7prefs') {
          goToStep(5);
          return;
        }
        if (sid === 'step8vo') {
          goToStep(
            isUgcRealFlow()
              ? studioUgcNineStep()
              : isStudioProductVideo()
                ? studioPrefsStep()
                : studioNonUgcCharacterStep()
          );
          return;
        }
        if (sid === 'step9music') {
          goToStep(studioVoStep());
          return;
        }
        if (sid === 'step10assets') {
          goToStep(studioMusicStep());
          return;
        }
        if (sid === 'step12images') {
          goToStep(isUgcRealFlow() ? studioMusicStep() : studioPromptsStep());
          return;
        }
        if (sid === 'step13final') {
          goToStep(isUgcRealFlow() ? studioMusicStep() : studioImagesStep());
          return;
        }
        if (sid === 'step14subs') {
          goToStep(studioFinalStep());
          return;
        }
        var prev = parseInt(btn.dataset.prev, 10);
        goToStep(prev);
      });
    });
  }

  function createMediaZones() {
    var refContainer = document.getElementById('refImageZones');
    var refImageZones = [];
    var refImageExplains = [];
    if (refContainer) {
      for (var i = 1; i <= 5; i++) {
        var wrap = document.createElement('div');
        wrap.className = 'studio-field';
        var label = document.createElement('label');
        label.className = 'studio-label';
        label.textContent = 'Image ' + i;
        wrap.appendChild(label);
        var z = StudioUpload.createZone({ id: 'ref_' + i, label: 'Image ' + i, accept: 'image/*' });
        wrap.appendChild(z.getEl());
        var explainLabel = document.createElement('label');
        explainLabel.className = 'studio-label';
        explainLabel.textContent = 'Explain (optional)';
        explainLabel.style.marginTop = '0.5rem';
        wrap.appendChild(explainLabel);
        var explainInput = document.createElement('input');
        explainInput.type = 'text';
        explainInput.className = 'studio-input';
        explainInput.placeholder = 'Short description';
        explainInput.dataset.refIndex = i - 1;
        wrap.appendChild(explainInput);
        refContainer.appendChild(wrap);
        refImageZones.push(z);
        refImageExplains.push(explainInput);
      }
    } else {
      console.warn('Studio: #refImageZones missing — reference image uploads disabled (use full studio index.html).');
    }

    var assetContainer = document.getElementById('assetZones');
    var assetZones = [];
    if (assetContainer) {
      for (var a = 1; a <= 3; a++) {
        var awrap = document.createElement('div');
        awrap.className = 'studio-field';
        var alabel = document.createElement('label');
        alabel.className = 'studio-label';
        alabel.textContent = 'Asset ' + a;
        awrap.appendChild(alabel);
        var az = StudioUpload.createZone({ id: 'asset_' + a, label: 'Video clip ' + a, accept: 'video/*,image/*' });
        awrap.appendChild(az.getEl());
        assetContainer.appendChild(awrap);
        assetZones.push(az);
      }
    } else {
      console.warn('Studio: #assetZones missing — asset uploads disabled.');
    }

    var cleanContainer = document.getElementById('cleanProductZones');
    var cleanProductZones = [];
    if (cleanContainer) {
      for (var c = 0; c < 3; c++) {
        var clabel = c === 0 ? 'Clean Product image' : 'Clean Product image ' + (c + 1);
        var cz = StudioUpload.createZone({ id: 'clean_' + (c + 1), label: clabel, accept: 'image/*' });
        cleanContainer.appendChild(cz.getEl());
        cleanProductZones.push(cz);
      }
    } else {
      console.warn('Studio: #cleanProductZones missing — product image uploads disabled.');
    }

    function onCharacterZoneMediaChange() {
      try {
        if (typeof StudioSteps !== 'undefined' && StudioSteps.refreshStep4CharacterExclusiveHint) {
          StudioSteps.refreshStep4CharacterExclusiveHint();
        }
      } catch (eCz) {}
    }
    var char1 = StudioUpload.createZone({
      id: 'char1',
      label: 'Character',
      accept: 'image/*',
      onChange: onCharacterZoneMediaChange
    });
    var char2 = StudioUpload.createZone({
      id: 'char2',
      label: 'Character 2',
      accept: 'image/*',
      onChange: onCharacterZoneMediaChange
    });
    var char3 = StudioUpload.createZone({
      id: 'char3',
      label: 'Character 3',
      accept: 'image/*',
      onChange: onCharacterZoneMediaChange
    });
    var cz1 = document.getElementById('characterZone1');
    var cz2 = document.getElementById('characterZone2');
    var cz3 = document.getElementById('characterZone3');
    if (cz1) cz1.appendChild(char1.getEl());
    else console.warn('Studio: #characterZone1 missing.');
    if (cz2) cz2.appendChild(char2.getEl());
    else console.warn('Studio: #characterZone2 missing.');
    if (cz3) cz3.appendChild(char3.getEl());
    else console.warn('Studio: #characterZone3 missing.');

    var logoZ = StudioUpload.createZone({ id: 'logo', label: 'Logo', accept: 'image/*' });
    var lz = document.getElementById('logoZone');
    if (lz) lz.appendChild(logoZ.getEl());
    else console.warn('Studio: #logoZone missing.');

    StudioSteps.registerMediaZones({
      refImageZones: refImageZones,
      refImageExplains: refImageExplains,
      assetZones: assetZones,
      cleanProductZones: cleanProductZones,
      characterZones: [char1, char2, char3],
      logoZone: logoZ
    });

    [char1, char2, char3].forEach(function (chZone) {
      var wrap = chZone.getEl();
      var finp = wrap.querySelector('input[type="file"]');
      if (finp) {
        finp.addEventListener('change', function () {
          restartCharacterPrefetchFromUserInput();
          try {
            updateCharacterLibrarySaveVisibility();
          } catch (eCh) {}
        });
      }
    });

    var step4el = document.getElementById('step4');
    if (step4el) {
      step4el.addEventListener('click', function (e) {
        if (e.target && e.target.classList && e.target.classList.contains('studio-upload-remove')) {
          setTimeout(function () {
            try {
              updateCharacterLibrarySaveVisibility();
            } catch (eRm) {}
            try {
              if (typeof StudioSteps !== 'undefined' && StudioSteps.refreshStep4CharacterExclusiveHint) {
                StudioSteps.refreshStep4CharacterExclusiveHint();
              }
            } catch (eRm2) {}
          }, 0);
        }
      });
    }
  }

  function uploadPendingZones() {
    var zones = StudioSteps.getAllUploadZones();
    var promises = zones.map(function (z) {
      if (z.getFile && z.getFile() && (!z.getUrl || !z.getUrl())) {
        return z.upload().catch(function (err) {
          console.error('Upload failed', err);
          return null;
        });
      }
      return Promise.resolve(null);
    });
    return Promise.all(promises);
  }

  /** True if any zone has a local file selected but no uploaded URL yet (can block Start generation indefinitely). */
  function hasPendingFileUploads() {
    try {
      var zones = StudioSteps.getAllUploadZones();
      return zones.some(function (z) {
        return z.getFile && z.getFile() && (!z.getUrl || !z.getUrl());
      });
    } catch (e) {
      return false;
    }
  }

  function setStartGenerateBusy(busy) {
    var btn = document.getElementById('btnStartGenerate');
    if (!btn) return;
    if (busy) {
      if (btn.dataset.prevStartLabel == null) btn.dataset.prevStartLabel = btn.textContent;
      btn.disabled = true;
      btn.textContent = 'Starting…';
    } else {
      btn.disabled = false;
      if (btn.dataset.prevStartLabel != null) {
        btn.textContent = btn.dataset.prevStartLabel;
        delete btn.dataset.prevStartLabel;
      }
    }
  }

  /** Reject if promise does not settle within ms (e.g. stuck file upload). */
  function promiseWithTimeout(promise, ms, errMsg) {
    return new Promise(function (resolve, reject) {
      var settled = false;
      var tid = setTimeout(function () {
        if (settled) return;
        settled = true;
        reject(new Error(errMsg || ('Timed out after ' + Math.round(ms / 1000) + 's')));
      }, ms);
      promise.then(
        function (v) {
          if (settled) return;
          settled = true;
          clearTimeout(tid);
          resolve(v);
        },
        function (e) {
          if (settled) return;
          settled = true;
          clearTimeout(tid);
          reject(e);
        }
      );
    });
  }

  function watchJob(jobId) {
    currentJobId = jobId;
    pollJob();
    if (pollIntervalId) clearInterval(pollIntervalId);
    pollIntervalId = setInterval(pollJob, 2000);
    if (sseSource) {
      try {
        sseSource.close();
      } catch (e) {}
    }
    sseSource = null;
    _sseActive = false;
    StudioAPI.connectSSE(currentJobId, function (eventType, data) {
      if (data && (data.event_type === 'complete' || data.event_type === 'error' || data.event_type === 'abort' || data.event_type === 'pause')) {
        _sseActive = false;
      }
      pollJob();
    }).then(function (es) {
      sseSource = es;
      _sseActive = true;
    }).catch(function () { _sseActive = false; });
  }

  function startGeneration() {
    var isAuto = document.getElementById('modeAuto').classList.contains('active');
    var payload = StudioSteps.collectGeneratePayload();
    if (!payload.prompt || !payload.prompt.trim()) {
      alert('Please enter a prompt.');
      return;
    }
    // Phase 1 payload is built *after* uploadPendingZones so product_image_urls are present.
    var wantsPhase1 = !payload.simulation && (currentStep <= 5 || !isAuto);

    // Reuse Phase 1 job from auto-start on step 4–5 — avoid duplicate parse_prompt jobs.
    if (!isAuto && phase1JobId && currentStep <= 5) {
      currentJobId = phase1JobId;
      collectedIntermediates = {};
      _musicAutoStarted = false;
      if (sseSource) { sseSource.close(); _sseActive = false; }
      pollJob();
      if (pollIntervalId) clearInterval(pollIntervalId);
      pollIntervalId = setInterval(pollJob, 2000);
      StudioAPI.connectSSE(currentJobId, function (eventType, data) {
        if (data.event_type === 'complete' || data.event_type === 'error' || data.event_type === 'abort') {
          if (pollIntervalId) clearInterval(pollIntervalId);
          pollIntervalId = null;
          _sseActive = false;
        } else if (data.event_type === 'pause') {
          _sseActive = false;
          if (!pollIntervalId) pollIntervalId = setInterval(pollJob, 2000);
        }
        if (data.asset_url && data.asset_type === 'audio') {
          document.getElementById('musicAudio').src = data.asset_url;
          document.getElementById('musicPlayer').style.display = 'flex';
        }
        pollJob();
      }).then(function (es) {
        sseSource = es;
        _sseActive = true;
      });
      setTimeout(function () { if (pollIntervalId) clearInterval(pollIntervalId); pollIntervalId = null; }, 30 * 60 * 1000);
      goToStep(isUgcRealFlow() ? studioPrefsStep() : 6);
      return;
    }

    var uploadOverlayPushed = false;

    var authGate = payload.simulation
      ? Promise.resolve(true)
      : StudioAPI.ensureCanCallProtectedApi();

    authGate
      .then(function (ok) {
        if (!ok) {
          var apiKeyEl = document.getElementById('studioApiKey');
          if (apiKeyEl) {
            apiKeyEl.focus();
            apiKeyEl.placeholder = 'Paste API key or use Account → Sign in';
          }
          alert(
            'Before starting generation: open Account → Sign in, or paste API key (sk-tvd-...) in the header.\n\n' +
              'Cloud sign-in only works if the API server has SUPABASE_SERVICE_ROLE_KEY and exactly one active api_tenants row, or STUDIO_FALLBACK_API_KEY in .env. Hard-refresh (Ctrl+Shift+R) if this page looks outdated.'
          );
          return Promise.reject(new Error('__studio_needs_credentials'));
        }
        setStartGenerateBusy(true);
        return Promise.resolve();
      })
      .then(function () {
      var uploadPromise;
      if (hasPendingFileUploads()) {
        if (typeof StudioWaitingOverlay !== 'undefined' && StudioWaitingOverlay.push) {
          StudioWaitingOverlay.push('upload');
          uploadOverlayPushed = true;
        }
        uploadPromise = promiseWithTimeout(
          uploadPendingZones(),
          240000,
          'File uploads timed out (4 minutes). Check the network, remove unfinished uploads, or clear stuck files in the upload zones, then try Start generation again.'
        );
      } else {
        uploadPromise = uploadPendingZones();
      }
      return uploadPromise;
    })
      .finally(function () {
        if (uploadOverlayPushed && typeof StudioWaitingOverlay !== 'undefined' && StudioWaitingOverlay.pop) {
          try {
            StudioWaitingOverlay.pop();
          } catch (ePop) {}
          uploadOverlayPushed = false;
        }
      })
      .then(function () {
        var toSend = wantsPhase1 ? StudioSteps.collectPhase1Payload() : payload;
        return StudioAPI.generate(toSend);
      })
      .then(function (res) {
        var jid = res && (res.job_id != null ? res.job_id : res.id);
        if (jid == null || String(jid).trim() === '') {
          alert(
            'Start generation: the server did not return a job id. Open the **API Log** section on this page, verify the API base URL and API key match your running server, and check the server console for errors.'
          );
          return;
        }
        currentJobId = String(jid).trim();
        if (!document.getElementById('modeAuto').classList.contains('active')) {
          if (String(phase1JobId || '') !== String(currentJobId)) {
            try {
              studioResetUgcRealClientCaches();
            } catch (eUgNew) {}
          }
          phase1JobId = currentJobId;
          _phase1LastPolledStatus = null;
        }
        collectedIntermediates = {};
        _musicAutoStarted = false;
        if (sseSource) { sseSource.close(); _sseActive = false; }
        try {
          updateGenerateVOButtonState();
        } catch (eP1Manual) {}

        pollJob();
        if (pollIntervalId) clearInterval(pollIntervalId);
        pollIntervalId = setInterval(pollJob, 2000);
        StudioAPI.connectSSE(currentJobId, function (eventType, data) {
          if (data.event_type === 'complete' || data.event_type === 'error' || data.event_type === 'abort') {
            if (pollIntervalId) clearInterval(pollIntervalId);
            pollIntervalId = null;
            _sseActive = false;
          } else if (data.event_type === 'pause') {
            _sseActive = false;
            if (!pollIntervalId) pollIntervalId = setInterval(pollJob, 2000);
          }
          if (data.asset_url && data.asset_type === 'audio') {
            document.getElementById('musicAudio').src = data.asset_url;
            document.getElementById('musicPlayer').style.display = 'flex';
          }
          pollJob();
        }).then(function (es) {
          sseSource = es;
          _sseActive = true;
        });
        setTimeout(function () { if (pollIntervalId) clearInterval(pollIntervalId); pollIntervalId = null; }, 30 * 60 * 1000);

        if (isAuto) {
          goToStep(studioFinalStep());
          document.getElementById('finalStatus').textContent = 'Job ' + currentJobId + ' running. Progress updates every few seconds.';
        } else {
          goToStep(isUgcRealFlow() ? studioPrefsStep() : 6);
        }
      })
      .catch(function (err) {
        var msg = err.message || String(err);
        if (msg.indexOf('__studio_needs_credentials') >= 0) {
          return;
        }
        if (msg.indexOf('401') >= 0 || msg.indexOf('API key') >= 0 || msg.indexOf('Missing') >= 0) {
          var apiKeyEl = document.getElementById('studioApiKey');
          if (apiKeyEl) {
            apiKeyEl.focus();
            apiKeyEl.placeholder = 'Paste your API key (required)';
          }
          console.warn('Studio generate auth failed:', msg);
          alert(
            'Authentication failed (401).\n\n' +
              '• Paste sk-tvd-... in the API key field (header), or\n' +
              '• If you use cloud sign-in only: open Account → Sign in again (the session may have expired), or ask your admin to set STUDIO_FALLBACK_API_KEY on the API server.\n\n' +
              (msg.length < 350 ? msg : msg.slice(0, 350) + '…')
          );
        } else {
          alert('Failed to start: ' + msg);
        }
      })
      .finally(function () {
        setStartGenerateBusy(false);
      });
  }

  function init() {
    populateSelect('country', StudioData.countries);
    populateSelect('language', StudioData.languages);
    setupVideoTypeCards();
    setupStyleCards();
    setupDurationButtons();
    setupGenderToggle();
    setupNavButtons();
    createMediaZones();
    setupCharacterPortraitControls();
    var productNoCharCb = document.getElementById('productNoOnScreenCharacter');
    if (productNoCharCb) {
      productNoCharCb.addEventListener('change', function () {
        if (productNoCharCb.checked) {
          if (typeof StudioSteps.clearCharacterSlotsAndBrief === 'function') {
            try {
              StudioSteps.clearCharacterSlotsAndBrief();
            } catch (eClr) {}
          }
          _characterReviewPendingUrl = null;
          try {
            hideCharPreview();
          } catch (eH) {}
          try {
            restartCharacterPrefetchFromUserInput();
          } catch (eRf) {}
          var libSel = document.getElementById('characterLibrarySelect');
          if (libSel) libSel.value = '';
          try {
            if (typeof StudioSteps.updateVisibilityForVideoType === 'function') {
              StudioSteps.updateVisibilityForVideoType();
            }
          } catch (eVisPn) {}
          try {
            saveSession();
          } catch (eSv) {}
        } else {
          try {
            if (typeof StudioSteps.updateVisibilityForVideoType === 'function') {
              StudioSteps.updateVisibilityForVideoType();
            }
          } catch (eVisUn) {}
        }
        try {
          if (typeof StudioSteps !== 'undefined' && StudioSteps.applyStep4CharacterSourceUI) {
            StudioSteps.applyStep4CharacterSourceUI();
          }
          if (typeof StudioSteps !== 'undefined' && StudioSteps.refreshStep4CharacterExclusiveHint) {
            StudioSteps.refreshStep4CharacterExclusiveHint();
          }
        } catch (ePnH) {}
      });
    }
    document.querySelectorAll('input[name="characterSourceMode"]').forEach(function (inp) {
      inp.addEventListener('change', function () {
        if (!inp.checked) return;
        if (typeof StudioSteps.setCharacterSourceMode === 'function') {
          StudioSteps.setCharacterSourceMode(inp.value);
        }
        try {
          if (typeof StudioSteps.refreshStep4CharacterExclusiveHint === 'function') {
            StudioSteps.refreshStep4CharacterExclusiveHint();
          }
        } catch (eRadH) {}
        try {
          saveSession();
        } catch (eRad) {}
      });
    });
    updateUgcRealStepVisibility();
    document.addEventListener('studio:applyStep4CharacterSourceUI', function () {
      try {
        updateCharacterLibrarySaveVisibility();
      } catch (eLibVis) {}
    });
    var charLibNameEl = document.getElementById('characterLibraryName');
    if (charLibNameEl) {
      charLibNameEl.addEventListener('input', function () {
        try {
          saveSession();
        } catch (eNm) {}
      });
    }
    var charLibSel = document.getElementById('characterLibrarySelect');
    if (charLibSel) {
      charLibSel.addEventListener('change', function () {
        var id = charLibSel.value;
        var rec = _characterLibraryById[id];
        if (rec) {
          var rLibEl = document.getElementById('characterSourceLibrary');
          if (rLibEl) {
            rLibEl.checked = true;
            document.body.dataset.studioCharacterSourceMode = 'library';
            try {
              if (typeof StudioSteps.applyStep4CharacterSourceUI === 'function') {
                StudioSteps.applyStep4CharacterSourceUI();
              }
            } catch (eLsyn) {}
          }
          var urls = rec.reference_images || [];
          var url = urls[0] || rec.thumbnail;
          if (url && typeof StudioSteps !== 'undefined' && StudioSteps.setPrimaryCharacterUrl) {
            StudioSteps.setPrimaryCharacterUrl(url);
            try {
              updateCharacterLibrarySaveVisibility();
            } catch (eLibUrl) {}
            try {
              maybeKickVoiceDesignAfterCharacterReady();
            } catch (eLibVk) {}
          }
          try {
            var dna = rec.character_dna;
            if (dna && typeof dna === 'object' && dna.character_brief) {
              var bEl = document.getElementById('characterBrief');
              if (bEl) bEl.value = String(dna.character_brief);
            }
            if (dna && typeof dna === 'object' && dna.portrait_image_prompt) {
              var hidPr = document.getElementById('portraitImagePromptHidden');
              if (hidPr) hidPr.value = String(dna.portrait_image_prompt);
            }
          } catch (eBrief) {}
        }
        try {
          saveSession();
        } catch (eSvLib) {}
        try {
          if (typeof StudioSteps !== 'undefined' && StudioSteps.refreshStep4CharacterExclusiveHint) {
            StudioSteps.refreshStep4CharacterExclusiveHint();
          }
        } catch (eLibH) {}
        if (id && StudioAPI.updateCharacter) {
          StudioAPI.updateCharacter(id, { last_used_at: new Date().toISOString() }).catch(function () {});
        }
      });
    }
    var btnCharLibRef = document.getElementById('btnCharacterLibraryRefresh');
    if (btnCharLibRef) {
      btnCharLibRef.addEventListener('click', function () {
        refreshCharacterLibrarySelect();
      });
    }
    var btnCharLibSave = document.getElementById('btnCharacterLibrarySave');
    if (btnCharLibSave) {
      btnCharLibSave.addEventListener('click', function () {
        var statusEl = document.getElementById('characterLibrarySaveStatus');
        var nameReq = getCharacterLibraryNameForSave();
        if (!nameReq) {
          alert('Enter a library name in the field above (or a Character look — first line is used as fallback), then click Save to library.');
          return;
        }
        var cloudSv =
          typeof StudioAuth !== 'undefined' &&
          StudioAuth.isCloudConfigured &&
          StudioAuth.isCloudConfigured();
        var authedSv =
          typeof StudioAuth !== 'undefined' && StudioAuth.isAuthEnabled && StudioAuth.isAuthEnabled();
        if (!cloudSv || !authedSv) {
          alert('Sign in via Account to save characters.');
          return;
        }
        var body = buildCharacterLibraryCreatePayload();
        if (!body) {
          alert('Upload, approve, or auto-generate a character first (need an http(s) image URL in slot 1).');
          return;
        }
        btnCharLibSave.disabled = true;
        if (statusEl) {
          statusEl.style.display = 'block';
          statusEl.textContent = 'Saving character…';
        }
        StudioAPI.createCharacter(body)
          .then(function () {
            if (statusEl) statusEl.textContent = 'Saved to character library.';
            refreshCharacterLibrarySelect();
          })
          .catch(function (err) {
            if (statusEl) statusEl.textContent = 'Save failed: ' + (err.message || err);
          })
          .finally(function () {
            btnCharLibSave.disabled = false;
          });
      });
    }
    populateSelect('imageModel', StudioData.imageModels);
    populateSelect('animationModel', StudioData.animationModels);
    populateSelect('subtitleTemplate', StudioData.subtitleTemplates);
    var sub12 = document.getElementById('subtitleTemplateStep12');
    if (sub12 && StudioData.subtitleTemplates) {
      StudioData.subtitleTemplates.forEach(function (opt) {
        var o = document.createElement('option');
        o.value = opt.value;
        o.textContent = opt.label || opt.value;
        sub12.appendChild(o);
      });
    }

    document.getElementById('language').addEventListener('change', function () {
      var customVo = document.getElementById('voiceIdCustom');
      if (customVo) customVo.value = '';
      _voicesCache = {};
      updateVoiceDropdown();
      if (typeof refreshStep7VoiceBlock === 'function') refreshStep7VoiceBlock();
      invalidateCharacterBriefAiSuggestionCache();
      restartCharacterPrefetchFromUserInput();
    });
    document.getElementById('country').addEventListener('change', function () {
      invalidateCharacterBriefAiSuggestionCache();
      restartCharacterPrefetchFromUserInput();
    });
    updateVoiceDropdown();

    var promptElPrefetch = document.getElementById('prompt');
    if (promptElPrefetch) {
      var promptDebounce = null;
      promptElPrefetch.addEventListener('input', function () {
        invalidateCharacterBriefAiSuggestionCache();
        try {
          clearTimeout(promptDebounce);
        } catch (e) {}
        promptDebounce = setTimeout(function () {
          promptDebounce = null;
          /* Do not call restartCharacterPrefetchFromUserInput() here: it bumps _characterPrefetchGen and drops
           * in-flight /api/generate-character responses, so the Character approval step can stay on "Generating…"
           * for minutes until the Phase 1 job eventually stores influencer_image. Chips only need cache invalidation. */
          invalidateCharacterBriefAiSuggestionCache();
          try {
            if (typeof currentStep === 'number' && currentStep === 4) {
              refreshCharacterBriefAiSuggestions(false);
            }
          } catch (eSugPrompt) {}
        }, 900);
      });
    }

    var characterBriefEl = document.getElementById('characterBrief');
    if (characterBriefEl) {
      var briefVoiceDesignDebounce = null;
      function scheduleVoiceDesignFromCharacterBrief() {
        try {
          clearTimeout(briefVoiceDesignDebounce);
        } catch (eBd) {}
        briefVoiceDesignDebounce = setTimeout(function () {
          briefVoiceDesignDebounce = null;
          try {
            maybeKickVoiceDesignAfterCharacterReady();
          } catch (eVkBrief) {}
        }, 2000);
      }
      characterBriefEl.addEventListener('input', function () {
        scheduleVoiceDesignFromCharacterBrief();
        try {
          if (typeof StudioSteps !== 'undefined' && StudioSteps.refreshStep4CharacterExclusiveHint) {
            StudioSteps.refreshStep4CharacterExclusiveHint();
          }
        } catch (eBriefH) {}
      });
      characterBriefEl.addEventListener('blur', function () {
        try {
          clearTimeout(briefVoiceDesignDebounce);
        } catch (eBl) {}
        briefVoiceDesignDebounce = null;
        try {
          maybeKickVoiceDesignAfterCharacterReady();
        } catch (eVkBlur) {}
        /* If still on step 4, restart the prefetch so the new brief text is sent to Kie. */
        if (currentStep === 4 && shouldPrefetchAutoCharacter() && characterBriefEl.value.trim()) {
          restartCharacterPrefetchFromUserInput();
        }
      });
    }

    var simModeEl = document.getElementById('simulationMode');
    if (simModeEl) {
      simModeEl.addEventListener('change', function () {
        if (simModeEl.checked) {
          _characterPrefetchGen++;
          _characterPrefetchPromise = null;
          _characterReviewPendingUrl = null;
          try {
            clearTimeout(_characterPrefetchDebounceTimer);
          } catch (e2) {}
          _characterPrefetchDebounceTimer = null;
        } else {
          restartCharacterPrefetchFromUserInput();
        }
      });
    }

    document.getElementById('btnStartGenerate').addEventListener('click', startGeneration);

    var btnPrefsWizardNext = document.getElementById('btnPrefsWizardNext');
    if (btnPrefsWizardNext) {
      btnPrefsWizardNext.addEventListener('click', function () {
        if (!step6AllPreferenceTextsFilled()) {
          alert(
            isUgcRealFlow()
              ? 'Fill Target audience, Main problem, and Key benefits & CTA (all three) first — or wait until Phase 1 fills them.'
              : 'Fill Headline (TEXT 1), Key message (TEXT 2), and Call to action (TEXT 3) first — or wait until Phase 1 fills them.'
          );
          return;
        }
        if (isUgcRealFlow()) {
          goToStep(studioUgcCharGateStep());
          return;
        }
        if (isStudioProductVideo()) {
          goToStep(studioVoStep());
          return;
        }
        goToStep(studioNonUgcCharacterStep());
      });
    }

    // Step 6: Character preview — Approve and Regenerate buttons
    var btnApproveChar = document.getElementById('btnApproveChar');
    if (btnApproveChar) {
      btnApproveChar.addEventListener('click', function () {
        approveCharacterForPipeline();
      });
    }
    var btnRegenerateChar = document.getElementById('btnRegenerateChar');
    if (btnRegenerateChar) {
      btnRegenerateChar.addEventListener('click', function () {
        runCharacterRegenerateFromUI(btnRegenerateChar);
      });
    }
    var btnNonUgcPortraitRetry = document.getElementById('btnNonUgcCharPortraitRetry');
    if (btnNonUgcPortraitRetry) {
      btnNonUgcPortraitRetry.addEventListener('click', function () {
        restartCharacterPrefetchFromUserInput();
        try {
          maybePrefetchAutoCharacter();
        } catch (eRetryPf) {}
        try {
          refreshNonUgcCharacterPortraitStatus();
        } catch (eRetryRf) {}
      });
    }
    var btnUgcCharGateApprove = document.getElementById('btnUgcCharGateApprove');
    if (btnUgcCharGateApprove) {
      btnUgcCharGateApprove.addEventListener('click', function () {
        approveCharacterForPipeline();
      });
    }
    var btnUgcCharGateRegenerate = document.getElementById('btnUgcCharGateRegenerate');
    if (btnUgcCharGateRegenerate) {
      btnUgcCharGateRegenerate.addEventListener('click', function () {
        runCharacterRegenerateFromUI(btnUgcCharGateRegenerate);
      });
    }

    // Step 6 (Preferences): Generate VO (Phase 2)
    var btnGenerateVO = document.getElementById('btnGenerateVO');
    if (btnGenerateVO) {
      ['text1', 'text2', 'text3'].forEach(function (tid) {
        var tel = document.getElementById(tid);
        if (tel) {
          tel.addEventListener('input', function () {
            try {
              updateGenerateVOButtonState();
            } catch (eT) {}
          });
          tel.addEventListener('change', function () {
            try {
              updateGenerateVOButtonState();
            } catch (eT2) {}
          });
        }
      });
      updateGenerateVOButtonState();
      btnGenerateVO.addEventListener('click', function () {
        if (!phase1JobId) {
          alert('Start generation first (step 5) so Phase 1 can produce TEXT 1–3.');
          return;
        }
        if (!step6AllPreferenceTextsFilled()) {
          alert(
            isUgcRealFlow()
              ? 'Fill Target audience, Main problem, and Key benefits & CTA (all three) — or wait until Phase 1 finishes. Continue to Phase 2 uses these fields for the next segment.'
              : 'Fill Headline (TEXT 1), Key message (TEXT 2), and Call to action (TEXT 3) first — or wait until Phase 1 finishes and they appear here. Generate VO runs Phase 2 using all three.'
          );
          return;
        }
        if (
          _characterReviewPendingUrl &&
          !StudioSteps.hasUploadedCharacter() &&
          !(typeof StudioSteps.isProductNoOnScreenCharacter === 'function' && StudioSteps.isProductNoOnScreenCharacter())
        ) {
          applyCharacterUrl(_characterReviewPendingUrl);
          hideCharPreview();
        }
        var prevLabel = btnGenerateVO.textContent;
        btnGenerateVO.setAttribute('data-studio-vo-busy', '1');
        btnGenerateVO.disabled = true;
        btnGenerateVO.textContent = 'Starting…';
        function finishGenerateVoButton() {
          btnGenerateVO.removeAttribute('data-studio-vo-busy');
          btnGenerateVO.textContent = prevLabel;
          updateGenerateVOButtonState();
        }
        promiseWithTimeout(
          uploadPendingZones(),
          240000,
          'File uploads timed out (4 minutes). Check the network, finish or remove pending uploads in step 4/9, then try again.'
        )
          .then(function () {
            if (isUgcRealFlow()) {
              var jidU = String(phase1JobId).trim();
              return StudioAPI.getJob(jidU).then(function (preCheck) {
                if (preCheck.status === 'failed') {
                  _phase1LastPolledStatus = 'failed';
                  finishGenerateVoButton();
                  alert(
                    'Phase 1 job failed' +
                    (preCheck.error ? ': ' + preCheck.error : '') +
                    '.\n\nGo back to step 5 and click Start generation to create a new job.'
                  );
                  return { _ugc_real_resume_same_job: true };
                }
                if (preCheck.status === 'completed') {
                  finishGenerateVoButton();
                  alert(
                    'Phase 1 job already completed. Go back to step 5 and click Start generation to create a new job if needed.'
                  );
                  return { _ugc_real_resume_same_job: true };
                }
                if (preCheck.status === 'processing') {
                  _ugcRealOfferResumeIssued = true;
                  finishGenerateVoButton();
                  ugcRealOfferApproveGoToCharacterGate();
                  if (!_musicAutoStarted) {
                    _musicAutoStarted = true;
                    StudioAPI.generateMusic(StudioSteps.collectMusicPayload())
                      .then(function (data) {
                        applyMusicGenerateResult(data);
                        if (data.music_url || data.music_description) {
                          return patchStudioMusicIntermediates(data);
                        }
                      })
                      .catch(function () {
                        _musicAutoStarted = false;
                      });
                  } else if (
                    phase1JobId &&
                    (collectedIntermediates.music_url || collectedIntermediates.music_description)
                  ) {
                    patchStudioMusicIntermediates({
                      music_url: collectedIntermediates.music_url,
                      music_description: collectedIntermediates.music_description
                    }).catch(function () {});
                  }
                  return { __ugc_no_op: true };
                }
                if (preCheck.status === 'paused' && (preCheck.current_step || '').trim() !== 'step_parse') {
                  _ugcRealOfferResumeIssued = true;
                  finishGenerateVoButton();
                  ugcRealOfferApproveGoToCharacterGate();
                  if (!_musicAutoStarted) {
                    _musicAutoStarted = true;
                    StudioAPI.generateMusic(StudioSteps.collectMusicPayload())
                      .then(function (data) {
                        applyMusicGenerateResult(data);
                        if (data.music_url || data.music_description) {
                          return patchStudioMusicIntermediates(data);
                        }
                      })
                      .catch(function () {
                        _musicAutoStarted = false;
                      });
                  } else if (
                    phase1JobId &&
                    (collectedIntermediates.music_url || collectedIntermediates.music_description)
                  ) {
                    patchStudioMusicIntermediates({
                      music_url: collectedIntermediates.music_url,
                      music_description: collectedIntermediates.music_description
                    }).catch(function () {});
                  }
                  return { __ugc_no_op: true };
                }
                return StudioAPI.patchIntermediates(jidU, buildUgcRealOfferResumePatchBody())
                  .then(function () {
                    return StudioAPI.resumeJob(jidU, { stop_after_scene_animations: true });
                  })
                  .then(function () {
                    _ugcRealOfferResumeIssued = true;
                  });
              })
                .then(function (ugcBranch) {
                  if (
                    ugcBranch &&
                    typeof ugcBranch === 'object' &&
                    (ugcBranch._ugc_real_resume_same_job || ugcBranch.__ugc_no_op)
                  )
                    return;
                  phase2JobId = null;
                  phase3JobId = null;
                  _phase3AnimateJobId = null;
                  _scenePromptsJobStarted = false;
                  currentJobId = jidU;
                  watchJob(jidU);
                  finishGenerateVoButton();
                  ugcRealOfferApproveGoToCharacterGate();
                  saveSession();
                  if (!_musicAutoStarted) {
                    _musicAutoStarted = true;
                    StudioAPI.generateMusic(StudioSteps.collectMusicPayload())
                      .then(function (data) {
                        applyMusicGenerateResult(data);
                        if (data.music_url || data.music_description) {
                          return patchStudioMusicIntermediates(data);
                        }
                      })
                      .catch(function () {
                        _musicAutoStarted = false;
                      });
                  } else if (
                    phase1JobId &&
                    (collectedIntermediates.music_url || collectedIntermediates.music_description)
                  ) {
                    patchStudioMusicIntermediates({
                      music_url: collectedIntermediates.music_url,
                      music_description: collectedIntermediates.music_description
                    }).catch(function () {});
                  }
                  return { _ugc_real_resume_same_job: true };
                });
            }
            var exP2 = phase2JobId && String(phase2JobId).trim();
            if (exP2 && exP2.length >= 8) {
              return StudioAPI.getJob(exP2).then(function (jobDup) {
                var jstDup = (jobDup && jobDup.status) || '';
                if (jstDup === 'failed' || jstDup === 'aborted') {
                  phase2JobId = null;
                  _nonUgcVoPhase2KickoffForPhase1 = null;
                  try {
                    saveSession();
                  } catch (eClrDup) {}
                  var payloadFresh = StudioSteps.collectPhase2Payload(phase1JobId);
                  return StudioAPI.generate(payloadFresh);
                }
                if (jstDup === 'completed') {
                  finishGenerateVoButton();
                  alert(
                    'Phase 2 for this session is already completed. Use step 9 (VO) or later steps to review output — do not start another Phase 2 from here.'
                  );
                  goToStep(studioVoStep());
                  return { _studio_phase2_duplicate_skip: true };
                }
                var voDup =
                  jobDup.intermediates &&
                  jobDup.intermediates.vo_script &&
                  String(jobDup.intermediates.vo_script).trim().length >= 12;
                finishGenerateVoButton();
                alert(
                  'Phase 2 is already running for this session (job ' +
                    exP2.slice(0, 8) +
                    '…).\n\nOpening step ' +
                    (voDup ? '9 (VO script)' : '7 (character)') +
                    '. Do not start a second Phase 2 from Preferences — it breaks polling and can stall the UI.'
                );
                goToStep(
                  voDup
                    ? studioVoStep()
                    : isStudioProductVideo()
                      ? studioVoStep()
                      : studioNonUgcCharacterStep()
                );
                return { _studio_phase2_duplicate_skip: true };
              }).catch(function () {
                var payloadRetry = StudioSteps.collectPhase2Payload(phase1JobId);
                return StudioAPI.generate(payloadRetry);
              });
            }
            var payload = StudioSteps.collectPhase2Payload(phase1JobId);
            return StudioAPI.generate(payload);
          })
          .then(function (res) {
            if (res && res._ugc_real_resume_same_job) return;
            if (res && res._studio_phase2_duplicate_skip) return;
            var jid = res && (res.job_id != null ? res.job_id : res.id);
            if (jid == null || String(jid).trim() === '') {
              finishGenerateVoButton();
              alert('Generate VO: server returned no job_id. Open API Log and check the last POST /api/generate response.');
              return;
            }
            phase2JobId = String(jid).trim();
            phase3JobId = null;
            _phase3AnimateJobId = null;
            _scenePromptsJobStarted = false;
            currentJobId = phase2JobId;
            goToStep(
              isUgcRealFlow()
                ? studioVoStep()
                : isStudioProductVideo()
                  ? studioVoStep()
                  : studioNonUgcCharacterStep()
            );
            pollJob();
            if (pollIntervalId) clearInterval(pollIntervalId);
            _studioPollMs = 2000;
            pollIntervalId = setInterval(pollJob, 2000);
            try {
              studioEnsureJobPollInterval();
            } catch (ePiGenVo) {}
            if (sseSource) {
              try {
                sseSource.close();
              } catch (eSse) {}
              sseSource = null;
              _sseActive = false;
            }
            StudioAPI.connectSSE(currentJobId, function (eventType, data) {
              if (data.event_type === 'complete' || data.event_type === 'error' || data.event_type === 'abort') {
                if (pollIntervalId) clearInterval(pollIntervalId);
                pollIntervalId = null;
                _sseActive = false;
              } else if (data.event_type === 'pause') {
                _sseActive = false;
                if (!pollIntervalId) pollIntervalId = setInterval(pollJob, 2000);
              }
              pollJob();
            }).then(function (es) {
              sseSource = es;
              _sseActive = true;
            });
            finishGenerateVoButton();
            saveSession();
            // Kick off music if step 6 did not already start it (text_1/2/3 are enough; vo_script is optional)
            if (!_musicAutoStarted) {
              _musicAutoStarted = true;
              StudioAPI.generateMusic(StudioSteps.collectMusicPayload())
                .then(function (data) {
                  applyMusicGenerateResult(data);
                  if (data.music_url || data.music_description) {
                    return patchStudioMusicIntermediates(data);
                  }
                })
                .catch(function () {
                  _musicAutoStarted = false;
                });
            } else if (phase2JobId && (collectedIntermediates.music_url || collectedIntermediates.music_description)) {
              patchStudioMusicIntermediates({
                music_url: collectedIntermediates.music_url,
                music_description: collectedIntermediates.music_description
              }).catch(function () {});
            }
          })
          .catch(function (err) {
            finishGenerateVoButton();
            alert('Generate VO failed: ' + (err.message || err));
          });
      });
    }

    // Step 8: Music
    var btnRegenerateMusic = document.getElementById('btnRegenerateMusic');
    if (btnRegenerateMusic) {
      btnRegenerateMusic.addEventListener('click', function () {
        btnRegenerateMusic.disabled = true;
        StudioAPI.generateMusic(StudioSteps.collectMusicPayload())
          .then(function (data) {
            applyMusicGenerateResult(data);
            if (data.music_url || data.music_description) {
              return patchStudioMusicIntermediates(data);
            }
          })
          .then(function () {
            var ms = document.getElementById('musicStatus');
            if (ms && collectedIntermediates.music_url) {
              ms.textContent = 'Music ready. Approve or regenerate again.';
            }
            btnRegenerateMusic.disabled = false;
          })
          .catch(function (err) {
            btnRegenerateMusic.disabled = false;
            alert('Music failed: ' + (err.message || err));
          });
      });
    }

    var btnUgcRealContinueToPhase2Grid = document.getElementById('btnUgcRealContinueToPhase2Grid');
    if (btnUgcRealContinueToPhase2Grid) {
      btnUgcRealContinueToPhase2Grid.addEventListener('click', function () {
        var jid = ugcRealPrimaryPipelineJobId();
        if (!jid) {
          alert('Start the UGC Real job first.');
          return;
        }
        btnUgcRealContinueToPhase2Grid.disabled = true;
        ugcRealResumeFromNineCellReview(jid)
          .then(function () {
            phase1JobId = jid;
            goToStep(studioVoStep());
            watchJob(jid);
          })
          .catch(function (err) {
            if (err && err._ugc_real_skip) {
              if (err._ugc_real_open_step8) {
                phase1JobId = jid;
                goToStep(studioVoStep());
                watchJob(jid);
              }
              if (err.message) alert(err.message);
              return;
            }
            var msg = err && err.message ? String(err.message) : '';
            if (/Resume failed:\s*409\b/.test(msg) || /\b409\b/.test(msg) && /already running/i.test(msg)) {
              alert(
                'The job is already running (e.g. Nano Banana / Kie). Open the VO step and wait for the grid — do not press Continue again until the job is Paused after the nine-cell storyboard.'
              );
              phase1JobId = jid;
              goToStep(studioVoStep());
              watchJob(jid);
              return;
            }
            alert('Could not continue UGC Real toward the 3×3 grid: ' + (err.message || err));
          })
          .finally(function () {
            btnUgcRealContinueToPhase2Grid.disabled = false;
          });
      });
    }

    var btnUgcRealRestartScenePlan = document.getElementById('btnUgcRealRestartScenePlan');
    if (btnUgcRealRestartScenePlan) {
      btnUgcRealRestartScenePlan.addEventListener('click', function () {
        var jid = ugcRealPrimaryPipelineJobId();
        if (!jid) {
          alert('No UGC Real job to restart yet.');
          return;
        }
        btnUgcRealRestartScenePlan.disabled = true;
        StudioAPI.restartJob(jid, 'step_1')
          .then(function () {
            phase1JobId = jid;
            goToStep(studioUgcNineStep());
            watchJob(jid);
          })
          .catch(function (err) {
            alert('Could not regenerate nine-cell plan: ' + (err.message || err));
          })
          .finally(function () {
            btnUgcRealRestartScenePlan.disabled = false;
          });
      });
    }

    var btnUgcRealApproveGrids = document.getElementById('btnUgcRealApproveGrids');
    if (btnUgcRealApproveGrids) {
      btnUgcRealApproveGrids.addEventListener('click', function () {
        var jid = ugcRealPrimaryPipelineJobId();
        if (!jid) {
          alert('No paused UGC Real grid-review job found.');
          return;
        }
        StudioAPI.ensureCanCallProtectedApi()
          .then(function (ok) {
            if (!ok) {
              alert(
                'Cannot resume: sign in with Studio (or set an API key). Open the account menu and sign in, then try again.'
              );
              return;
            }
            return StudioAPI.getJob(jid).then(function (jb) {
              var pf = ugcRealApproveGridsPreflight(jb);
              if (!pf.ok) {
                alert(pf.message || pf.title || 'Cannot approve grids yet.');
                return;
              }
              btnUgcRealApproveGrids.disabled = true;
              _ugcRealPostGridResumePending = true;
              var body = { stop_after_scene_animations: true };
              var liveTApprove = document.getElementById('step7LiveStatusText');
              var liveWApprove = document.getElementById('step7LiveStatus');
              if (liveTApprove)
                liveTApprove.textContent =
                  'Sending resume… Next the server generates per-cell VO audio, lip-sync clips, and nine animated videos (several minutes). After this screen, use Next → Scene images to see nine stills sync.';
              if (liveWApprove) {
                liveWApprove.style.display = 'block';
                liveWApprove.classList.remove('studio-status-error', 'studio-status-success');
                liveWApprove.classList.add('studio-step7-running');
              }
              var tryResume = function () {
                return StudioAPI.resumeJob(jid, body);
              };
              return tryResume()
                .catch(function (firstErr) {
                  var m = String((firstErr && firstErr.message) || firstErr || '');
                  // Server may sync processing→paused on first resume; one retry covers that race.
                  if (/Resume failed:\s*400\b/.test(m) && /paused/i.test(m)) {
                    return new Promise(function (r) {
                      setTimeout(r, 500);
                    }).then(tryResume);
                  }
                  if (/Resume failed:\s*409\b/.test(m) || (/\b409\b/.test(m) && /running/i.test(m))) {
                    return StudioAPI.getJob(jid).then(function (jb2) {
                      var stJ = jb2 && jb2.status;
                      if (stJ === 'processing') return { _ugc_grid_resume_already_running: true };
                      throw firstErr;
                    });
                  }
                  throw firstErr;
                })
                .then(function (res) {
                  phase1JobId = jid;
                  goToStep(studioMusicStep());
                  watchJob(jid);
                  saveSession();
                  var alreadyRun = !!(res && res._ugc_grid_resume_already_running);
                  if (liveTApprove) {
                    liveTApprove.textContent = alreadyRun
                      ? 'Job was already running. The activity panel on Background music shows live progress (VO audio → lip-sync → animations). Use Next to Scene images for nine stills, then Final.'
                      : 'Resume accepted — on Background music you will see an activity panel tracking VO audio, lip-sync, and nine animations. Use Next to Scene images for stills, then Final video.';
                  }
                  if (liveWApprove) {
                    liveWApprove.classList.remove('studio-step7-running');
                    liveWApprove.classList.add('studio-status-success');
                  }
                })
                .catch(function (err) {
                  _ugcRealPostGridResumePending = false;
                  alert('Could not continue from grid review: ' + (err.message || err));
                })
                .finally(function () {
                  StudioAPI.getJob(jid)
                    .then(function (latest) {
                      refreshUgcRealApproveGridsButton(latest, (latest && latest.intermediates) || {});
                    })
                    .catch(function () {
                      btnUgcRealApproveGrids.disabled = false;
                    });
                });
            });
          })
          .catch(function (err) {
            _ugcRealPostGridResumePending = false;
            alert('Could not continue from grid review: ' + (err.message || err));
          });
      });
    }

    var btnUgcRealRestartGrids = document.getElementById('btnUgcRealRestartGrids');
    if (btnUgcRealRestartGrids) {
      btnUgcRealRestartGrids.addEventListener('click', function () {
        var jid = ugcRealPrimaryPipelineJobId();
        if (!jid) {
          alert('No UGC Real job to restart yet.');
          return;
        }
        btnUgcRealRestartGrids.disabled = true;
        StudioAPI.restartJob(jid, 'step_3')
          .then(function () {
            phase1JobId = jid;
            goToStep(studioVoStep());
            watchJob(jid);
          })
          .catch(function (err) {
            alert('Could not regenerate grids: ' + (err.message || err));
          })
          .finally(function () {
            btnUgcRealRestartGrids.disabled = false;
          });
      });
    }

    var ugcGridReviewList = document.getElementById('ugcRealGridReviewList');
    if (ugcGridReviewList) {
      ugcGridReviewList.addEventListener('click', function (ev) {
        var btn = ev.target.closest('button[data-scene-index][data-action]');
        if (!btn || !isUgcRealFlow() || !ugcGridReviewList.contains(btn)) return;
        var idx = parseInt(btn.getAttribute('data-scene-index') || '', 10);
        if (!isFinite(idx) || idx < 0 || idx > 8) return;
        var scenes = window._scenePromptsForImages;
        if (!scenes || !scenes.length) {
          alert('Nine-cell prompts are not loaded yet. Wait for the storyboard plan or refresh.');
          return;
        }
        var action = btn.getAttribute('data-action') || 'fix';
        var card = btn.closest('.studio-media-card');
        var input = card ? card.querySelector('input.studio-input') : null;
        var correction = input ? String(input.value || '').trim() : '';
        var scene = scenes[idx] || {};
        var prompt =
          (scene.first_prompt || '').trim() ||
          (scene.image_prompt || '').trim() ||
          (scene.second_prompt || '').trim() ||
          ('Grid cell ' + (idx + 1));
        var gcRow = ugcRealFindGridCellForIndex(collectedIntermediates.grid_cells, idx + 1);
        var curSlot = currentSceneImages && currentSceneImages[idx];
        var currentUrl = null;
        if (gcRow && gcRow.image_url && String(gcRow.image_url).indexOf('http') === 0) {
          currentUrl = stripStudioSceneImageCacheBuster(String(gcRow.image_url));
        } else if (curSlot && typeof curSlot === 'string' && curSlot.length > 5) {
          currentUrl = stripStudioSceneImageCacheBuster(curSlot);
        }
        var isLast = idx === 8;
        var payload = StudioSteps.collectSceneImagePayload(
          idx,
          prompt,
          action === 'fix' ? (correction || undefined) : undefined,
          isLast,
          action === 'fix' && currentUrl ? currentUrl : undefined
        );
        var jid = ugcRealPrimaryPipelineJobId();
        if (!jid) {
          alert('No UGC Real job id — refresh the page or return to step 5 and ensure generation is linked.');
          return;
        }
        var allBtns = card ? card.querySelectorAll('button[data-scene-index]') : [];
        for (var bi = 0; bi < allBtns.length; bi++) allBtns[bi].disabled = true;
        var imgEl = card ? card.querySelector('img') : null;
        var placeholderEl = card ? card.querySelector('.studio-scene-image-placeholder') : null;
        if (card && (imgEl || placeholderEl)) {
          var wrapPh = imgEl || placeholderEl;
          var newPlaceholder = document.createElement('div');
          newPlaceholder.className = 'studio-scene-image-placeholder';
          newPlaceholder.textContent = 'Regenerating…';
          wrapPh.parentNode.replaceChild(newPlaceholder, wrapPh);
        }
        if (!window._sceneImageErrors || window._sceneImageErrors.length !== (currentSceneImages || []).length) {
          window._sceneImageErrors = new Array(Math.max(9, (currentSceneImages || []).length));
        }
        StudioAPI.generateSceneImage(payload)
          .then(function (data) {
            if (data && data.image_url) {
              var url = data.image_url;
              var sep = url.indexOf('?') >= 0 ? '&' : '?';
              var disp = url + sep + '_=' + Date.now();
              try {
                collectedIntermediates.grid_cells = ugcRealMergeGridCellImageUrl(collectedIntermediates.grid_cells, idx, url);
              } catch (eMgc) {}
              if (!Array.isArray(currentSceneImages)) currentSceneImages = [];
              while (currentSceneImages.length < 9) currentSceneImages.push(undefined);
              currentSceneImages[idx] = disp;
              studioPinSceneImageSlot(idx, disp, url);
              if (window._sceneImageErrors) window._sceneImageErrors[idx] = null;
              var planForRender = ugcRealGetStoryboardPlan({ nine_cell_plan: collectedIntermediates.nine_cell_plan }) || _ugcRealScenePlan;
              var gUrl = '';
              try {
                gUrl = (collectedIntermediates.grid_image_url || '').trim();
              } catch (eGurl) {}
              renderUgcRealGridReview(
                planForRender,
                gUrl,
                collectedIntermediates.grid_cells,
                collectedIntermediates.frame_routing || _ugcRealFrameClassifications
              );
              var urlsForPatch = ugcRealOrderedSceneImagesFromGridCells(collectedIntermediates.grid_cells);
              while (urlsForPatch.length < 9) urlsForPatch.push(null);
              for (var pi = 0; pi < urlsForPatch.length; pi++) {
                if (urlsForPatch[pi] == null && currentSceneImages[pi] && typeof currentSceneImages[pi] === 'string') {
                  urlsForPatch[pi] = stripStudioSceneImageCacheBuster(currentSceneImages[pi]);
                }
              }
              return StudioAPI.patchIntermediates(
                jid,
                { intermediates: { scene_images: urlsForPatch, grid_cells: collectedIntermediates.grid_cells } },
                { skipWaitingOverlay: true }
              );
            }
            if (window._sceneImageErrors) window._sceneImageErrors[idx] = 'No image_url in API response';
            throw new Error('No image_url in API response');
          })
          .catch(function (err) {
            if (window._sceneImageErrors) {
              window._sceneImageErrors[idx] = (err && err.message) ? String(err.message) : String(err);
            }
            var planForRender2 = ugcRealGetStoryboardPlan({ nine_cell_plan: collectedIntermediates.nine_cell_plan }) || _ugcRealScenePlan;
            var gUrl2 = '';
            try {
              gUrl2 = (collectedIntermediates.grid_image_url || '').trim();
            } catch (eG2) {}
            renderUgcRealGridReview(
              planForRender2,
              gUrl2,
              collectedIntermediates.grid_cells,
              collectedIntermediates.frame_routing || _ugcRealFrameClassifications
            );
            alert((action === 'fix' ? 'Fix' : 'Regenerate') + ' failed: ' + (err.message || err));
          })
          .finally(function () {
            for (var bj = 0; bj < allBtns.length; bj++) allBtns[bj].disabled = false;
          });
      });
    }

    var btnApproveMusic = document.getElementById('btnApproveMusic');
    if (btnApproveMusic) {
      btnApproveMusic.addEventListener('click', function () {
        if (isUgcRealFlow()) {
          if (!ugcRealPrimaryPipelineJobId()) {
            alert('Finish the VO & grid step (or restore a session with an active job) before continuing.');
            return;
          }
        } else if (!phase2JobId) {
          alert('Complete VO step first.');
          return;
        }
        goToStep(isUgcRealFlow() ? studioFinalStep() : studioAssetsStep()); // UGC Real: skip to Final; product → Scene assets
      });
    }

    // Step 10: Generate scene prompts — start Job C (pause after step_3) with scene assets from form
    var btnGenerateScenePrompts = document.getElementById('btnGenerateScenePrompts');
    if (btnGenerateScenePrompts) {
      btnGenerateScenePrompts.addEventListener('click', function () {
        var seedJobP3 = isUgcRealFlow() ? ugcRealPrimaryPipelineJobId() : phase2JobId;
        var sjP3 = seedJobP3 != null ? String(seedJobP3).trim() : '';
        if (!sjP3 || sjP3.length < 8) {
          alert('Complete VO step first.');
          return;
        }
        // Clear any stale scene data from a previous run or restored session.
        resetStudioMonotonicMediaAccumulators();
        window._scenePromptsForImages = [];
        currentSceneImages = [];
        _lastScenePromptsJson = null;
        _lastSceneImagesRendered = null;
        try {
          var spg = document.getElementById('scenePromptsGrid');
          if (spg) spg.innerHTML = '';
        } catch (eClearSp) {}
        try {
          if (typeof StudioMedia !== 'undefined') StudioMedia.renderSceneImages([], []);
        } catch (eClearImg2) {}
        var payload = StudioSteps.collectPhase3PauseScenePromptsPayload(sjP3);
        uploadPendingZones().then(function () { return StudioAPI.generate(payload);         }).then(function (res) {
          phase3JobId = res.job_id;
          _phase3AnimateJobId = res.job_id;
          currentJobId = phase3JobId;
          if (sseSource) {
            try { sseSource.close(); } catch (eSseP3) {}
            sseSource = null;
            _sseActive = false;
          }
          goToStep(studioPromptsStep());
          try { saveSession(); } catch (eSaveP3) {}
          pollJob();
          if (pollIntervalId) clearInterval(pollIntervalId);
          pollIntervalId = setInterval(pollJob, 2000);
          StudioAPI.connectSSE(currentJobId, function (eventType, data) {
            if (data.event_type === 'complete' || data.event_type === 'error' || data.event_type === 'abort' || data.event_type === 'pause') {
              _sseActive = false;
            }
            pollJob();
          }).then(function (es) {
            sseSource = es;
            _sseActive = true;
          }).catch(function () { _sseActive = false; });
        }).catch(function (err) { alert('Start scene prompts failed: ' + (err.message || err)); });
      });
    }

    // Steps 10–11: Generate all scene images — parallel POST /api/generate-scene-image (server then calls Kie or Vertex).
    var btnGenerateImages = document.getElementById('btnGenerateImages');
    var btnGenerateImagesStep11 = document.getElementById('btnGenerateImagesStep11');
    function runGenerateAllSceneImages() {
      if (!phase3JobId || !window._scenePromptsForImages || !window._scenePromptsForImages.length) {
        alert('Scene prompts not ready yet. Wait for the job to pause after scene prompts.');
        return;
      }
      if (_sceneImagesBatchInFlight) {
        return;
      }
      var scenesCheck = window._scenePromptsForImages;
      var allSlotsAlreadyHaveImages =
        scenesCheck.length > 0 &&
        scenesCheck.every(function (_scene, index) {
          var u = currentSceneImages && currentSceneImages[index];
          return u && typeof u === 'string' && u.length > 5;
        });
      if (allSlotsAlreadyHaveImages) {
        if (
          !window.confirm(
            'Every scene already has an image. Run again to regenerate all scenes? ' +
              'This starts ' +
              scenesCheck.length +
              ' new image API calls (additional credits).'
          )
        ) {
          return;
        }
      }
      _sceneImagesBatchInFlight = true;
      _studioAccumulatedSceneImages = null;
      var controls = [
        [btnGenerateImages, 'Generate images'],
        [btnGenerateImagesStep11, 'Generate all scene images']
      ];
      controls.forEach(function (pair) {
        if (pair[0]) {
          pair[0].disabled = true;
          pair[0].textContent = 'Generating...';
        }
      });
      var scenes = window._scenePromptsForImages;
      studioClearAllSceneImagePins();
      currentSceneImages = new Array(scenes.length);
      window._sceneImageErrors = new Array(scenes.length);
      goToStep(studioImagesStep());
      if (typeof StudioMedia !== 'undefined') StudioMedia.renderSceneImages(currentSceneImages, window._sceneImageErrors);
      _lastSceneImagesRendered = currentSceneImages.slice();
      var totalSlots = scenes.length;
      function updateStep11ProgressLine() {
        var line = document.getElementById('step11ProgressLine');
        if (!line || currentStep !== studioImagesStep()) return;
        var done = (currentSceneImages || []).filter(function (u) {
          return u && typeof u === 'string' && u.length > 5;
        }).length;
        var failed = (currentSceneImages || []).filter(function (u) {
          return u === false;
        }).length;
        var pending = totalSlots - done - failed;
        var parts = [done + ' / ' + totalSlots + ' scene images'];
        if (pending > 0) parts.push('generating…');
        if (failed > 0) parts.push(failed + ' failed');
        if (pending > 0 && done === 0 && failed === 0) {
          parts.push('(each image often ~30–90s; 6 scenes ≈ a few minutes)');
        }
        line.textContent = parts.join(' — ');
        line.style.display = 'block';
      }
      updateStep11ProgressLine();
      var promises = scenes.map(function (scene, index) {
        var taFirst = document.querySelector('#scenePromptsGrid textarea[data-scene-index="' + index + '"][data-prompt-type="first"]');
        var prompt =
          (taFirst && taFirst.value && taFirst.value.trim()) ||
          (scene.first_prompt || '').trim() ||
          (scene.image_prompt || '').trim() ||
          (scene.second_prompt || '').trim() ||
          (scene.motion_prompt || '').trim() ||
          ('Scene ' + (index + 1));
        var isLast = index === scenes.length - 1;
        var payload = StudioSteps.collectSceneImagePayload(index, prompt, null, isLast);
        return StudioAPI.generateSceneImage(payload).then(function (data) {
          if (data.image_url) {
            var rawUrl = data.image_url;
            var sep0 = rawUrl.indexOf('?') >= 0 ? '&' : '?';
            var disp0 = rawUrl + sep0 + '_=' + Date.now();
            currentSceneImages[index] = disp0;
            studioPinSceneImageSlot(index, disp0, rawUrl);
            if (window._sceneImageErrors) window._sceneImageErrors[index] = null;
          } else {
            studioUnpinSceneImageSlot(index);
            currentSceneImages[index] = false;
            if (window._sceneImageErrors) {
              window._sceneImageErrors[index] = 'No image_url in API response';
            }
          }
          if (typeof StudioMedia !== 'undefined') {
            StudioMedia.renderSceneImages(currentSceneImages, window._sceneImageErrors);
          }
          _lastSceneImagesRendered = currentSceneImages.slice();
          updateStep11ProgressLine();
          return index;
        }).catch(function (err) {
          studioUnpinSceneImageSlot(index);
          currentSceneImages[index] = false;
          if (window._sceneImageErrors) {
            window._sceneImageErrors[index] = (err && err.message) ? String(err.message) : String(err);
          }
          if (typeof StudioMedia !== 'undefined') {
            StudioMedia.renderSceneImages(currentSceneImages, window._sceneImageErrors);
          }
          _lastSceneImagesRendered = currentSceneImages.slice();
          updateStep11ProgressLine();
          return index;
        });
      });
      Promise.all(promises).then(function () {
        var patchId = phase3JobId;
        var urlsForPatch = currentSceneImages.map(function (u) {
          if (u && typeof u === 'string' && u.length > 5) return stripStudioSceneImageCacheBuster(u);
          return null;
        });
        StudioAPI.patchIntermediates(patchId, { intermediates: { scene_images: urlsForPatch } }, { skipWaitingOverlay: true })
          .then(function () {
            _phase3AnimateJobId = patchId;
            phase3JobId = patchId;
            if (typeof StudioMedia !== 'undefined') {
              StudioMedia.renderSceneImages(currentSceneImages, window._sceneImageErrors);
            }
            _lastSceneImagesRendered = currentSceneImages.slice();
          })
          .catch(function (err) {
            alert('Patch failed: ' + (err.message || err));
          });
      }).finally(function () {
        _sceneImagesBatchInFlight = false;
        controls.forEach(function (pair) {
          if (pair[0]) {
            pair[0].disabled = false;
            pair[0].textContent = pair[1];
          }
        });
      });
    }
    if (btnGenerateImages) btnGenerateImages.addEventListener('click', runGenerateAllSceneImages);
    if (btnGenerateImagesStep11) btnGenerateImagesStep11.addEventListener('click', runGenerateAllSceneImages);

    // Step 13 (scene videos grid): per-scene Re-animate. Calls /api/animate-scene with the typed
    // motion prompt for one scene only; auto-falls back to whole-job retry if the per-scene call
    // 502s (e.g. provider rejection) and the user opts in.
    var sceneVideosGrid = document.getElementById('sceneVideosGrid');
    if (sceneVideosGrid) {
      sceneVideosGrid.addEventListener('click', function (e) {
        var btn = e.target.closest('button[data-scene-index]');
        if (!btn) return;
        var jid = phase3JobId || _phase3AnimateJobId || currentJobId;
        if (!jid) {
          alert('No phase-3 job to re-animate. Generate scene images first.');
          return;
        }
        if (btn.dataset.studioReanimating === '1') return;
        var idx = parseInt(btn.dataset.sceneIndex, 10);
        if (isNaN(idx)) return;
        var card = btn.closest('.studio-media-card');
        var motionInput = card ? card.querySelector('input[type="text"]') : null;
        var motionPrompt = motionInput && motionInput.value ? motionInput.value.trim() : '';
        var prevLabel = btn.textContent;
        btn.dataset.studioReanimating = '1';
        btn.disabled = true;
        btn.textContent = 'Re-animating…';
        // Show "Animating…" placeholder over the existing video so the user knows something is happening.
        var videoEl = card ? card.querySelector('video') : null;
        if (videoEl) {
          videoEl.style.opacity = '0.35';
          videoEl.style.filter = 'grayscale(0.5)';
        }
        var body = { job_id: jid, scene_index: idx };
        if (motionPrompt) body.motion_prompt = motionPrompt;
        StudioAPI.animateScene(body)
          .then(function (data) {
            if (data && data.video_url && videoEl) {
              videoEl.src = data.video_url;
              videoEl.load();
            }
            try { pollJob(); } catch (e) {}
          })
          .catch(function (err) {
            var msg = (err && err.message) || String(err);
            var fallback = window.confirm(
              'Per-scene re-animate failed: ' + msg + '\n\n' +
              'Fall back to whole-job retry (re-animates ALL ' +
              ((window._scenePromptsForImages && window._scenePromptsForImages.length) || 'all') +
              ' clips)?'
            );
            if (fallback) {
              return StudioAPI.retrySceneAnimations(jid).catch(function (err2) {
                alert('Whole-job retry also failed: ' + ((err2 && err2.message) || String(err2)));
              });
            }
          })
          .finally(function () {
            if (videoEl) {
              videoEl.style.opacity = '';
              videoEl.style.filter = '';
            }
            btn.removeAttribute('data-studio-reanimating');
            btn.disabled = false;
            btn.textContent = prevLabel || 'Re-animate';
          });
      });
    }

    // Step 11: Scene images grid — Regenerate (same prompt) and Fix this image (current image + correction) via delegation
    var sceneImagesGrid = document.getElementById('sceneImagesGrid');
    if (sceneImagesGrid) {
      sceneImagesGrid.addEventListener('click', function (e) {
        var btn = e.target.closest('button[data-scene-index]');
        if (!btn || !window._scenePromptsForImages) return;
        var idx = parseInt(btn.dataset.sceneIndex, 10);
        if (isNaN(idx)) return;
        var action = btn.dataset.action || 'fix';
        var card = btn.closest('.studio-media-card');
        var input = card ? card.querySelector('input[type="text"], .studio-input') : null;
        var correction = input ? input.value.trim() : '';
        var currentUrl =
          currentSceneImages && currentSceneImages[idx] !== undefined && currentSceneImages[idx] !== false
            ? currentSceneImages[idx]
            : null;
        var scenes = window._scenePromptsForImages;
        var scene = scenes[idx] || {};
        var taFirst = document.querySelector('#scenePromptsGrid textarea[data-scene-index="' + idx + '"][data-prompt-type="first"]');
        var prompt =
          (taFirst && taFirst.value && taFirst.value.trim()) ||
          (scene.first_prompt || '').trim() ||
          (scene.image_prompt || '').trim() ||
          (scene.second_prompt || '').trim() ||
          (scene.motion_prompt || '').trim() ||
          ('Scene ' + (idx + 1));
        var isLast = idx === scenes.length - 1;
        var allBtns = card ? card.querySelectorAll('button[data-scene-index]') : [];
        for (var b = 0; b < allBtns.length; b++) allBtns[b].disabled = true;
        var imgEl = card ? card.querySelector('img') : null;
        var placeholderEl = card ? card.querySelector('.studio-scene-image-placeholder') : null;
        if (card && (imgEl || placeholderEl)) {
          var wrap = imgEl || placeholderEl;
          var newPlaceholder = document.createElement('div');
          newPlaceholder.className = 'studio-scene-image-placeholder';
          newPlaceholder.textContent = 'Regenerating…';
          wrap.parentNode.replaceChild(newPlaceholder, wrap);
        }
        var payload = StudioSteps.collectSceneImagePayload(
          idx, prompt,
          action === 'fix' ? (correction || undefined) : undefined,
          isLast,
          action === 'fix' && currentUrl ? currentUrl : undefined
        );
        if (!window._sceneImageErrors || window._sceneImageErrors.length !== (currentSceneImages || []).length) {
          window._sceneImageErrors = new Array((currentSceneImages || []).length);
        }
        StudioAPI.generateSceneImage(payload)
          .then(function (data) {
            if (data.image_url) {
              var url = data.image_url;
              var sep = url.indexOf('?') >= 0 ? '&' : '?';
              var disp = url + sep + '_=' + Date.now();
              currentSceneImages[idx] = disp;
              studioPinSceneImageSlot(idx, disp, url);
              if (window._sceneImageErrors) window._sceneImageErrors[idx] = null;
            } else {
              studioUnpinSceneImageSlot(idx);
              currentSceneImages[idx] = false;
              if (window._sceneImageErrors) {
                window._sceneImageErrors[idx] = 'No image_url in API response';
              }
            }
            if (typeof StudioMedia !== 'undefined') {
              StudioMedia.renderSceneImages(currentSceneImages, window._sceneImageErrors);
            }
            _lastSceneImagesRendered = currentSceneImages.slice();
            var patchJobId = phase3JobId || _phase3AnimateJobId;
            if (!patchJobId) return Promise.resolve();
            // Merge with latest pipeline-saved scene_images instead of replacing the whole array.
            // Replacing used to wipe out slots the pipeline had filled (we patched [null, url, null, ...])
            // → studio displayed mostly-empty even though the pipeline had real images for every scene.
            var pipelineSceneImages = (collectedIntermediates && collectedIntermediates.scene_images) || [];
            var slotCount = Math.max(currentSceneImages.length, pipelineSceneImages.length);
            var urlsForPatch = new Array(slotCount);
            for (var sIdx = 0; sIdx < slotCount; sIdx++) {
              var localU = currentSceneImages[sIdx];
              if (localU && typeof localU === 'string' && localU.length > 5) {
                urlsForPatch[sIdx] = stripStudioSceneImageCacheBuster(localU);
              } else {
                var pipelineU = pipelineSceneImages[sIdx];
                urlsForPatch[sIdx] =
                  (pipelineU && typeof pipelineU === 'string' && pipelineU.length > 5) ? pipelineU : null;
              }
            }
            return StudioAPI.patchIntermediates(
              patchJobId,
              { intermediates: { scene_images: urlsForPatch } },
              { skipWaitingOverlay: true }
            ).catch(
              function (pe) {
                console.warn('Studio: patch scene_images after regenerate/fix failed', pe);
              }
            );
          })
          .catch(function (err) {
            studioUnpinSceneImageSlot(idx);
            currentSceneImages[idx] = false;
            if (window._sceneImageErrors) {
              window._sceneImageErrors[idx] = (err && err.message) ? String(err.message) : String(err);
            }
            if (typeof StudioMedia !== 'undefined') {
              StudioMedia.renderSceneImages(currentSceneImages, window._sceneImageErrors);
            }
            _lastSceneImagesRendered = currentSceneImages.slice();
            alert((action === 'fix' ? 'Fix' : 'Regenerate') + ' failed: ' + (err.message || err));
          })
          .finally(function () {
            for (var b = 0; b < allBtns.length; b++) allBtns[b].disabled = false;
          });
      });
    }

    // Step 12 (Final video): Animate all — show N slots immediately, patch scene_images, resume; each scene video appears when ready
    var btnAnimateAll = document.getElementById('btnAnimateAll');
    if (btnAnimateAll) {
      btnAnimateAll.addEventListener('click', function () {
        _animateAllClickedAt = Date.now();
        var cands = phase3JobCandidatesForAnimate();

        // No images at all → block with a clear message
        if (!currentSceneImages.length) {
          alert(
            isUgcRealFlow()
              ? 'Cell images are not ready yet. Wait for the 3×3 grid on the VO & grid step, or use Regenerate on each cell.'
              : 'Generate scene images first (open the Scene images step).'
          );
          return;
        }

        // No valid Phase 3 job but images ARE ready → auto-reconnect path
        if (!cands.length) {
          var reconnectSeedJob =
            isUgcRealFlow() ? ugcRealPrimaryPipelineJobId() : phase2JobId && String(phase2JobId).trim();
          if (!reconnectSeedJob || String(reconnectSeedJob).length < 8) {
            alert(
              'Your scene images are ready but there is no active job and no Phase 2 job on this server. ' +
                'Go back to Preferences and run Generate VO to create a new session, then Generate scene prompts from Scene assets and generate images before animating.'
            );
            return;
          }
          // ── Auto-reconnect: create a new Phase 3 job, pre-seed scene_prompts,
          //    wait for it to pause (the monolith will checkpoint past prompts if they
          //    are already in intermediates), then animate with existing images. ──
          btnAnimateAll.disabled = true;
          var n = currentSceneImages.length;
          goToStep(studioFinalStep());
          var statusEl = document.getElementById('finalStatus');
          var placeholderEl = document.getElementById('finalVideoPlaceholder');
          var placeholderText = document.getElementById('finalVideoPlaceholderText');
          var resultEl = document.getElementById('finalVideoResult');
          var progressEl = document.getElementById('animationProgressLine');
          if (placeholderEl) placeholderEl.style.display = 'block';
          if (resultEl) resultEl.style.display = 'none';
          var emptySlots = new Array(n);
          if (typeof StudioMedia !== 'undefined') StudioMedia.renderSceneVideos(emptySlots);
          _studioAccumulatedSceneVideos = null;
          _lastSceneVideosRendered = [];
          for (var zi = 0; zi < n; zi++) _lastSceneVideosRendered.push(undefined);
          if (progressEl) { progressEl.textContent = '0 / ' + n + ' scene videos ready'; progressEl.style.display = 'block'; }
          var btnGenFinalR = document.getElementById('btnGenerateFinal');
          if (btnGenFinalR) btnGenFinalR.style.display = 'none';
          if (statusEl) statusEl.textContent = 'No active job found — creating new animation job from your saved images…';

          var reconPayload = StudioSteps.collectPhase3PauseScenePromptsPayload(reconnectSeedJob);

          function waitForPauseRecon(jobId, attemptsLeft) {
            if (attemptsLeft <= 0) return Promise.reject(new Error('Timed out waiting for job to initialize.'));
            return new Promise(function (r) { setTimeout(r, 2500); })
              .then(function () { return StudioAPI.getJob(jobId); })
              .then(function (job) {
                if (job.status === 'paused' || job.status === 'failed') return job;
                return waitForPauseRecon(jobId, attemptsLeft - 1);
              });
          }

          // Define animateWithJobId inline for the reconnect path (same logic as below)
          function animateRecon(jid) {
            var urlsR = currentSceneImages.map(function (u) {
              if (u && typeof u === 'string' && u.length > 5) return stripStudioSceneImageCacheBuster(u);
              return null;
            });
            return StudioAPI.patchIntermediates(jid, { intermediates: { scene_images: urlsR } }).then(function () {
              _phase3AnimateJobId = jid;
              phase3JobId = jid;
              currentJobId = jid;
              if (statusEl) statusEl.textContent = 'Starting animations…';
              return StudioAPI.getJob(jid);
            }).then(function (job) {
              if (job.status === 'paused') {
                return StudioAPI.resumeJob(jid, { stop_after_scene_animations: true });
              }
              if (job.status === 'failed') {
                return StudioAPI.retrySceneAnimations(jid);
              }
              return StudioAPI.retrySceneAnimations(jid);
            });
          }

          uploadPendingZones()
            .then(function () { return StudioAPI.generate(reconPayload); })
            .then(function (res) {
              var newJobId = res.job_id;
              phase3JobId = newJobId;
              _phase3AnimateJobId = newJobId;
              currentJobId = newJobId;
              if (statusEl) statusEl.textContent = 'Initializing job…';
              // Pre-seed existing scene_prompts so the monolith checkpoints past that step
              if (window._scenePromptsForImages && window._scenePromptsForImages.length) {
                return StudioAPI.patchIntermediates(newJobId, {
                  intermediates: { scene_prompts: window._scenePromptsForImages }
                }).then(function () { return newJobId; });
              }
              return newJobId;
            })
            .then(function (newJobId) {
              if (statusEl) statusEl.textContent = 'Waiting for job setup…';
              // Start the regular poll interval so the UI stays in sync
              pollJob();
              if (pollIntervalId) clearInterval(pollIntervalId);
              pollIntervalId = setInterval(pollJob, 2000);
              return waitForPauseRecon(newJobId, 30); // ~75 s max
            })
            .then(function () {
              return animateRecon(phase3JobId);
            })
            .then(function (resumeRes) {
              if (!resumeRes) {
                btnAnimateAll.disabled = false;
                return;
              }
              if (statusEl) statusEl.textContent = 'Animating scenes — clips will appear below…';
              if (pollIntervalId) clearInterval(pollIntervalId);
              pollIntervalId = setInterval(pollJob, 2000);
            })
            .catch(function (err) {
              btnAnimateAll.disabled = false;
              if (statusEl) statusEl.textContent = '';
              alert('Could not start animation: ' + (err.message || err));
            });
          return; // do not fall through to the normal path
        }

        // ── Normal path: Phase 3 job exists ──
        var n = currentSceneImages.length;
        btnAnimateAll.disabled = true;
        goToStep(studioFinalStep());
        var statusEl = document.getElementById('finalStatus');
        var placeholderEl = document.getElementById('finalVideoPlaceholder');
        var placeholderText = document.getElementById('finalVideoPlaceholderText');
        var resultEl = document.getElementById('finalVideoResult');
        var progressEl = document.getElementById('animationProgressLine');
        if (placeholderEl) placeholderEl.style.display = 'block';
        if (resultEl) resultEl.style.display = 'none';
        var emptySlots = new Array(n);
        if (typeof StudioMedia !== 'undefined') StudioMedia.renderSceneVideos(emptySlots);
        _studioAccumulatedSceneVideos = null;
        _lastSceneVideosRendered = [];
        for (var zi = 0; zi < n; zi++) _lastSceneVideosRendered.push(undefined);
        if (statusEl) statusEl.textContent = 'Saving images and checking job…';
        if (progressEl) progressEl.textContent = '0 / ' + n + ' scene videos ready';
        if (progressEl) progressEl.style.display = 'block';
        var btnGenFinal = document.getElementById('btnGenerateFinal');
        if (btnGenFinal) btnGenFinal.style.display = 'none';

        function animateWithJobId(jid) {
          var urlsForAnimate = currentSceneImages.map(function (u) {
            if (u && typeof u === 'string' && u.length > 5) return stripStudioSceneImageCacheBuster(u);
            return null;
          });
          return StudioAPI.patchIntermediates(jid, { intermediates: { scene_images: urlsForAnimate } }).then(function () {
            _phase3AnimateJobId = jid;
            phase3JobId = jid;
            currentJobId = jid;
            if (statusEl) statusEl.textContent = 'Checking job status…';
            return StudioAPI.getJob(jid);
          }).then(function (job) {
            if (job.status === 'completed') {
              btnAnimateAll.disabled = false;
              if (statusEl) statusEl.textContent = '';
              if (progressEl) progressEl.style.display = 'none';
              alert(
                'This job already finished. Do not use Animate all again — open the final video above, or use New video in the gallery to start again.'
              );
              return { _studioAnimateNotPaused: true };
            }
            var imj0 = job.intermediates || {};
            var oj0 = job.output || {};
            if (job.status === 'processing') {
              if (_studioFinalAssemblyStarted || imj0.concat_url || oj0.final_mp4_url) {
                btnAnimateAll.disabled = false;
                if (statusEl) statusEl.textContent = '';
                if (progressEl) progressEl.style.display = 'none';
                alert(
                  'Final video is already being assembled (or done). Stay on this step and wait — do not go back to Animate all.'
                );
                return { _studioAnimateNotPaused: true };
              }
              var sv0 = studioEffectiveSceneVideos(imj0, oj0);
              var anyVid0 = false;
              for (var vi0 = 0; vi0 < sv0.length; vi0++) {
                if (sv0[vi0] && String(sv0[vi0]).length > 5) anyVid0 = true;
              }
              if (anyVid0) {
                btnAnimateAll.disabled = false;
                if (statusEl) statusEl.textContent = '';
                if (progressEl) progressEl.style.display = 'none';
                alert(
                  'Animations are still running or partially done (some clips exist). Wait, or use “Retry all scene animations” on the Final video step to clear all clips and start over.'
                );
                return { _studioAnimateNotPaused: true };
              }
              if (statusEl) statusEl.textContent = 'No clips yet — pausing run (if needed) and restarting animations…';
              return StudioAPI.retrySceneAnimations(jid);
            }
            if (job.status === 'failed') {
              var imFail = job.intermediates || {};
              var svFail = imFail.scene_videos || [];
              var httpClips = 0;
              for (var hf = 0; hf < svFail.length; hf++) {
                var uu = svFail[hf];
                if (uu && typeof uu === 'string' && uu.trim().toLowerCase().indexOf('http') === 0) httpClips++;
              }
              if (httpClips >= n && n > 0) {
                if (statusEl) {
                  statusEl.textContent =
                    'Job failed after clips were saved — retrying final assembly only (your scene videos are kept).';
                }
                return StudioAPI.retryFailedJob(jid);
              }
              if (statusEl) statusEl.textContent = 'Job failed — clearing clips and retrying animations…';
              return StudioAPI.retrySceneAnimations(jid);
            }
            if (job.status !== 'paused') {
              btnAnimateAll.disabled = false;
              if (statusEl) statusEl.textContent = '';
              if (progressEl) progressEl.style.display = 'none';
              alert(
                'Job status: ' +
                  (job.status || 'unknown') +
                  '. For a fresh phase-3 job: Scene assets → Generate scene prompts → wait Paused → Generate images → Animate all. If the job failed during animation, click Animate all again or use Retry all scene animations on the Final video step.'
              );
              return { _studioAnimateNotPaused: true };
            }
            if (statusEl) statusEl.textContent = 'Resuming animation…';
            return StudioAPI.resumeJob(jid, { stop_after_scene_animations: true });
          });
        }

        function tryAnimateCandidate(index) {
          if (index >= cands.length) {
            return Promise.reject(new Error('No reachable Phase-3 job (404 on all ids). Step 9 → Generate scene prompts again.'));
          }
          var jid = cands[index];
          if (statusEl && index > 0) statusEl.textContent = 'Trying job ' + (index + 1) + '/' + cands.length + '…';
          return animateWithJobId(jid).catch(function (err) {
            var m = String((err && err.message) || err || '');
            if (m.indexOf('404') !== -1) return tryAnimateCandidate(index + 1);
            throw err;
          });
        }

        tryAnimateCandidate(0).then(function (res) {
          if (res && res._studioAnimateNotPaused) return;
          if (!res) {
            btnAnimateAll.disabled = false;
            if (statusEl) statusEl.textContent = 'Nothing to continue. You can try Animate all again.';
            return;
          }
          var activeId = phase3JobForAnimate() || cands[0];
          currentJobId = activeId;
          if (res && res._studioRetryKind === 'final_assembly') {
            if (statusEl) {
              statusEl.textContent =
                'Retrying concat / audio / subtitles from saved checkpoints — keep this tab open.';
            }
          } else if (statusEl) {
            statusEl.textContent =
              'Job resumed. Your ' + n + ' images are being animated — each scene video will appear below as it completes.';
          }
          if (placeholderText) placeholderText.textContent = 'Scene videos will appear in the grid above as each animation finishes. First clips can take 10–25 min.';
          if (pollIntervalId) clearInterval(pollIntervalId);
          pollIntervalId = setInterval(function () {
            pollJob();
            var pollJid = phase3JobForAnimate() || activeId;
            StudioAPI.getJob(pollJid).then(function (job) {
              if (job.status === 'completed') {
                if (pollIntervalId) clearInterval(pollIntervalId);
                pollIntervalId = null;
                var im = job.intermediates || {};
                var out = job.output || {};
                applyIntermediates(im, out, job);
                if (placeholderEl) placeholderEl.style.display = 'none';
                if (resultEl) resultEl.style.display = 'block';
                if (statusEl) statusEl.textContent = 'Video ready.';
                _studioFinalAssemblyStarted = false;
                btnAnimateAll.disabled = false;
                var btnGfDone = document.getElementById('btnGenerateFinal');
                if (btnGfDone) {
                  btnGfDone.disabled = false;
                  btnGfDone.style.display = 'none';
                  btnGfDone.textContent = 'Approve animations & assemble final video';
                }
              } else if (job.status === 'failed') {
                if (pollIntervalId) clearInterval(pollIntervalId);
                pollIntervalId = null;
                if (statusEl) statusEl.textContent = 'Failed: ' + (job.error || 'unknown');
                btnAnimateAll.disabled = false;
              } else if (job.status === 'paused' && studioAnimationsPausedForReview(job)) {
                if (pollIntervalId) clearInterval(pollIntervalId);
                pollIntervalId = setInterval(pollJob, 2000);
                pollJob();
                btnAnimateAll.disabled = false;
                var btnGf2 = document.getElementById('btnGenerateFinal');
                if (btnGf2) {
                  btnGf2.style.display = 'inline-block';
                  btnGf2.textContent = 'Approve animations & assemble final video';
                }
              }
            });
          }, 2000);
        }).catch(function (err) {
          btnAnimateAll.disabled = false;
          if (statusEl) statusEl.textContent = '';
          var em = (err && err.message) ? String(err.message) : String(err);
          if (em.indexOf('404') !== -1) {
            alert(
              'Job not found (404). If you used "Try Animate all again" while the final video was assembling, that can desync the page.\n\n' +
                'Fix: Step 9 → Generate scene prompts → wait Paused → Generate images → Animate all. Same API token. Save session after Generate images.'
            );
          } else {
            alert('Animate failed: ' + em);
          }
        });
      });

      var btnTryAnimateAgain = document.getElementById('btnTryAnimateAgain');
      if (btnTryAnimateAgain) {
        btnTryAnimateAgain.addEventListener('click', function () {
          if (
            !confirm(
              'Use this ONLY if Animate all failed before you saw any scene videos.\n\n' +
                'If you already see videos or "Assembling final video", click Cancel — going back will break the job id. Wait on this page instead.'
            )
          ) {
            return;
          }
          var animateBtn = document.getElementById('btnAnimateAll');
          if (animateBtn) {
            animateBtn.disabled = false;
            var st = document.getElementById('finalStatus');
            if (st) st.textContent = 'Animate all re-enabled. Go to Scene images only if the first run never started.';
          }
        });
      }
    }

    var btnRetryAllAnimations = document.getElementById('btnRetryAllAnimations');
    if (btnRetryAllAnimations) {
      btnRetryAllAnimations.addEventListener('click', function () {
        var jid = phase3JobForAnimate() || phase3JobId;
        if (!jid) {
          alert('No job for this stage. Complete Scene assets → Generate scene prompts, then generate images first.');
          return;
        }
        var nSc = Math.max((currentSceneImages && currentSceneImages.length) || 0, 1);
        if (!confirm('Clear every scene clip and run animations again from your current scene images?')) return;
        _animateAllClickedAt = Date.now();
        btnRetryAllAnimations.disabled = true;
        var statusElR = document.getElementById('finalStatus');
        var placeholderElR = document.getElementById('finalVideoPlaceholder');
        var placeholderTextR = document.getElementById('finalVideoPlaceholderText');
        var resultElR = document.getElementById('finalVideoResult');
        var progressElR = document.getElementById('animationProgressLine');
        if (statusElR) statusElR.textContent = 'Retrying animations…';
        StudioAPI.retrySceneAnimations(jid)
          .then(function () {
            currentJobId = jid;
            _phase3AnimateJobId = jid;
            phase3JobId = jid;
            goToStep(studioFinalStep());
            if (placeholderElR) placeholderElR.style.display = 'block';
            if (resultElR) resultElR.style.display = 'none';
            if (progressElR) {
              progressElR.style.display = 'block';
              progressElR.textContent = '0 / ' + nSc + ' scene videos ready';
            }
            var emptyR = new Array(nSc);
            if (typeof StudioMedia !== 'undefined') StudioMedia.renderSceneVideos(emptyR);
            _studioAccumulatedSceneVideos = null;
            _lastSceneVideosRendered = [];
            for (var ir = 0; ir < nSc; ir++) _lastSceneVideosRendered.push(undefined);
            if (statusElR) statusElR.textContent = 'Animations restarted — each clip appears when ready.';
            if (placeholderTextR) {
              placeholderTextR.textContent =
                'First clips can take 10–25 min. This page updates every 2 seconds.';
            }
            if (pollIntervalId) clearInterval(pollIntervalId);
            pollIntervalId = setInterval(function () {
              pollJob();
              var pollJidR = phase3JobForAnimate() || jid;
              StudioAPI.getJob(pollJidR).then(function (job) {
                if (job.status === 'completed') {
                  if (pollIntervalId) clearInterval(pollIntervalId);
                  pollIntervalId = null;
                  var imr = job.intermediates || {};
                  var outr = job.output || {};
                  applyIntermediates(imr, outr, job);
                  if (placeholderElR) placeholderElR.style.display = 'none';
                  if (resultElR) resultElR.style.display = 'block';
                  if (statusElR) statusElR.textContent = 'Video ready.';
                  _studioFinalAssemblyStarted = false;
                  var btnGfR = document.getElementById('btnGenerateFinal');
                  if (btnGfR) {
                    btnGfR.disabled = false;
                    btnGfR.style.display = 'none';
                  }
                } else if (job.status === 'failed') {
                  if (pollIntervalId) clearInterval(pollIntervalId);
                  pollIntervalId = null;
                  if (statusElR) statusElR.textContent = 'Failed: ' + (job.error || 'unknown');
                } else if (job.status === 'paused' && studioAnimationsPausedForReview(job)) {
                  if (pollIntervalId) clearInterval(pollIntervalId);
                  pollIntervalId = setInterval(pollJob, 2000);
                  pollJob();
                  var btnGfR2 = document.getElementById('btnGenerateFinal');
                  if (btnGfR2) {
                    btnGfR2.style.display = 'inline-block';
                    btnGfR2.textContent = 'Approve animations & assemble final video';
                  }
                }
              });
            }, 2000);
          })
          .catch(function (e) {
            var em = (e && e.message) ? String(e.message) : String(e);
            if (isStudioJobNotFoundError(e)) {
              _studioLinkedJobsMissing = true;
              if (jid) clearInvalidStudioJobId(jid);
              try {
                saveSession();
              } catch (eSave3) {}
              updateStudioJobLinkBanner();
              alert(
                isUgcRealFlow()
                  ? 'Retry animations: job not found (404). The saved job id is not on this server. Your session still has grid data. Fix: ensure the correct API key/URL, or start a new generation from step 5, then use Animate all again.'
                  : 'Retry animations: job not found (404). The saved job id is not on this server (wrong API key, URL, account, or the job was deleted). Your session still has prompts and images. Fix: open Scene prompts (step ' +
                      studioPromptsStep() +
                      ') → Generate scene prompts to create a new job, then generate images and Animate all again.'
              );
            } else {
              alert(em);
            }
          })
          .finally(function () {
            btnRetryAllAnimations.disabled = false;
          });
      });
    }

    var btnStep7Continue = document.getElementById('btnStep7ApproveContinue');
    if (btnStep7Continue) {
      btnStep7Continue.addEventListener('click', function () {
        if (!StudioSteps.validateStep(studioVoStep())) {
          alert('Please wait for a voiceover script in the box above (or paste at least a short script).');
          return;
        }
        var _vt = StudioSteps.getVideoType ? StudioSteps.getVideoType() : '';
        if (_vt !== 'product video' && !_voGeneratedForApprove) {
          alert('Generate the VO audio first — click "Generate VO" above, then Approve and continue.');
          return;
        }
        var vt = StudioSteps.getVideoType ? StudioSteps.getVideoType() : '';
        if (vt !== 'product video' || !phase2JobId) {
          goToStep(studioMusicStep());
          return;
        }
        var voEl = document.getElementById('voScript');
        var voText = voEl ? String(voEl.value || '').trim() : '';
        btnStep7Continue.disabled = true;
        StudioAPI.getJob(phase2JobId)
          .then(function (job) {
            var st = job && job.status;
            if (st === 'processing') {
              var im = (job && job.intermediates) || {};
              var hasScript =
                voText.length >= 15 ||
                (im.vo_script && String(im.vo_script).trim().length >= 15);
              if (!hasScript) {
                alert('Still generating the voiceover script. Wait until text appears above, then try again.');
                return { advance: false };
              }
              alert(
                'The job is still running (for example reference-video analysis). Wait until this step finishes, then click Approve and continue again.'
              );
              return { advance: false };
            }
            if (st === 'failed') {
              alert(
                'This job failed. Go back to Preferences (step 6) or Review character (step 7) and run Generate VO again, or use New video in the gallery to start again.'
              );
              return { advance: false };
            }
            if (st === 'paused') {
              return StudioAPI.patchIntermediates(phase2JobId, { intermediates: { vo_script: voText } })
                .then(function () {
                  return StudioAPI.resumeJob(phase2JobId, { stop_after_scene_animations: true });
                })
                .then(function () {
                  return { advance: true };
                });
            }
            if (st === 'completed') {
              return { advance: true };
            }
            return { advance: false };
          })
          .then(function (out) {
            if (!out || !out.advance) return;
            goToStep(studioMusicStep());
            var statusEl = document.getElementById('finalStatus');
            if (statusEl) {
              statusEl.textContent =
                'Pipeline continued — next planned stop for product video is after scene prompts (Scene assets step). Then images → animate → final.';
            }
          })
          .catch(function (err) {
            alert('Could not continue: ' + (err.message || err));
          })
          .finally(function () {
            btnStep7Continue.disabled = false;
          });
      });
    }

    /* Wire up the Redesign voice button */
    var btnRedesignVoice = document.getElementById('btnRedesignVoice');
    if (btnRedesignVoice) {
      btnRedesignVoice.addEventListener('click', function () {
        _voiceDesignCache = null;
        _voiceDesignCacheKey = '';
        _designedVoiceId = null;
        _persistEffectiveVoiceId(''); /* clear previously saved voice so user must pick from new cards */
        triggerVoiceDesign(true);
      });
    }

    var btnGenerateVOInStep7 = document.getElementById('btnGenerateVOInStep7');
    if (btnGenerateVOInStep7) {
      btnGenerateVOInStep7.addEventListener('click', function () {
        var voScript = (document.getElementById('voScript') || {}).value || '';
        if (!voScript.trim()) {
          alert('Enter or wait for the VO script first.');
          return;
        }
        var manualInput = document.getElementById('voVoiceIdManual');
        var sel = document.getElementById('voVoiceSelect');
        var savedEffectiveId = ((document.getElementById('voEffectiveVoiceId') || {}).value || '').trim();
        /* Priority: manual paste > saved designed voice (real voice_id) > dropdown list
           NOTE: _designedVoiceId is the generated_voice_id (temporary preview ID) which cannot be
           used for TTS. Only savedEffectiveId (written after /api/voice-save) is a real voice_id. */
        var voiceId = (manualInput && manualInput.value) ? manualInput.value.trim() : '';
        if (!voiceId) voiceId = savedEffectiveId;
        if (!voiceId) voiceId = (sel && sel.value) ? sel.value.trim() : '';
        if (!voiceId) {
          alert('Click "Use this" on a designed voice to save it, choose from the list below, or paste an ElevenLabs voice ID.');
          return;
        }
        var lang = StudioSteps.getLanguage ? StudioSteps.getLanguage() : 'en';
        var videoType = StudioSteps.getVideoType ? StudioSteps.getVideoType() : 'influencer';
        btnGenerateVOInStep7.disabled = true;
        btnGenerateVOInStep7.textContent = 'Generating…';
        StudioAPI.generateVo({
          vo_script: voScript,
          language: lang,
          voice_id: voiceId,
          video_type: videoType,
          job_id: phase2JobId || undefined
        }).then(function (data) {
          if (data.vo_audio_url) {
            _voGeneratedForApprove = true;
            var voAudio = document.getElementById('voAudio');
            var voWrap = document.getElementById('voAudioPlayerWrap');
            if (voAudio) { voAudio.src = data.vo_audio_url; voAudio.load(); }
            if (voWrap) voWrap.style.display = 'block';
            if (data.vo_duration != null) {
              var durEl = document.getElementById('voDuration');
              if (durEl) durEl.textContent = Math.round(data.vo_duration);
            }
            var toPatch = { vo_audio_url: data.vo_audio_url };
            if (data.vo_word_segments) toPatch.vo_word_segments = data.vo_word_segments;
            if (data.vo_duration != null) toPatch.vo_duration = data.vo_duration;
            if (phase2JobId) {
              return StudioAPI.patchIntermediates(phase2JobId, { intermediates: toPatch }).then(function () {
                if (pollIntervalId) pollJob();
              });
            }
          }
        }).catch(function (err) {
          alert('Generate VO failed: ' + (err.message || err));
        }).finally(function () {
          btnGenerateVOInStep7.disabled = false;
          btnGenerateVOInStep7.textContent = 'Generate VO';
        });
      });
    }

    document.getElementById('btnGenerateFinal').addEventListener('click', function () {
      var btn = document.getElementById('btnGenerateFinal');
      var p3f = isUgcRealFlow() ? ugcRealPrimaryPipelineJobId() : phase3JobForAnimate();
      if (!p3f) {
        alert(isUgcRealFlow() ? 'No UGC Real review job found.' : 'No phase-3 job. Use Animate all from Scene images first.');
        return;
      }
      var fromGrid = collectSceneVideoUrlsFromGrid();
      var fromJob = (collectedIntermediates.scene_videos || []).slice();
      var urls = fromGrid.some(function (u) { return u; }) ? fromGrid : fromJob;
      if (!urls.length || urls.some(function (u) { return !u || (typeof u === 'string' && u.length < 10); })) {
        alert('Every scene needs a finished animation URL. Wait for all clips or fix empty slots.');
        return;
      }
      btn.disabled = true;
      var st = document.getElementById('finalStatus');
      var ph = document.getElementById('finalVideoPlaceholderText');
      if (st) st.textContent = 'Saving clip URLs and starting final assembly (Rendi)…';
      if (ph) ph.textContent = 'Concatenating scenes, mixing VO and music. This may take several minutes.';
      StudioAPI.patchIntermediates(p3f, { intermediates: { scene_videos: urls } })
        .then(function () {
          _phase3AnimateJobId = p3f;
          phase3JobId = p3f;
          return StudioAPI.resumeJob(p3f, { stop_after_scene_animations: false });
        })
        .then(function () {
          _studioFinalAssemblyStarted = true;
          currentJobId = p3f;
          if (pollIntervalId) clearInterval(pollIntervalId);
          pollIntervalId = setInterval(pollJob, 2000);
          pollJob();
        })
        .catch(function (err) {
          var msg = (err && err.message) || String(err);
          if (/409/.test(msg) && /already running/i.test(msg)) {
            _studioFinalAssemblyStarted = true;
            currentJobId = p3f;
            if (pollIntervalId) clearInterval(pollIntervalId);
            pollIntervalId = setInterval(pollJob, 2000);
            pollJob();
            if (st) st.textContent = 'Job already running — tracking progress…';
            return;
          }
          if (st) st.textContent = '';
          alert('Could not start final assembly: ' + msg);
        })
        .finally(function () {
          btn.disabled = false;
        });
    });
    // Step 13: Add subtitles — use template/position from this step; sync to step 5 (models) for next run; no separate API
    var btnAddSubtitles = document.getElementById('btnAddSubtitles');
    if (btnAddSubtitles) {
      btnAddSubtitles.addEventListener('click', function () {
        var templateEl = document.getElementById('subtitleTemplateStep12');
        var positionEl = document.getElementById('subtitlePositionStep12');
        var template = templateEl && templateEl.value ? templateEl.value : '';
        var position = positionEl && positionEl.value ? positionEl.value : 'middle';
        var step5Template = document.getElementById('subtitleTemplate');
        var step5Position = document.getElementById('subtitlePosition');
        if (step5Template && template) step5Template.value = template;
        if (step5Position && position) step5Position.value = position;
        document.getElementById('addSubtitles').checked = true;
        var wrap = document.getElementById('subtitledVideoWrap');
        var player = document.getElementById('subtitledVideoPlayer');
        var download = document.getElementById('subtitledVideoDownload');
        var finalUrl = document.getElementById('finalVideoPlayer') && document.getElementById('finalVideoPlayer').src;
        if (finalUrl && finalUrl.indexOf('http') === 0) {
          wrap.style.display = 'block';
          player.src = finalUrl;
          download.setAttribute('data-download-url', finalUrl);
          download.setAttribute('data-download-name', 'video_with_subtitles.mp4');
        }
        alert('Subtitle options saved (template: ' + (template || 'default') + ', position: ' + position + '). They will be applied on your next generation. Subtitles are baked in during pipeline run when "Add subtitles" is checked in step 5.');
      });
    }

    wireStudioDownloadButtons();

    document.getElementById('modeStep').addEventListener('click', function () {
      document.getElementById('modeAuto').classList.remove('active');
      this.classList.add('active');
    });
    document.getElementById('modeAuto').addEventListener('click', function () {
      document.getElementById('modeStep').classList.remove('active');
      this.classList.add('active');
    });

    document.getElementById('prompt').addEventListener('input', function () {
      document.getElementById('promptCount').textContent = (this.value || '').length;
    });

    StudioSteps.updateVisibilityForVideoType();

    var saved = loadSession();
    // loadSession() only returns data when at least one phase job id exists — that is what we auto-restore after refresh.
    var hasRestorableSession = !!saved;
    var restoreBar = document.getElementById('sessionRestoreBar');
    var restoreText = document.getElementById('sessionRestoreText');
    var btnRestore = document.getElementById('btnRestoreSession');
    var btnDismiss = document.getElementById('btnDismissSession');
    if (btnRestore) {
      btnRestore.addEventListener('click', function () {
        var s = loadSession();
        if (s && restoreBar) {
          restoreBar.style.display = 'none';
          applyRestoredSession(s);
        }
      });
    }
    if (btnDismiss) {
      btnDismiss.addEventListener('click', function () {
        clearSessionStorage();
        if (restoreBar) restoreBar.style.display = 'none';
      });
    }
    var btnSaveSession = document.getElementById('btnSaveSession');
    var btnClearSession = document.getElementById('btnClearSession');
    var btnClearAllSessions = document.getElementById('btnClearAllSessions');
    if (btnSaveSession) {
      btnSaveSession.addEventListener('click', function () {
        var stepNow = migrateStoredWizardStep(currentStep, { wizardStepSchema: WIZARD_STEP_SCHEMA });
        if (stepNow < MIN_WIZARD_STEP_FOR_SESSION) {
          if (restoreText) {
            restoreText.textContent =
              'Reach step 2 (Style & duration) or beyond before saving a session to the list.';
          }
          if (restoreBar) restoreBar.style.display = 'flex';
          return;
        }
        saveSession(true);
        if (restoreText) restoreText.textContent = 'Session saved. It\'s in Previous sessions — you can restore it anytime.';
        if (restoreBar) restoreBar.style.display = 'flex';
      });
    }
    if (btnClearSession) {
      btnClearSession.addEventListener('click', function () {
        if (!confirm('Clear current session? Any in-progress generation will be lost.')) return;
        clearSessionStorage();
        window._studioServerSessionId = null;
        if (restoreBar) restoreBar.style.display = 'none';
        currentStep = 1;
        currentJobId = phase1JobId = phase2JobId = phase3JobId = null;
        _phase3AnimateJobId = null;
        _phase1LastPolledStatus = null;
        _scenePromptsJobStarted = false;
        collectedIntermediates = {};
        try {
          studioResetUgcRealClientCaches();
        } catch (eUgcCl) {}
        _characterReviewPendingUrl = null;
        try {
          hideCharPreview();
        } catch (eCh2) {}
        _phase1AutoStarted = false;
        _studioLastJobOutput = {};
        _studioLinkedJobsMissing = false;
        updateStudioJobLinkBanner();
        studioClearAllSceneImagePins();
        currentSceneImages = [];
        if (window._scenePromptsForImages) window._scenePromptsForImages = [];
        if (sseSource) { sseSource.close(); _sseActive = false; }
        if (pollIntervalId) clearInterval(pollIntervalId);
        pollIntervalId = null;
        try {
          if (typeof StudioSteps !== 'undefined' && StudioSteps.resetWizardFormAndZones) {
            StudioSteps.resetWizardFormAndZones();
          }
        } catch (e) {}
        _studioFinalAssemblyStarted = false;
        _lastSceneImagesRendered = _lastSceneVideosRendered = _lastScenePromptsJson = null;
        resetStudioMonotonicMediaAccumulators();
        goToStep(1);
      });
    }
    if (btnClearAllSessions) {
      btnClearAllSessions.addEventListener('click', function () {
        if (!confirm('Clear all saved sessions? You will not be able to restore any previous session.')) return;
        clearAllSessions();
        // Also delete cloud sessions from Supabase
        if (typeof StudioAuth !== 'undefined' && StudioAuth.isAuthEnabled()) {
          if (typeof StudioWaitingOverlay !== 'undefined' && StudioWaitingOverlay.push) {
            StudioWaitingOverlay.push('clearing-sessions');
          }
          StudioAuth.deleteAllSessionsFromServer()
            .catch(function (e) { console.warn('deleteAllSessionsFromServer:', e); })
            .finally(function () {
              if (typeof StudioWaitingOverlay !== 'undefined' && StudioWaitingOverlay.pop) {
                StudioWaitingOverlay.pop();
              }
              updateSessionListUI();
            });
        }
        window._studioServerSessionId = null;
        if (restoreBar) restoreBar.style.display = 'none';
        currentStep = 1;
        currentJobId = phase1JobId = phase2JobId = phase3JobId = null;
        _phase3AnimateJobId = null;
        _phase1LastPolledStatus = null;
        _scenePromptsJobStarted = false;
        collectedIntermediates = {};
        try {
          studioResetUgcRealClientCaches();
        } catch (eUgcAll) {}
        _characterReviewPendingUrl = null;
        try {
          hideCharPreview();
        } catch (eCh3) {}
        _phase1AutoStarted = false;
        _studioLastJobOutput = {};
        _studioLinkedJobsMissing = false;
        updateStudioJobLinkBanner();
        studioClearAllSceneImagePins();
        currentSceneImages = [];
        if (window._scenePromptsForImages) window._scenePromptsForImages = [];
        if (sseSource) { sseSource.close(); _sseActive = false; }
        if (pollIntervalId) clearInterval(pollIntervalId);
        pollIntervalId = null;
        goToStep(1);
        if (typeof StudioAuth === 'undefined' || !StudioAuth.isAuthEnabled()) {
          updateSessionListUI();
        }
      });
    }
    var btnPreviousSessions = document.getElementById('btnPreviousSessions');
    var sessionListPanel = document.getElementById('sessionListPanel');
    if (btnPreviousSessions && sessionListPanel) {
      btnPreviousSessions.addEventListener('click', function (e) {
        e.stopPropagation();
        var hidden =
          sessionListPanel.style.display === 'none' || !String(sessionListPanel.style.display || '').trim();
        if (hidden) {
          attachSessionListPanelToBody();
          sessionListPanel.style.display = 'block';
          updateSessionListUI();
          schedulePositionStudioSessionListPanel();
        } else {
          closeSessionListPanel();
        }
      });
      window.addEventListener('resize', schedulePositionStudioSessionListPanel);
      window.addEventListener('scroll', schedulePositionStudioSessionListPanel, true);
      document.addEventListener('click', function () {
        closeSessionListPanel();
      });
      sessionListPanel.addEventListener('click', function (e) {
        e.stopPropagation();
        var btn = e.target.closest('button[data-session-index]');
        if (!btn) return;
        var idx = parseInt(btn.dataset.sessionIndex, 10);
        if (isNaN(idx)) return;
        if (idx < 0 || idx >= _sessionListMerged.length) return;
        var entry = _sessionListMerged[idx];
        closeSessionListPanel();
        if (restoreBar) restoreBar.style.display = 'none';
        if (entry.source === 'server' && entry.serverId) {
          window._studioServerSessionId = entry.serverId;
        }
        applyRestoredSession(entry.data);
      });
    }
    updateSessionListUI();
    window.addEventListener('beforeunload', function () { saveSession(); });

    if (hasRestorableSession) {
      // Don't silently auto-restore — that surprised users who opened the studio expecting a
      // blank slate and saw old TEXT 1-3 + uploaded assets reappear. Show the restore bar so
      // the user picks: click Restore to continue prior work, or Dismiss to start fresh.
      if (restoreBar) restoreBar.style.display = 'block';
      if (restoreText) {
        var stepHint = saved && saved.currentStep ? ' (left off at step ' + saved.currentStep + ')' : '';
        restoreText.textContent = 'Resume your previous session' + stepHint + '?';
      }
      goToStep(1);
    } else {
      goToStep(1);
    }

    var apiKeyInput = document.getElementById('studioApiKey');
    if (apiKeyInput) {
      var key = StudioAPI.getApiKey();
      var cloudCfg =
        typeof StudioAuth !== 'undefined' &&
        StudioAuth.isCloudConfigured &&
        StudioAuth.isCloudConfigured();
      if (cloudCfg && key === 'dev') {
        StudioAPI.setApiKey('');
        key = '';
      }
      if (
        !key &&
        (window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1') &&
        !cloudCfg
      ) {
        StudioAPI.setApiKey('dev');
        key = 'dev';
      }
      apiKeyInput.value = key;
      apiKeyInput.addEventListener('change', function () {
        StudioAPI.setApiKey(apiKeyInput.value.trim());
      });
      apiKeyInput.addEventListener('blur', function () {
        StudioAPI.setApiKey(apiKeyInput.value.trim());
      });
    }

    function updateStudioServerAuthHint() {
      var el = document.getElementById('studioServerAuthHint');
      if (!el) return;
      var c =
        typeof StudioAuth !== 'undefined' && StudioAuth.getLastConfig
          ? StudioAuth.getLastConfig()
          : null;
      if (!c || !c.studio_cloud_available) {
        el.style.display = 'none';
        el.textContent = '';
        return;
      }
      if (c.studio_sign_in_only_ready) {
        el.style.display = 'none';
        el.textContent = '';
        return;
      }
      el.textContent =
        c.studio_tenant_auth_hint ||
        'Cloud sign-in is on, but this API cannot resolve a tenant from JWT alone. Paste sk-tvd-… in the header or fix server .env / api_tenants.';
      el.style.display = 'block';
    }
    updateStudioServerAuthHint();

    window.__studioResetForNewVideo = function () {
      window._studioServerSessionId = null;
      try {
        clearSessionStorage();
      } catch (e) {}
      collectedIntermediates = {};
      try {
        studioResetUgcRealClientCaches();
      } catch (eUgcNv) {}
      _characterReviewPendingUrl = null;
      try {
        hideCharPreview();
      } catch (eCh) {}
      try {
        clearTimeout(_characterPrefetchDebounceTimer);
      } catch (eDeb) {}
      _characterPrefetchDebounceTimer = null;
      _characterPrefetchPromise = null;
      _characterPrefetchGen++;
      _studioLastJobOutput = {};
      _studioLinkedJobsMissing = false;
      updateStudioJobLinkBanner();
      _musicAutoStarted = false;
      _phase1AutoStarted = false;
      currentStep = 1;
      currentJobId = phase1JobId = phase2JobId = phase3JobId = null;
      _phase3AnimateJobId = null;
      _phase1LastPolledStatus = null;
      _scenePromptsJobStarted = false;
      studioClearAllSceneImagePins();
      currentSceneImages = [];
      if (window._scenePromptsForImages) window._scenePromptsForImages = [];
      if (sseSource) sseSource.close();
      if (pollIntervalId) clearInterval(pollIntervalId);
      pollIntervalId = null;
      try {
        if (typeof StudioSteps !== 'undefined' && StudioSteps.resetWizardFormAndZones) {
          StudioSteps.resetWizardFormAndZones();
        }
      } catch (e) {
        console.warn('resetWizardFormAndZones:', e);
      }
      _studioFinalAssemblyStarted = false;
      _lastSceneImagesRendered = _lastSceneVideosRendered = _lastScenePromptsJson = null;
      goToStep(1);
      if (typeof StudioAuth !== 'undefined' && StudioAuth.isAuthEnabled()) {
        StudioAuth.getUser()
          .then(function (u) {
            if (!u) return;
            var p = buildSessionPayload();
            if (!isCountableStudioSession(p)) return;
            return StudioAuth.saveSessionToServer(p, null, 'New video');
          })
          .then(function (id) {
            if (id) window._studioServerSessionId = id;
          })
          .catch(function (e) {
            console.warn('New video cloud session:', e);
          });
      }
    };
    try {
      refreshCharacterLibrarySelect();
    } catch (eRcLib) {}
  }

  window.__studioApplyRestoredSession = function (session) {
    applyRestoredSession(session);
  };

  function updateAccountStrip() {
    var strip = document.getElementById('studioAccountStrip');
    if (
      !strip ||
      typeof StudioAuth === 'undefined' ||
      !StudioAuth.isAuthEnabled() ||
      StudioAuth.isLoginGateRequired()
    ) {
      if (strip) strip.style.display = 'none';
      return;
    }
    strip.style.display = 'flex';
    StudioAuth.getUser().then(function (u) {
      var lab = document.getElementById('studioAccountStripLabel');
      var si = document.getElementById('btnAccountStripSignIn');
      var gal = document.getElementById('btnAccountStripGallery');
      var so = document.getElementById('btnAccountStripSignOut');
      if (!u) {
        if (lab) {
          lab.textContent =
            'Cloud: sign in before you run Generate — finished videos are saved to My videos only when you are signed in.';
        }
        if (si) si.style.display = 'inline-flex';
        if (gal) gal.style.display = 'none';
        if (so) so.style.display = 'none';
      } else {
        if (lab) lab.textContent = u.email ? String(u.email) : 'Signed in';
        if (si) si.style.display = 'none';
        if (gal) gal.style.display = 'inline-flex';
        if (so) so.style.display = 'inline-flex';
      }
    });
  }

  window.__studioUpdateAccountStrip = updateAccountStrip;

  function showStudioAuthBanner(text) {
    var b = document.getElementById('studioAuthBanner');
    var t = document.getElementById('studioAuthBannerText');
    if (t) t.textContent = text || '';
    if (b) b.style.display = 'flex';
  }

  function wireStudioAccountButton() {
    var btn = document.getElementById('btnStudioAccount');
    if (!btn || typeof StudioAuth === 'undefined') return;
    btn.addEventListener('click', function () {
      var overlay = document.getElementById('studioAuthOverlay');
      if (StudioAuth.isAuthEnabled()) {
        StudioAuth.getUser().then(function (u) {
          if (u && typeof StudioGallery !== 'undefined') {
            StudioGallery.show();
          } else {
            window._studioOpenGalleryAfterAuth = false;
            if (overlay) {
              overlay.style.display = 'flex';
              overlay.setAttribute('aria-hidden', 'false');
            }
          }
        });
        return;
      }
      if (StudioAuth.isCloudConfigured && StudioAuth.isCloudConfigured()) {
        showStudioAuthBanner(
          'Supabase is configured but the client did not start. Check the browser console; ensure @supabase/supabase-js loads (CDN not blocked). Then refresh.'
        );
        if (overlay) {
          overlay.style.display = 'flex';
          overlay.setAttribute('aria-hidden', 'false');
        }
        return;
      }
      var cfg = StudioAuth.getLastConfig ? StudioAuth.getLastConfig() : null;
      var base =
        typeof StudioAPI !== 'undefined' && StudioAPI.getBaseUrl
          ? StudioAPI.getBaseUrl()
          : '';
      var msg =
        cfg && cfg._fetchFailed
          ? 'Cannot load server config from ' +
            base +
            '/api/config. Use the same origin as the API (e.g. http://localhost:8000/studio/). If you changed the API base URL in another app, clear localStorage key studio_api_base or set it to this server.'
          : 'Sign-in and cloud gallery are off until the API server has Supabase env vars: SUPABASE_URL and SUPABASE_ANON_KEY or SUPABASE_PUBLISHABLE_KEY. Restart the server after editing api_pipeline/.env — then refresh this page.';
      showStudioAuthBanner(msg);
      var ban = document.getElementById('studioAuthBanner');
      if (ban) {
        try {
          ban.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        } catch (e) {}
      }
    });
  }

  async function runStudioBootstrap() {
    window._studioOpenGalleryAfterAuth = false;
    window._studioWizardOpenedFromGallery = false;
    try {
      await StudioAuth.init();
    } catch (e) {
      console.warn('StudioAuth.init', e);
    }
    if (typeof StudioGallery !== 'undefined') {
      StudioGallery.setupAuthForms();
      StudioGallery.wireGalleryChrome();
    }
    wireStudioAccountButton();
    var dismissBanner = document.getElementById('btnDismissAuthBanner');
    if (dismissBanner) {
      dismissBanner.addEventListener('click', function () {
        var b = document.getElementById('studioAuthBanner');
        if (b) b.style.display = 'none';
      });
    }
    var dismissJobBanner = document.getElementById('btnDismissJobLinkBanner');
    if (dismissJobBanner) {
      dismissJobBanner.addEventListener('click', function () {
        var jb = document.getElementById('studioJobLinkBanner');
        if (jb) jb.style.display = 'none';
      });
    }
    if (!StudioAuth.isAuthEnabled()) {
      if (StudioAuth.isCloudConfigured && StudioAuth.isCloudConfigured()) {
        showStudioAuthBanner(
          'Sign-in UI: allow jsdelivr.net (Supabase JS) or check the console. Click Account to retry opening the login form.'
        );
      } else {
        var cfg0 = StudioAuth.getLastConfig ? StudioAuth.getLastConfig() : null;
        var autoMsg =
          cfg0 && cfg0._fetchFailed
            ? 'Could not reach the API config. Open Studio from the same host as the server (e.g. http://localhost:8000/studio/).'
            : 'Cloud sign-in is not configured. Add SUPABASE_URL and SUPABASE_ANON_KEY or SUPABASE_PUBLISHABLE_KEY to api_pipeline/.env, restart the API, run migrations/001_user_auth_sessions_videos.sql in Supabase, enable Email auth — then refresh.';
        showStudioAuthBanner(autoMsg);
      }
    }
    var overlay = document.getElementById('studioAuthOverlay');
    var gview = document.getElementById('studioGalleryView');
    var wview = document.getElementById('studioWizardView');

    var btnStripIn = document.getElementById('btnAccountStripSignIn');
    var btnStripGal = document.getElementById('btnAccountStripGallery');
    var btnStripOut = document.getElementById('btnAccountStripSignOut');
    if (btnStripIn) {
      btnStripIn.addEventListener('click', function () {
        window._studioOpenGalleryAfterAuth = false;
        if (overlay) {
          overlay.style.display = 'flex';
          overlay.setAttribute('aria-hidden', 'false');
        }
      });
    }
    if (btnStripGal) {
      btnStripGal.addEventListener('click', function () {
        StudioAuth.getUser().then(function (u) {
          if (u && typeof StudioGallery !== 'undefined') {
            StudioGallery.show();
          } else {
            window._studioOpenGalleryAfterAuth = true;
            if (overlay) {
              overlay.style.display = 'flex';
              overlay.setAttribute('aria-hidden', 'false');
            }
          }
        });
      });
    }
    if (btnStripOut) {
      btnStripOut.addEventListener('click', function () {
        StudioAuth.signOut()
          .then(function () {
            window._studioWizardOpenedFromGallery = false;
            updateAccountStrip();
          })
          .catch(function (e) {
            console.warn('Sign out', e);
          });
      });
    }

    if (!StudioAuth.isAuthEnabled()) {
      if (overlay) overlay.style.display = 'none';
      if (gview) gview.style.display = 'none';
      if (wview) wview.style.display = 'block';
      updateAccountStrip();
      init();
      return;
    }

    if (!StudioAuth.isLoginGateRequired()) {
      if (overlay) {
        overlay.style.display = 'none';
        overlay.setAttribute('aria-hidden', 'true');
      }
      if (gview) gview.style.display = 'none';
      if (wview) wview.style.display = 'block';
      updateAccountStrip();
      StudioAuth.onAuthStateChange(function () {
        updateAccountStrip();
        try {
          refreshCharacterLibrarySelect();
        } catch (e) {}
      });
      init();
      return;
    }

    StudioAuth.onAuthStateChange(function (ev, session) {
      if (session && session.user) {
        // Confirmed signed-in event — show gallery.
        if (overlay) overlay.style.display = 'none';
        if (gview) gview.style.display = 'block';
        if (wview) wview.style.display = 'none';
        if (typeof StudioGallery !== 'undefined') StudioGallery.refresh();
        try {
          refreshCharacterLibrarySelect();
        } catch (e2) {}
      } else if (ev === 'SIGNED_OUT') {
        // Don't redirect immediately. Browsers throttle background tabs and
        // GoTrue may emit SIGNED_OUT when it simply couldn't auto-refresh the
        // token (tab was invisible). Give Supabase up to 3 s to recover the
        // session before we decide the user is actually signed out.
        var _pendingSignOutTimer = setTimeout(function () {
          StudioAuth.getSession().then(function (s) {
            if (s && s.user) return; // session recovered — stay put
            if (overlay) overlay.style.display = 'flex';
            if (gview) gview.style.display = 'none';
            if (wview) wview.style.display = 'none';
          }).catch(function () {
            if (overlay) overlay.style.display = 'flex';
            if (gview) gview.style.display = 'none';
            if (wview) wview.style.display = 'none';
          });
        }, 3000);
        // If a SIGNED_IN / TOKEN_REFRESHED fires within those 3 s the timer
        // will still run, but at that point getSession() will return a valid
        // session and the early return above will prevent the redirect.
        void _pendingSignOutTimer;
      }
    });
    var sess = await StudioAuth.getSession();
    if (sess && sess.user) {
      if (overlay) overlay.style.display = 'none';
      if (gview) gview.style.display = 'block';
      if (wview) wview.style.display = 'none';
    } else {
      if (overlay) overlay.style.display = 'flex';
      if (gview) gview.style.display = 'none';
      if (wview) wview.style.display = 'none';
    }
    updateAccountStrip();
    init();
    if (sess && sess.user && typeof StudioGallery !== 'undefined') {
      StudioGallery.refresh();
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () {
      runStudioBootstrap().catch(function (e) {
        console.error(e);
        init();
      });
    });
  } else {
    runStudioBootstrap().catch(function (e) {
      console.error(e);
      init();
    });
  }
})();

/**
 * Full-screen waiting state for server work. User-facing copy only — no API paths or service names.
 */
var StudioWaitingOverlay = (function () {
  var depth = 0;
  var overlay = null;
  var linePrimary = null;
  var lineSecondary = null;
  var rotateTimer = null;
  var phraseIndex = 0;

  var PHASES = {
    default: {
      title: 'Working on it',
      lines: [
        'Your request was sent successfully.',
        'Please keep this tab open while we process it.',
        'This may take a minute or two.',
      ],
    },
    connecting: {
      title: 'Connecting',
      lines: [
        'Reaching the studio…',
        'Almost ready.',
      ],
    },
    upload: {
      title: 'Uploading',
      lines: [
        'Sending your file…',
        'Still uploading — thanks for waiting.',
      ],
    },
    start_pipeline: {
      title: 'Starting',
      lines: [
        'Queuing your video…',
        'The pipeline is picking up your job.',
      ],
    },
    resume: {
      title: 'Continuing',
      lines: [
        'Resuming from where you left off…',
      ],
    },
    patch: {
      title: 'Saving',
      lines: [
        'Saving your changes…',
      ],
    },
    music: {
      title: 'Creating audio',
      lines: [
        'Generating background music…',
        'This part can take a little while.',
      ],
    },
    voices: {
      title: 'Loading options',
      lines: [
        'Fetching voice suggestions…',
      ],
    },
    vo: {
      title: 'Voice-over',
      lines: [
        'Generating spoken audio…',
        'Almost there.',
      ],
    },
    character: {
      title: 'Character',
      lines: [
        'Creating the portrait…',
      ],
    },
    scene_image: {
      title: 'Scene image',
      lines: [
        'Rendering the scene…',
        'Still working on the image.',
      ],
    },
    retry_animations: {
      title: 'Retrying',
      lines: [
        'Updating scene animations…',
        'If the video was still running, we may wait for a safe pause first.',
      ],
    },
    gallery: {
      title: 'Loading',
      lines: [
        'Loading your library…',
      ],
    },
    sync: {
      title: 'Syncing',
      lines: [
        'Updating your account data…',
        'Almost done.',
      ],
    },
  };

  function phaseConfig(key) {
    return PHASES[key] || PHASES.default;
  }

  function clearRotate() {
    if (rotateTimer) {
      clearInterval(rotateTimer);
      rotateTimer = null;
    }
  }

  function applyPhase(key) {
    var cfg = phaseConfig(key);
    if (linePrimary) linePrimary.textContent = cfg.title;
    phraseIndex = 0;
    if (lineSecondary && cfg.lines.length) {
      lineSecondary.textContent = cfg.lines[0];
    }
    clearRotate();
    if (cfg.lines.length > 1 && lineSecondary) {
      rotateTimer = setInterval(function () {
        phraseIndex = (phraseIndex + 1) % cfg.lines.length;
        lineSecondary.textContent = cfg.lines[phraseIndex];
      }, 4200);
    }
  }

  function ensureDom() {
    if (overlay) return;
    overlay = document.createElement('div');
    overlay.id = 'studioWaitingOverlay';
    overlay.className = 'studio-wait-overlay';
    overlay.setAttribute('role', 'status');
    overlay.setAttribute('aria-live', 'polite');
    overlay.setAttribute('aria-busy', 'true');
    overlay.innerHTML =
      '<div class="studio-wait-overlay__panel">' +
      '<div class="studio-wait-overlay__orbit" aria-hidden="true">' +
      '<span></span><span></span><span></span>' +
      '</div>' +
      '<p class="studio-wait-overlay__title" id="studioWaitPrimary"></p>' +
      '<p class="studio-wait-overlay__sub" id="studioWaitSecondary"></p>' +
      '<p class="studio-wait-overlay__hint">You can safely wait here — we will update the page when there is something new.</p>' +
      '</div>';
    document.body.appendChild(overlay);
    linePrimary = document.getElementById('studioWaitPrimary');
    lineSecondary = document.getElementById('studioWaitSecondary');
    orbitals = overlay.querySelector('.studio-wait-overlay__orbit');
  }

  function show(key) {
    ensureDom();
    applyPhase(key);
    overlay.classList.add('studio-wait-overlay--visible');
    overlay.setAttribute('aria-hidden', 'false');
  }

  function hide() {
    clearRotate();
    if (overlay) {
      overlay.classList.remove('studio-wait-overlay--visible');
      overlay.setAttribute('aria-hidden', 'true');
      overlay.setAttribute('aria-busy', 'false');
    }
  }

  var stack = [];

  return {
    /**
     * @param {string} [phase] — internal key mapped to generic UI copy (not shown to user)
     */
    push: function (phase) {
      stack.push(phase || 'default');
      depth++;
      if (depth === 1) show(stack[stack.length - 1]);
      else applyPhase(stack[stack.length - 1]);
    },
    pop: function () {
      if (depth <= 0) return;
      stack.pop();
      depth--;
      if (depth === 0) hide();
      else applyPhase(stack[stack.length - 1]);
    },
  };
})();

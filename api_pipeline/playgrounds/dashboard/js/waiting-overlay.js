/**
 * Full-screen wait for dashboard server calls. Copy is generic — never shows endpoint or provider names.
 */

const PHASES = {
  default: {
    title: 'Please wait',
    lines: [
      'Your request was sent.',
      'Processing can take a little while.',
    ],
  },
  submit_job: {
    title: 'Submitting',
    lines: [
      'Sending your video job…',
      'The server is accepting your request.',
    ],
  },
  job_control: {
    title: 'Updating job',
    lines: [
      'Applying your action…',
    ],
  },
  upload: {
    title: 'Uploading',
    lines: [
      'Sending your file…',
    ],
  },
  load_settings: {
    title: 'Loading',
    lines: [
      'Fetching workspace options…',
    ],
  },
  monitor: {
    title: 'Connecting',
    lines: [
      'Loading job status…',
    ],
  },
  list_jobs: {
    title: 'Refreshing',
    lines: [
      'Fetching active jobs…',
    ],
  },
};

let _depth = 0;
const _stack = [];
let _root = null;
let _titleEl = null;
let _subEl = null;
let _rotateTimer = null;
let _phraseIndex = 0;

function _cfg(key) {
  return PHASES[key] || PHASES.default;
}

function _clearRotate() {
  if (_rotateTimer) {
    clearInterval(_rotateTimer);
    _rotateTimer = null;
  }
}

function _applyPhase(key) {
  const c = _cfg(key);
  if (_titleEl) _titleEl.textContent = c.title;
  _phraseIndex = 0;
  if (_subEl && c.lines.length) _subEl.textContent = c.lines[0];
  _clearRotate();
  if (c.lines.length > 1 && _subEl) {
    _rotateTimer = setInterval(() => {
      _phraseIndex = (_phraseIndex + 1) % c.lines.length;
      _subEl.textContent = c.lines[_phraseIndex];
    }, 4000);
  }
}

function _ensureDom() {
  if (_root) return;
  _root = document.createElement('div');
  _root.id = 'dashboardWaitingOverlay';
  _root.className = 'dashboard-wait-overlay';
  _root.setAttribute('role', 'status');
  _root.setAttribute('aria-live', 'polite');
  _root.innerHTML =
    '<div class="dashboard-wait-overlay__panel">' +
    '<div class="dashboard-wait-overlay__bars" aria-hidden="true">' +
    '<span></span><span></span><span></span><span></span><span></span>' +
    '</div>' +
    '<p class="dashboard-wait-overlay__title" id="dashWaitTitle"></p>' +
    '<p class="dashboard-wait-overlay__sub" id="dashWaitSub"></p>' +
    '<p class="dashboard-wait-overlay__hint">Stay on this page until the action finishes.</p>' +
    '</div>';
  document.body.appendChild(_root);
  _titleEl = document.getElementById('dashWaitTitle');
  _subEl = document.getElementById('dashWaitSub');
}

function _show(key) {
  _ensureDom();
  _applyPhase(key);
  _root.classList.add('dashboard-wait-overlay--visible');
  _root.setAttribute('aria-busy', 'true');
  _root.setAttribute('aria-hidden', 'false');
}

function _hide() {
  _clearRotate();
  if (_root) {
    _root.classList.remove('dashboard-wait-overlay--visible');
    _root.setAttribute('aria-busy', 'false');
    _root.setAttribute('aria-hidden', 'true');
  }
}

export function pushWaiting(phase) {
  _stack.push(phase || 'default');
  _depth++;
  if (_depth === 1) _show(_stack[_stack.length - 1]);
  else _applyPhase(_stack[_stack.length - 1]);
}

export function popWaiting() {
  if (_depth <= 0) return;
  _stack.pop();
  _depth--;
  if (_depth === 0) _hide();
  else _applyPhase(_stack[_stack.length - 1]);
}

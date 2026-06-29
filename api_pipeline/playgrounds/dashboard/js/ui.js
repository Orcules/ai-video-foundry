/**
 * UI utility functions — setStatus, setProgress, setButtons, escapeHtml, etc.
 *
 * To break the ui <-> job circular dependency, setButtons uses a registered
 * callback for populateRestartDropdown. job.js calls registerRestartPopulator()
 * to wire it up.
 */
import { state } from './state.js';

let _restartPopulator = null;

export function registerRestartPopulator(fn) {
  _restartPopulator = fn;
}

export function setStatus(status, text) {
  const badge = document.getElementById('statusBadge');
  const statusText = document.getElementById('statusText');
  badge.className = 'status-badge badge-' + status;
  statusText.textContent = text;
}

export function setProgress(pct, className) {
  const fill = document.getElementById('progressFill');
  const text = document.getElementById('progressText');
  fill.style.width = pct + '%';
  text.textContent = pct + '%';
  fill.className = 'progress-fill' + (className ? ' ' + className : '');
}

export function setButtons(mode) {
  const startBtn = document.getElementById('startBtn');
  const pauseBtn = document.getElementById('pauseBtn');
  const abortBtn = document.getElementById('abortBtn');
  const resumeBtn = document.getElementById('resumeBtn');
  const restartRow = document.getElementById('restartRow');
  const simToggle = document.getElementById('simulationMode');

  switch (mode) {
    case 'idle':
      startBtn.disabled = false; startBtn.style.display = '';
      pauseBtn.disabled = true; pauseBtn.style.display = 'none';
      abortBtn.style.display = 'none';
      resumeBtn.style.display = 'none';
      restartRow.style.display = 'none';
      simToggle.disabled = false;
      break;
    case 'running':
      startBtn.disabled = true; startBtn.style.display = 'none';
      pauseBtn.disabled = false; pauseBtn.style.display = '';
      abortBtn.disabled = false; abortBtn.style.display = '';
      resumeBtn.style.display = 'none';
      restartRow.style.display = 'none';
      simToggle.disabled = true;
      break;
    case 'paused':
      startBtn.disabled = true; startBtn.style.display = 'none';
      pauseBtn.disabled = true; pauseBtn.style.display = 'none';
      abortBtn.disabled = false; abortBtn.style.display = '';
      resumeBtn.disabled = false; resumeBtn.style.display = '';
      restartRow.style.display = 'flex';
      if (_restartPopulator) _restartPopulator();
      simToggle.disabled = true;
      break;
    case 'failed':
      startBtn.disabled = false; startBtn.style.display = '';
      pauseBtn.disabled = true; pauseBtn.style.display = 'none';
      abortBtn.style.display = 'none';
      resumeBtn.style.display = 'none';
      restartRow.style.display = 'flex';
      if (_restartPopulator) _restartPopulator();
      simToggle.disabled = false;
      break;
    case 'completed':
      startBtn.disabled = false; startBtn.style.display = '';
      pauseBtn.disabled = true; pauseBtn.style.display = 'none';
      abortBtn.style.display = 'none';
      resumeBtn.style.display = 'none';
      restartRow.style.display = 'flex';
      if (_restartPopulator) _restartPopulator();
      simToggle.disabled = false;
      break;
    case 'queued':
      startBtn.disabled = true; startBtn.style.display = 'none';
      pauseBtn.disabled = true; pauseBtn.style.display = 'none';
      abortBtn.disabled = false; abortBtn.style.display = '';
      resumeBtn.style.display = 'none';
      restartRow.style.display = 'none';
      simToggle.disabled = true;
      break;
  }
}

export function getStepClass(step) {
  const s = step.toLowerCase().replace(/[\s.-]/g, '');
  if (s.includes('pipeline')) return 'step-pipeline';
  if (s.includes('step0')) return 'step-step0';
  if (s.includes('step1')) return 'step-step1';
  if (s.includes('step2')) return 'step-step2';
  if (s.includes('step3')) return 'step-step3';
  if (s.includes('steps47') || s.includes('steps4')) return 'step-steps47';
  if (s.includes('step75')) return 'step-steps47';
  if (s.includes('step8')) return 'step-step8';
  if (s.includes('step9')) return 'step-step9';
  if (s.includes('upload') || s.includes('mux')) return 'step-upload';
  if (s.includes('server')) return 'step-server';
  return 'step-pipeline';
}

export function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

export function toggleSection(header) {
  header.classList.toggle('collapsed');
  const body = header.nextElementSibling;
  if (body) body.classList.toggle('collapsed');
}

export function copyJobId() {
  if (!state.currentJobId) return;
  const text = state.currentJobId;
  const onSuccess = () => {
    const btn = document.getElementById('copyJobIdBtn');
    btn.textContent = '\u2705';
    setTimeout(() => { btn.innerHTML = '&#x1F4CB;'; }, 1500);
  };
  // navigator.clipboard requires HTTPS; fall back for HTTP origins
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(onSuccess);
  } else {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    onSuccess();
  }
}

export function updateQueueDisplay(queuePos, activeJobs, maxConcurrent) {
  const el = document.getElementById('queueDisplay');
  if (!el) return;
  if (queuePos != null) {
    el.style.display = '';
    el.textContent = 'Queue position: ' + queuePos + ' | Active: ' + (activeJobs || 0) + '/' + (maxConcurrent || '?');
  } else {
    el.style.display = 'none';
    el.textContent = '';
  }
}

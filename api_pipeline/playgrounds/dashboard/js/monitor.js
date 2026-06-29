import { state } from './state.js';
import { authHeaders } from './config.js';
import { pushWaiting, popWaiting } from './waiting-overlay.js';
import { setStatus, setButtons, setProgress, updateQueueDisplay } from './ui.js';
import { addEvent } from './event-log.js';
import { stopCostPoll, updateCostDisplay } from './cost.js';
import { connectSSE } from './sse.js';
import { startQueuePoll, stopQueuePoll } from './queue.js';
import { fetchFinalOutput } from './output.js';

/**
 * Shared cleanup — resets all job-related state so a new job or monitor can start fresh.
 */
export function cleanupPreviousJob() {
  // Close SSE
  if (state.eventSource) {
    state.eventSource.close();
    state.eventSource = null;
  }

  // Mux realtime
  if (state.muxRealtimeChannel) {
    try { state.muxRealtimeChannel.unsubscribe(); } catch (e) {}
    state.muxRealtimeChannel = null;
  }
  if (state.muxPollInterval) {
    clearInterval(state.muxPollInterval);
    state.muxPollInterval = null;
  }

  // Polls
  stopCostPoll();
  stopQueuePoll();
  updateQueueDisplay(null);

  // Clear event log
  document.getElementById('eventLog').innerHTML = '';
  state.eventCount = 0;
  document.getElementById('eventCount').textContent = '0 events';

  // Hide output & logs sections
  document.getElementById('outputSection').classList.remove('visible');
  document.getElementById('outputLinks').innerHTML = '';
  document.getElementById('logsSection').classList.remove('visible');
  document.getElementById('fallbackList').innerHTML = '';

  // Reset progress, cost, fetch guard
  setProgress(0, '');
  updateCostDisplay(null);
  state._fetchingFinalOutput = false;
}

/**
 * Monitor an existing job by ID — fetches its current state and connects to the
 * appropriate live stream or shows final output.
 */
export async function monitorJob() {
  const input = document.getElementById('monitorJobId');
  const btn = document.getElementById('monitorBtn');
  const jobId = input.value.trim();

  if (!jobId) {
    input.classList.add('shake');
    setTimeout(() => input.classList.remove('shake'), 500);
    return;
  }

  cleanupPreviousJob();

  // Set current job
  state.currentJobId = jobId;
  document.getElementById('jobIdDisplay').textContent = jobId;
  document.getElementById('copyJobIdBtn').style.display = '';

  setStatus('running', 'Connecting...');
  btn.disabled = true;

  try {
    pushWaiting('monitor');
    const resp = await fetch(state.API_BASE + '/api/jobs/' + jobId, { headers: authHeaders() });

    if (resp.status === 404) {
      setStatus('error', 'Not Found');
      addEvent({
        timestamp: new Date().toLocaleTimeString('en-US', { hour12: false }),
        step: 'CLIENT',
        message: 'Job not found: ' + jobId,
        event_type: 'error',
        progress: -1,
        elapsed: null,
      });
      setButtons('idle');
      return;
    }

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: resp.statusText }));
      setStatus('error', 'Error');
      addEvent({
        timestamp: new Date().toLocaleTimeString('en-US', { hour12: false }),
        step: 'CLIENT',
        message: 'Failed to fetch job: ' + (err.detail || resp.statusText),
        event_type: 'error',
        progress: -1,
        elapsed: null,
      });
      setButtons('idle');
      return;
    }

    const job = await resp.json();

    switch (job.status) {
      case 'processing':
        setButtons('running');
        connectSSE(jobId);  // replays all events from 0
        break;

      case 'queued':
        setStatus('queued', 'Queued');
        setButtons('queued');
        startQueuePoll(jobId);
        break;

      case 'paused':
        setStatus('paused', 'Paused');
        setButtons('paused');
        if (job.cost_usd) updateCostDisplay(job.cost_usd);
        if (job.progress != null) setProgress(job.progress, '');
        break;

      case 'completed':
      case 'failed':
      case 'aborted':
        fetchFinalOutput(jobId);
        break;

      default:
        setStatus('idle', job.status || 'Unknown');
        setButtons('idle');
        break;
    }
  } catch (e) {
    setStatus('error', 'Connection Error');
    addEvent({
      timestamp: new Date().toLocaleTimeString('en-US', { hour12: false }),
      step: 'CLIENT',
      message: 'Monitor failed: ' + e.message,
      event_type: 'error',
      progress: -1,
      elapsed: null,
    });
    setButtons('idle');
  } finally {
    popWaiting();
    btn.disabled = false;
  }
}

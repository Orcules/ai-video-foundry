import { state } from './state.js';
import { setStatus } from './ui.js';
import { addEvent } from './event-log.js';
import { startCostPoll } from './cost.js';

export function connectSSE(jobId, cursor) {
  if (state.eventSource) {
    state.eventSource.close();
  }

  // If no cursor provided (new job), clear the log. Otherwise keep history.
  if (!cursor) {
    document.getElementById('eventLog').innerHTML = '';
    state.eventCount = 0;
    document.getElementById('eventCount').textContent = '0 events';
  }

  setStatus('running', 'Connecting...');
  startCostPoll(jobId);
  const params = new URLSearchParams();
  if (cursor) params.set('after', cursor);
  if (state.API_KEY) params.set('token', state.API_KEY);
  const qs = params.toString();
  const url = state.API_BASE + '/api/jobs/' + jobId + '/events' + (qs ? '?' + qs : '');
  state.eventSource = new EventSource(url);

  state.eventSource.onmessage = function(e) {
    try {
      const ev = JSON.parse(e.data);
      addEvent(ev);
    } catch (err) {
      console.error('Parse error:', err);
    }
  };

  state.eventSource.addEventListener('done', function() {
    if (state.eventSource) { state.eventSource.close(); state.eventSource = null; }
    // Dispatch terminal event so output.js picks it up
    document.dispatchEvent(new CustomEvent('pipeline:terminal', { detail: { jobId } }));
  });

  state.eventSource.onerror = function() {
    if (state.eventSource && state.eventSource.readyState === EventSource.CLOSED) {
      state.eventSource = null;
    }
  };
}

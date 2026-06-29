import { state } from './state.js';
import { authHeaders } from './config.js';
import { setStatus, setButtons, updateQueueDisplay } from './ui.js';
import { connectSSE } from './sse.js';

export function startQueuePoll(jobId) {
  stopQueuePoll();
  state.queuePollTimer = setInterval(async () => {
    try {
      const resp = await fetch(state.API_BASE + '/api/jobs/' + jobId, { headers: authHeaders() });
      if (!resp.ok) return;
      const job = await resp.json();
      if (job.status === 'queued') {
        // Still queued — update display but keep polling
        return;
      }
      // Job is no longer queued — transition to SSE
      stopQueuePoll();
      updateQueueDisplay(null);
      if (job.status === 'aborted') {
        setStatus('aborted', 'Aborted');
        setButtons('idle');
      } else {
        setStatus('running', 'Running');
        setButtons('running');
        connectSSE(jobId);
      }
    } catch (e) {
      console.error('Queue poll error:', e);
    }
  }, 5000);
}

export function stopQueuePoll() {
  if (state.queuePollTimer) {
    clearInterval(state.queuePollTimer);
    state.queuePollTimer = null;
  }
}

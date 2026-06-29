import { state } from './state.js';
import { authHeaders } from './config.js';

export function startCostPoll(jobId) {
  stopCostPoll();
  updateCostDisplay(null);
  state.costPollInterval = setInterval(async () => {
    try {
      const resp = await fetch(state.API_BASE + '/api/jobs/' + jobId, { headers: authHeaders() });
      if (!resp.ok) return;
      const job = await resp.json();
      updateCostDisplay(job.cost_usd);
    } catch (e) {}
  }, 2000);
}

export function stopCostPoll() {
  if (state.costPollInterval) {
    clearInterval(state.costPollInterval);
    state.costPollInterval = null;
  }
}

export function updateCostDisplay(costUsd) {
  const el = document.getElementById('costDisplay');
  if (!el) return;
  if (costUsd != null && costUsd > 0) {
    el.textContent = 'Accumulated cost: $' + costUsd.toFixed(4);
  } else {
    el.textContent = '';
  }
}

import { state } from './state.js';
import { authHeaders } from './config.js';
import { pushWaiting, popWaiting } from './waiting-overlay.js';
import { monitorJob } from './monitor.js';

/**
 * Fetch active jobs (processing + queued) and populate the dropdown.
 */
export async function refreshActiveJobs() {
  const select = document.getElementById('activeJobsSelect');
  const btn = document.getElementById('refreshJobsBtn');

  btn.disabled = true;
  btn.textContent = '...';

  pushWaiting('list_jobs');
  try {
    const [procResp, queuedResp] = await Promise.all([
      fetch(state.API_BASE + '/api/jobs?status=processing', { headers: authHeaders() }),
      fetch(state.API_BASE + '/api/jobs?status=queued', { headers: authHeaders() }),
    ]);

    const procJobs = procResp.ok ? await procResp.json() : [];
    const queuedJobs = queuedResp.ok ? await queuedResp.json() : [];

    // Normalize — endpoint may return { jobs: [...] } or [...]
    const proc = Array.isArray(procJobs) ? procJobs : (procJobs.jobs || []);
    const queued = Array.isArray(queuedJobs) ? queuedJobs : (queuedJobs.jobs || []);

    const allJobs = [...proc, ...queued];

    // Clear existing options
    select.innerHTML = '';

    if (allJobs.length === 0) {
      select.innerHTML = '<option value="">-- No active jobs --</option>';
      btn.textContent = 'REFRESH';
      return;
    }

    select.innerHTML = '<option value="">-- Select active job --</option>';

    for (const job of allJobs) {
      const opt = document.createElement('option');
      opt.value = job.id || job.job_id;

      const vtype = job.video_type || job.params?.video_type || '?';
      const progress = job.progress != null ? job.progress + '%' : '—';
      const status = job.status || '?';
      const created = job.created_at ? new Date(job.created_at).toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' }) : '';

      opt.textContent = `[${status}] ${vtype} — ${progress} — ${created}`;
      select.appendChild(opt);
    }

    btn.textContent = `REFRESH (${allJobs.length})`;
  } catch (e) {
    select.innerHTML = '<option value="">-- Fetch failed --</option>';
    btn.textContent = 'REFRESH';
    console.error('refreshActiveJobs error:', e);
  } finally {
    popWaiting();
    btn.disabled = false;
  }
}

/**
 * When a job is selected from the dropdown, fill the monitor input and start monitoring.
 */
export function selectActiveJob() {
  const select = document.getElementById('activeJobsSelect');
  const jobId = select.value;
  if (!jobId) return;

  document.getElementById('monitorJobId').value = jobId;
  monitorJob();
}

import { state } from './state.js';
import { authHeaders } from './config.js';
import { setStatus, setButtons, setProgress, escapeHtml } from './ui.js';
import { stopCostPoll, updateCostDisplay } from './cost.js';
import { buildVideoPreviewHtml, subscribeMuxRealtime } from './mux.js';
import { addEvent } from './event-log.js';

// Listen for terminal pipeline events (dispatched by event-log.js and sse.js)
document.addEventListener('pipeline:terminal', (e) => {
  fetchFinalOutput(e.detail.jobId);
});

export async function fetchFinalOutput(jobId) {
  if (state._fetchingFinalOutput) return;
  state._fetchingFinalOutput = true;
  try {
    const resp = await fetch(state.API_BASE + '/api/jobs/' + jobId, { headers: authHeaders() });
    if (!resp.ok) return;

    const job = await resp.json();
    const status = job.status;

    stopCostPoll();
    updateCostDisplay(job.cost_usd);

    if (status === 'completed') {
      setStatus('complete', 'Completed');
      setProgress(100, 'complete');
      setButtons('completed');

      const output = job.output || {};
      const section = document.getElementById('outputSection');
      const links = document.getElementById('outputLinks');
      links.innerHTML = '';

      // Show total cost at top of output
      const totalCost = output.cost_usd || job.cost_usd;
      if (totalCost != null && totalCost > 0) {
        const costDiv = document.createElement('div');
        costDiv.className = 'output-link';
        costDiv.innerHTML = `<span>Total Cost:</span><span style="color:var(--green);font-weight:bold;font-family:monospace">$${totalCost.toFixed(4)}</span>`;
        links.appendChild(costDiv);
      }

      // Video preview — show clickable thumbnail of the final video
      const previewUrl = output.final_stream_url || output.final_mp4_url;
      if (previewUrl) {
        const previewDiv = document.createElement('div');
        previewDiv.className = 'output-video-preview';
        previewDiv.innerHTML = buildVideoPreviewHtml(previewUrl);
        links.appendChild(previewDiv);
      }

      const mp4Label = output.mux_status === 'uploading' ? 'Subtitled Video (pre-CDN)' : 'Final MP4 (download)';
      const entries = [
        ['Final Stream (HLS)', output.final_stream_url],
        [mp4Label, output.final_mp4_url],
        ['VO Audio', output.vo_audio_url],
        ['Music', output.music_url],
        ['Concat (no audio)', output.concat_url],
      ];

      for (const [label, url] of entries) {
        if (url) {
          const div = document.createElement('div');
          div.className = 'output-link';
          div.innerHTML = `<span>${label}:</span><a href="${escapeHtml(url)}" target="_blank">${escapeHtml(url.length > 80 ? url.substring(0, 80) + '...' : url)}</a>`;
          links.appendChild(div);
        }
      }

      // Show local file path if available
      if (output.local_path) {
        const div = document.createElement('div');
        div.className = 'output-link';
        div.innerHTML = `<span>Local file:</span><span style="color:var(--green);word-break:break-all">${escapeHtml(output.local_path)}</span>`;
        links.appendChild(div);
      }

      // Show artifacts folder path if available
      if (output.artifacts_folder) {
        const div = document.createElement('div');
        div.className = 'output-link';
        div.innerHTML = `<span>Artifacts folder:</span><span style="color:var(--green);word-break:break-all">${escapeHtml(output.artifacts_folder)}</span>`;
        links.appendChild(div);
      }

      // Mux CDN status handling
      if (output.mux_status === 'uploading') {
        const indicator = document.createElement('div');
        indicator.className = 'mux-processing';
        indicator.id = 'muxProcessing';
        indicator.innerHTML = '<div class="pulse"></div> Mux CDN processing — stream URLs will appear when ready';
        links.appendChild(indicator);
        subscribeMuxRealtime(state.currentJobId);
      }

      if (links.children.length > 0) {
        section.classList.add('visible');
      }
    } else if (status === 'failed') {
      setStatus('error', 'Failed');
      setButtons('failed');
      if (job.error) {
        addEvent({ timestamp: new Date().toLocaleTimeString('en-US', { hour12: false }), step: 'SERVER', message: 'Error: ' + job.error, event_type: 'error', progress: -1, elapsed: null });
      }
    } else if (status === 'aborted') {
      setStatus('aborted', 'Aborted');
      setButtons('idle');
    } else if (status === 'paused') {
      setStatus('paused', 'Paused');
      setButtons('paused');
    } else if (status === 'queued') {
      setStatus('queued', 'Queued');
      setButtons('queued');
    }

    // Fetch fallback logs for any terminal state
    if (['completed', 'failed', 'aborted'].includes(status)) {
      fetchFallbackLogs(jobId);
    }
  } catch (e) {
    console.error('Failed to fetch final output:', e);
  } finally {
    state._fetchingFinalOutput = false;
  }
}

export async function fetchFallbackLogs(jobId) {
  const section = document.getElementById('logsSection');
  const list = document.getElementById('fallbackList');
  const countEl = document.getElementById('logCount');
  list.innerHTML = '';
  section.classList.remove('visible');

  try {
    // Try persisted logs from job output first (survives container restarts)
    let logs = [];
    let job = null;
    const jobResp = await fetch(state.API_BASE + '/api/jobs/' + jobId, { headers: authHeaders() });
    if (jobResp.ok) {
      job = await jobResp.json();
      logs = (job.output && job.output.fallback_logs) || [];
      // Also check error_details.fallback_logs for failed jobs
      if (logs.length === 0 && job.error_details && job.error_details.fallback_logs) {
        logs = job.error_details.fallback_logs;
      }
    }

    // Fall back to live in-memory endpoint (for in-progress or if output has no logs)
    if (logs.length === 0) {
      const resp = await fetch(state.API_BASE + '/api/jobs/' + jobId + '/logs', { headers: authHeaders() });
      if (resp.ok) {
        const data = await resp.json();
        logs = data.logs || [];
      }
    }

    for (const entry of logs) {
      const row = document.createElement('div');
      row.className = 'fallback-entry';
      const source = entry.logger ? entry.logger.replace('api_pipeline.', '') : '';
      row.innerHTML = `
        <span class="fb-time">${escapeHtml(entry.timestamp || '')}</span>
        <span class="fb-source">${escapeHtml(source)}</span>
        <span class="fb-msg">${escapeHtml(entry.message || '')}</span>
      `;
      list.appendChild(row);
    }

    // Render output issues (validation warnings/errors)
    const issues = (job && job.output && job.output.issues) || [];
    for (const issue of issues) {
      const row = document.createElement('div');
      row.className = 'fallback-entry';
      row.style.borderLeftColor = issue.severity === 'critical' ? 'var(--red)' : 'var(--yellow)';
      row.innerHTML = `
        <span class="fb-time">${escapeHtml(issue.severity.toUpperCase())}</span>
        <span class="fb-source">[${escapeHtml(issue.field)}]</span>
        <span class="fb-msg">${escapeHtml(issue.message)}</span>
      `;
      list.appendChild(row);
    }

    if (logs.length === 0 && issues.length === 0) {
      list.innerHTML = '<div class="no-fallbacks">No issues — all steps ran on the primary path.</div>';
    }

    countEl.textContent = logs.length + issues.length;
    section.classList.add('visible');
  } catch (e) {
    console.error('Failed to fetch fallback logs:', e);
  }
}

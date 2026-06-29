import { state } from './state.js';
import { authHeaders } from './config.js';
import { escapeHtml } from './ui.js';
import { addEvent } from './event-log.js';
import { openLightbox } from './lightbox.js';

function getMuxPlaybackId(url) {
  if (!url || !url.includes('stream.mux.com/')) return null;
  const m = url.match(/stream\.mux\.com\/([^/.?]+)/);
  return m ? m[1] : null;
}

function getMuxThumbnail(url) {
  const pid = getMuxPlaybackId(url);
  return pid ? `https://image.mux.com/${pid}/thumbnail.jpg?width=640` : null;
}

export function buildVideoPreviewHtml(playUrl) {
  const thumbUrl = getMuxThumbnail(playUrl);
  const lightboxUrl = escapeHtml(playUrl);
  if (thumbUrl) {
    return `
      <div class="preview-label">FINAL VIDEO</div>
      <div class="preview-wrap" onclick="openLightbox('video','${lightboxUrl}')" title="Click to play fullscreen">
        <img src="${escapeHtml(thumbUrl)}" style="width:100%;height:100%;object-fit:cover;border-radius:6px" loading="lazy">
        <div class="preview-play"><div class="preview-play-icon">&#9654;</div></div>
      </div>`;
  }
  return `
    <div class="preview-label">FINAL VIDEO</div>
    <div class="preview-wrap" onclick="openLightbox('video','${lightboxUrl}')" title="Click to play fullscreen">
      <video preload="metadata" muted playsinline><source src="${lightboxUrl}" type="video/mp4"></video>
      <div class="preview-play"><div class="preview-play-icon">&#9654;</div></div>
    </div>`;
}

export async function subscribeMuxRealtime(jobId) {
  // Cleanup previous subscription and poll
  if (state.muxRealtimeChannel) {
    try { state.muxRealtimeChannel.unsubscribe(); } catch (e) {}
    state.muxRealtimeChannel = null;
  }
  if (state.muxPollInterval) {
    clearInterval(state.muxPollInterval);
    state.muxPollInterval = null;
  }

  // Initialize Supabase client if needed
  if (!state.supabaseClient) {
    try {
      const configResp = await fetch(state.API_BASE + '/api/config', { headers: authHeaders() });
      if (!configResp.ok) return;
      const config = await configResp.json();
      if (!config.supabase_url || !config.supabase_anon_key) return;
      state.supabaseClient = supabase.createClient(config.supabase_url, config.supabase_anon_key);
    } catch (e) {
      console.error('Failed to init Supabase client for Realtime:', e);
    }
  }

  // Subscribe to changes on this job (if Supabase client available)
  if (state.supabaseClient) {
    state.muxRealtimeChannel = state.supabaseClient
      .channel('mux-status-' + jobId)
      .on(
        'postgres_changes',
        { event: 'UPDATE', schema: 'public', table: 'video_jobs', filter: 'id=eq.' + jobId },
        (payload) => {
          const output = payload.new && payload.new.output;
          if (output && output.mux_status === 'ready') {
            updateMuxUrls(output);
          } else if (output && output.mux_status === 'failed') {
            handleMuxTerminal('failed');
          } else if (output && output.mux_status === 'timeout') {
            handleMuxTerminal('timeout');
          }
        }
      )
      .subscribe();
  }

  // Polling fallback — poll every 15s in case realtime is broken
  async function pollMuxStatus() {
    try {
      const resp = await fetch(state.API_BASE + '/api/jobs/' + jobId, { headers: authHeaders() });
      if (!resp.ok) return;
      const job = await resp.json();
      const output = job.output || {};
      if (output.mux_status === 'ready') {
        updateMuxUrls(output);
      } else if (output.mux_status === 'failed') {
        handleMuxTerminal('failed');
      } else if (output.mux_status === 'timeout') {
        handleMuxTerminal('timeout');
      }
    } catch (e) {}
  }

  state.muxPollInterval = setInterval(pollMuxStatus, 15000);

  // Race condition guard: check if already ready after 1s
  setTimeout(pollMuxStatus, 1000);
}

export function handleMuxTerminal(status) {
  if (state.muxPollInterval) {
    clearInterval(state.muxPollInterval);
    state.muxPollInterval = null;
  }
  const indicator = document.getElementById('muxProcessing');
  if (!indicator) return;
  if (status === 'failed') {
    indicator.innerHTML = '<span style="color:var(--red)">Mux CDN processing failed</span>';
  } else if (status === 'timeout') {
    indicator.innerHTML = '<span style="color:var(--orange)">Mux CDN processing timed out</span>';
  }
}

export function updateMuxUrls(output) {
  // Remove the processing indicator
  const indicator = document.getElementById('muxProcessing');
  if (indicator) indicator.remove();

  // Unsubscribe from realtime
  if (state.muxRealtimeChannel) {
    try { state.muxRealtimeChannel.unsubscribe(); } catch (e) {}
    state.muxRealtimeChannel = null;
  }

  // Clear polling interval
  if (state.muxPollInterval) {
    clearInterval(state.muxPollInterval);
    state.muxPollInterval = null;
  }

  const links = document.getElementById('outputLinks');

  // Update or add stream URL
  const streamEntries = [
    ['Final Stream (HLS)', output.final_stream_url],
    ['Final MP4 (download)', output.final_mp4_url],
  ];

  for (const [label, url] of streamEntries) {
    if (!url) continue;
    // Check if this label already exists and update it, or add new
    let found = false;
    for (const link of links.querySelectorAll('.output-link')) {
      const labelSpan = link.querySelector('span');
      if (labelSpan && labelSpan.textContent.startsWith(label + ':')) {
        link.innerHTML = `<span>${label}:</span><a href="${escapeHtml(url)}" target="_blank">${escapeHtml(url.length > 80 ? url.substring(0, 80) + '...' : url)}</a>`;
        found = true;
        break;
      }
    }
    if (!found) {
      const div = document.createElement('div');
      div.className = 'output-link';
      div.innerHTML = `<span>${label}:</span><a href="${escapeHtml(url)}" target="_blank">${escapeHtml(url.length > 80 ? url.substring(0, 80) + '...' : url)}</a>`;
      links.insertBefore(div, links.firstChild);
    }
  }

  // Update video preview with new Mux MP4 URL
  const newPreviewUrl = output.final_stream_url || output.final_mp4_url;
  const existingPreview = links.querySelector('.output-video-preview');
  if (newPreviewUrl && existingPreview) {
    existingPreview.innerHTML = buildVideoPreviewHtml(newPreviewUrl);
  } else if (newPreviewUrl && !existingPreview) {
    const previewDiv = document.createElement('div');
    previewDiv.className = 'output-video-preview';
    previewDiv.innerHTML = buildVideoPreviewHtml(newPreviewUrl);
    links.insertBefore(previewDiv, links.firstChild);
  }

  // Add event log entry
  addEvent({
    timestamp: new Date().toLocaleTimeString('en-US', { hour12: false }),
    step: 'MUX',
    message: 'Mux CDN ready — stream and MP4 URLs available',
    event_type: 'info',
    progress: -1,
    elapsed: null
  });
}

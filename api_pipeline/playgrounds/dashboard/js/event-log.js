import { state } from './state.js';
import { getStepClass, escapeHtml, setStatus, setButtons, setProgress } from './ui.js';
import { openLightbox } from './lightbox.js';
import { updateCostDisplay } from './cost.js';

function _eventEmoji(msg) {
  const m = msg.toLowerCase();
  if (m.includes('image generated') || m.includes('clean product image')) return '\u{1F5BC}\uFE0F ';
  if (m.includes('video generated') || m.includes('animation')) return '\u{1F3AC} ';
  if (m.includes('voiceover') || m.includes('vo ') || m.includes('vo_') || m.includes('tts') || m.includes('voice')) return '\u{1F399}\uFE0F ';
  if (m.includes('music')) return '\u{1F3B5} ';
  if (m.includes('subtitle') || m.includes('zapcap')) return '\u{1F4AC} ';
  if (m.includes('mux') || m.includes('upload')) return '\u2601\uFE0F ';
  if (m.includes('concatenat') || m.includes('concat')) return '\u{1F517} ';
  if (m.includes('trim')) return '\u2702\uFE0F ';
  if (m.includes('scene prompt') || m.includes('generated') && m.includes('scene')) return '\u{1F3AF} ';
  if (m.includes('pars') || m.includes('prompt')) return '\u{1F4DD} ';
  if (m.includes('character') || m.includes('influencer')) return '\u{1F464} ';
  if (m.includes('quality') || m.includes('scoring')) return '\u{1F50D} ';
  if (m.includes('completed successfully')) return '\u{1F3C1} ';
  if (m.includes('complete')) return '\u2705 ';
  return '';
}

export function addEvent(ev) {
  // Update cost display from any event that carries cost data
  if (ev.cost_usd != null && ev.cost_usd > 0) {
    updateCostDisplay(ev.cost_usd);
  }

  // Skip rendering COST-only events — the accumulated cost display is sufficient
  if (ev.step === 'COST') return;

  const log = document.getElementById('eventLog');
  const row = document.createElement('div');
  row.className = 'event-row ev-type-' + ev.event_type;

  const elapsedStr = ev.elapsed != null ? `${ev.elapsed}s` : '';
  const costStr = (ev.step_cost_usd != null && ev.step_cost_usd > 0.0001)
    ? `+ $${ev.step_cost_usd.toFixed(4)}` : '';
  const emoji = _eventEmoji(ev.message || '');

  let assetHtml = '';
  if (ev.asset_url) {
    if (ev.asset_type === 'image') {
      assetHtml = `<img src="${escapeHtml(ev.asset_url)}" class="ev-asset-thumb"
                        onclick="openLightbox('image','${escapeHtml(ev.asset_url)}')" loading="lazy" title="Click to enlarge">`;
    } else if (ev.asset_type === 'video') {
      assetHtml = `<span class="ev-asset-video-wrap" onclick="openLightbox('video','${escapeHtml(ev.asset_url)}')" title="Click to play">
                     <video class="ev-asset-video" preload="metadata" muted><source src="${escapeHtml(ev.asset_url)}" type="video/mp4"></video>
                     <span class="ev-asset-play">&#9654;</span>
                   </span>`;
    } else if (ev.asset_type === 'audio') {
      assetHtml = `<audio class="ev-asset-audio" controls preload="none">
                     <source src="${escapeHtml(ev.asset_url)}">
                   </audio>`;
    }
  }

  row.innerHTML = `
    <span class="ev-time">${ev.timestamp}</span>
    <span class="ev-step ${getStepClass(ev.step)}">${ev.step}</span>
    <span class="ev-msg">${emoji}${escapeHtml(ev.message)}${assetHtml}</span>
    ${elapsedStr ? `<span class="ev-elapsed">${elapsedStr}</span>` : ''}
    ${costStr ? `<span class="ev-cost">${costStr}</span>` : ''}
  `;

  log.appendChild(row);
  log.scrollTop = log.scrollHeight;

  state.eventCount++;
  document.getElementById('eventCount').textContent = state.eventCount + ' events';

  if (ev.progress >= 0) {
    setProgress(ev.progress, ev.event_type === 'complete' ? 'complete' : ev.event_type === 'error' ? 'error' : '');
  }

  if (ev.event_type === 'start') {
    setStatus('running', 'Running');
    setButtons('running');
  } else if (ev.event_type === 'pause') {
    setStatus('paused', 'Paused');
    setButtons('paused');
  } else if (['complete', 'error', 'abort'].includes(ev.event_type)) {
    // Terminal event — close SSE and dispatch event for output.js
    if (state.eventSource) {
      state.eventSource.close();
      state.eventSource = null;
    }
    document.dispatchEvent(new CustomEvent('pipeline:terminal', { detail: { jobId: state.currentJobId } }));
  }
}

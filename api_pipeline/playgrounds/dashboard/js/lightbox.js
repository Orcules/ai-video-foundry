import { escapeHtml } from './ui.js';

export function openLightbox(type, url) {
  const overlay = document.getElementById('lightboxOverlay');
  const content = document.getElementById('lightboxContent');
  if (type === 'image') {
    content.innerHTML = `<img src="${escapeHtml(url)}">`;
  } else if (type === 'video') {
    const isHls = url.endsWith('.m3u8');
    const vid = document.createElement('video');
    vid.controls = true;
    vid.autoplay = true;
    vid.style.maxWidth = '90vw';
    vid.style.maxHeight = '85vh';
    vid.style.borderRadius = '8px';
    content.innerHTML = '';
    content.appendChild(vid);
    if (isHls && typeof Hls !== 'undefined' && Hls.isSupported()) {
      const hls = new Hls();
      hls.loadSource(url);
      hls.attachMedia(vid);
      hls.on(Hls.Events.MANIFEST_PARSED, () => vid.play());
      // Store hls instance for cleanup
      vid._hls = hls;
    } else {
      vid.src = url;
    }
  }
  overlay.classList.add('active');
}

export function closeLightbox() {
  const overlay = document.getElementById('lightboxOverlay');
  overlay.classList.remove('active');
  const content = document.getElementById('lightboxContent');
  const vid = content.querySelector('video');
  if (vid) {
    vid.pause();
    if (vid._hls) { vid._hls.destroy(); vid._hls = null; }
  }
  content.innerHTML = '';
}

// Close lightbox on Escape key
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeLightbox(); });

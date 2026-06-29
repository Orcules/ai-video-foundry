/**
 * Scene images and videos: grid rendering, approval checkboxes, correction inputs.
 * Called from app.js when intermediates (scene_images, scene_videos) are available.
 */
const StudioMedia = (function () {
  /**
   * @param {Array<string|undefined|false>} urls - Per scene: URL string, undefined (still generating), or false (failed).
   * @param {Array<string|null|undefined>} [errors] - Optional error message per index (used when url === false).
   */
  function renderSceneImages(urls, errors) {
    var grid = document.getElementById('sceneImagesGrid');
    if (!grid) return;
    if (!urls || !urls.length) {
      grid.innerHTML = '';
      grid.appendChild(document.createTextNode('Scene images will appear here after generation.'));
      return;
    }
    var existingCards = grid.querySelectorAll('.studio-media-card');
    // Grow or shrink the grid to match urls.length
    while (existingCards.length > urls.length) {
      grid.removeChild(grid.lastElementChild);
      existingCards = grid.querySelectorAll('.studio-media-card');
    }
    urls.forEach(function (url, i) {
      var card = existingCards[i];
      var isNew = !card;
      if (isNew) {
        card = document.createElement('div');
        card.className = 'studio-media-card';
        card.dataset.sceneIndex = String(i);
      }
      var hasImage = url && typeof url === 'string' && url.length > 5;
      var isFailed = url === false;
      // Update the media element (first child) only if state changed
      var mediaEl = card.querySelector('img, .studio-scene-image-placeholder');
      var currentState = card.dataset.state || '';
      var newState = hasImage ? ('img:' + url) : (isFailed ? 'fail' : 'pending');
      if (isNew || currentState !== newState) {
        if (mediaEl) mediaEl.remove();
        if (hasImage) {
          var img = document.createElement('img');
          img.src = url;
          img.alt = 'Scene ' + (i + 1);
          img.loading = 'lazy';
          card.insertBefore(img, card.firstChild);
        } else if (isFailed) {
          var failPh = document.createElement('div');
          failPh.className = 'studio-scene-image-placeholder studio-scene-image-error';
          var em = (errors && errors[i]) ? String(errors[i]) : 'Generation failed (check API log / Kie credits).';
          if (em.length > 220) em = em.slice(0, 217) + '…';
          failPh.textContent = em;
          card.insertBefore(failPh, card.firstChild);
        } else {
          var placeholder = document.createElement('div');
          placeholder.className = 'studio-scene-image-placeholder';
          placeholder.textContent = 'Generating…';
          card.insertBefore(placeholder, card.firstChild);
        }
        card.dataset.state = newState;
      }
      if (isNew) {
        var label = document.createElement('label');
        label.innerHTML = '<input type="checkbox" checked> Approve';
        var input = document.createElement('input');
        input.type = 'text';
        input.className = 'studio-input';
        input.placeholder = 'Correction notes (for Fix this image)';
        input.dataset.sceneIndex = String(i);
        card.appendChild(label);
        card.appendChild(input);
        var btnFix = document.createElement('button');
        btnFix.type = 'button';
        btnFix.className = 'studio-btn studio-btn-ghost';
        btnFix.textContent = 'Fix this image';
        btnFix.dataset.sceneIndex = String(i);
        btnFix.dataset.action = 'fix';
        card.appendChild(btnFix);
        grid.appendChild(card);
      }
      // Show/hide Regenerate button based on image/fail state
      var btnRegen = card.querySelector('button[data-action="regen"]');
      if (hasImage || isFailed) {
        if (!btnRegen) {
          btnRegen = document.createElement('button');
          btnRegen.type = 'button';
          btnRegen.className = 'studio-btn studio-btn-ghost';
          btnRegen.textContent = 'Regenerate';
          btnRegen.dataset.sceneIndex = String(i);
          btnRegen.dataset.action = 'regen';
          var fixBtn = card.querySelector('button[data-action="fix"]');
          if (fixBtn) card.insertBefore(btnRegen, fixBtn);
          else card.appendChild(btnRegen);
        }
      } else if (btnRegen) {
        btnRegen.remove();
      }
    });
  }

  function renderSceneVideos(urls) {
    var grid = document.getElementById('sceneVideosGrid');
    if (!grid) return;
    if (!urls || !urls.length) {
      grid.innerHTML = '';
      grid.appendChild(document.createTextNode('Scene videos will appear here after animation.'));
      return;
    }
    var existingCards = grid.querySelectorAll('.studio-media-card');
    while (existingCards.length > urls.length) {
      grid.removeChild(grid.lastElementChild);
      existingCards = grid.querySelectorAll('.studio-media-card');
    }
    urls.forEach(function (url, i) {
      var card = existingCards[i];
      var isNew = !card;
      if (isNew) {
        card = document.createElement('div');
        card.className = 'studio-media-card';
        card.dataset.sceneIndex = String(i);
      }
      var mediaEl = card.querySelector('video, .studio-scene-image-placeholder');
      var currentState = card.dataset.state || '';
      var newState = url ? ('vid:' + url) : 'pending';
      if (isNew || currentState !== newState) {
        if (mediaEl) mediaEl.remove();
        if (url) {
          var video = document.createElement('video');
          video.src = url;
          video.controls = true;
          video.muted = true;
          video.preload = 'metadata';
          card.insertBefore(video, card.firstChild);
        } else {
          var placeholder = document.createElement('div');
          placeholder.className = 'studio-scene-image-placeholder';
          placeholder.textContent = 'Animating…';
          card.insertBefore(placeholder, card.firstChild);
        }
        card.dataset.state = newState;
      }
      if (isNew) {
        var label = document.createElement('label');
        label.innerHTML = '<input type="checkbox" checked> Approve';
        var input = document.createElement('input');
        input.type = 'text';
        input.className = 'studio-input';
        input.placeholder = 'Motion prompt';
        input.dataset.sceneIndex = String(i);
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'studio-btn studio-btn-ghost';
        btn.textContent = 'Re-animate';
        btn.dataset.sceneIndex = String(i);
        card.appendChild(label);
        card.appendChild(input);
        card.appendChild(btn);
        grid.appendChild(card);
      }
    });
  }

  return {
    renderSceneImages: renderSceneImages,
    renderSceneVideos: renderSceneVideos
  };
})();

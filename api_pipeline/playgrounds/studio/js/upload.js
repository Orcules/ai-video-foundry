/**
 * Upload zones: drag-drop, preview, multi-file. Uses StudioAPI.uploadFile.
 */
const StudioUpload = (function () {
  function createZone(opts) {
    const optsIn = opts || {};
    const { id, accept, label, allowMultiple, onChange } = optsIn;
    function notifyChange() {
      if (typeof onChange === 'function') {
        try {
          onChange();
        } catch (e) {}
      }
    }
    const div = document.createElement('div');
    div.className = 'studio-upload-zone';
    div.dataset.uploadId = id || ('zone-' + Math.random().toString(36).slice(2));
    div.innerHTML =
      '<p>' + (label || 'Drop file here or click to browse') + '</p>' +
      '<input type="file" hidden accept="' + (accept || 'image/*,video/*') + '" ' +
      (allowMultiple ? 'multiple' : '') + '>';
    const input = div.querySelector('input');
    let currentUrl = null;
    let currentFile = null;

    function renderPreview(url, file) {
      if (url) {
        div.classList.add('has-file');
        const isVideo = (file && file.type.startsWith('video/')) || /\.(mp4|webm|mov)$/i.test(url);
        if (isVideo) {
          div.querySelector('p').innerHTML = '<video class="studio-upload-preview" src="' + url + '" muted></video><br><span class="studio-upload-remove">Remove</span>';
        } else {
          div.querySelector('p').innerHTML = '<img class="studio-upload-preview" src="' + url + '" alt=""><br><span class="studio-upload-remove">Remove</span>';
        }
        div.querySelector('.studio-upload-remove').onclick = clear;
      } else {
        div.classList.remove('has-file');
        div.querySelector('p').innerHTML = label || 'Drop file here or click to browse';
      }
    }

    function clear() {
      currentUrl = null;
      currentFile = null;
      input.value = '';
      renderPreview(null);
      notifyChange();
    }

    function handleFile(file) {
      if (!file) return;
      currentFile = file;
      const reader = new FileReader();
      reader.onload = function () {
        renderPreview(reader.result, file);
        notifyChange();
      };
      if (file.type.startsWith('image/')) reader.readAsDataURL(file);
      else if (file.type.startsWith('video/')) reader.readAsDataURL(file);
      else {
        renderPreview(null, file);
        notifyChange();
      }
    }

    async function upload() {
      if (!currentFile) return null;
      currentUrl = await StudioAPI.uploadFile(currentFile);
      renderPreview(currentUrl, currentFile);
      notifyChange();
      return currentUrl;
    }

    div.addEventListener('click', function (e) {
      if (!e.target.classList.contains('studio-upload-remove')) input.click();
    });
    div.addEventListener('dragover', function (e) {
      e.preventDefault();
      div.classList.add('dragover');
    });
    div.addEventListener('dragleave', function () {
      div.classList.remove('dragover');
    });
    div.addEventListener('drop', function (e) {
      e.preventDefault();
      div.classList.remove('dragover');
      const f = allowMultiple ? e.dataTransfer.files[0] : e.dataTransfer.files[0];
      if (f) handleFile(f);
    });
    input.addEventListener('change', function () {
      const f = allowMultiple ? input.files[0] : input.files[0];
      if (f) handleFile(f);
    });

    return {
      getEl: function () { return div; },
      getUrl: function () { return currentUrl; },
      setUrl: function (url) {
        currentUrl = url;
        renderPreview(url);
        notifyChange();
      },
      getFile: function () { return currentFile; },
      upload: upload,
      clear: clear
    };
  }

  /**
   * Create N zones (e.g. ref images 1-5) and append to container.
   */
  function createZones(container, count, opts) {
    const zones = [];
    for (let i = 0; i < count; i++) {
      const z = createZone({
        id: (opts.idPrefix || 'zone') + '_' + (i + 1),
        accept: opts.accept || 'image/*',
        label: (opts.labelPrefix || '') + (i + 1) + (opts.labelSuffix || ''),
        allowMultiple: false
      });
      container.appendChild(z.getEl());
      zones.push(z);
    }
    return zones;
  }

  return {
    createZone,
    createZones
  };
})();

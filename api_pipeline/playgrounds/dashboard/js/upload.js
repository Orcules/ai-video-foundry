import { state } from './state.js';
import { authHeaders } from './config.js';
import { pushWaiting, popWaiting } from './waiting-overlay.js';
import { addUrlInput, addAssetInput, updateRowButtons } from './form.js';

export function uploadFile(btnElement) {
  // Find the adjacent input element
  const row = btnElement.closest('.url-input-row, .single-url-row');
  const input = row ? row.querySelector('input') : null;
  if (!input) return;

  // Create hidden file picker
  const filePicker = document.createElement('input');
  filePicker.type = 'file';
  filePicker.accept = '.jpg,.jpeg,.png,.gif,.webp,.mp4,.mov,.webm';
  filePicker.style.display = 'none';

  filePicker.onchange = async function() {
    const file = filePicker.files[0];
    if (!file) return;

    const uploadUrl = state.API_BASE + '/api/upload';
    console.log('[upload] starting:', file.name, '→', uploadUrl);

    // Show filename immediately for visual feedback
    input.value = file.name;
    btnElement.classList.add('uploading');
    btnElement.innerHTML = '&#8987;';

    pushWaiting('upload');
    try {
      const formData = new FormData();
      formData.append('file', file);

      const resp = await fetch(uploadUrl, {
        method: 'POST',
        headers: authHeaders(),
        body: formData,
      });

      console.log('[upload] response status:', resp.status);

      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: resp.statusText }));
        throw new Error(err.detail || 'Upload failed');
      }

      const data = await resp.json();
      // Use URL as-is if absolute (GCS), otherwise prepend API_BASE
      const finalUrl = data.url.startsWith('http') ? data.url : state.API_BASE + data.url;
      input.value = finalUrl;
      console.log('[upload] success:', finalUrl);

      // Show success state briefly
      btnElement.classList.remove('uploading');
      btnElement.classList.add('done');
      btnElement.innerHTML = '&#10003;';
      setTimeout(() => {
        btnElement.classList.remove('done');
        btnElement.innerHTML = '&#8682;';
        updateRowButtons(row);
      }, 2000);
      updateRowButtons(row);
    } catch (e) {
      console.error('[upload] failed:', e);
      btnElement.classList.remove('uploading');
      btnElement.innerHTML = '&#8682;';
      input.value = '';
      input.placeholder = 'Upload failed: ' + e.message;
      updateRowButtons(row);
    } finally {
      popWaiting();
    }

    filePicker.remove();
  };

  document.body.appendChild(filePicker);
  filePicker.click();
}

export function uploadMultipleFiles(listId, inputClassName) {
  const filePicker = document.createElement('input');
  filePicker.type = 'file';
  filePicker.accept = '.jpg,.jpeg,.png,.gif,.webp';
  filePicker.multiple = true;
  filePicker.style.display = 'none';

  filePicker.onchange = async function() {
    const files = Array.from(filePicker.files);
    if (!files.length) return;

    pushWaiting('upload');
    try {
    // Create a row + upload for each file in parallel
    const uploads = files.map(async (file) => {
      // Add a new row
      const list = document.getElementById(listId);
      // Reuse first row if its input is empty
      let row, input, btn;
      const firstEmpty = list.querySelector('input.' + inputClassName + ':placeholder-shown');
      if (firstEmpty && !firstEmpty.value) {
        row = firstEmpty.closest('.url-input-row');
        input = firstEmpty;
        btn = row.querySelector('.upload-btn');
      } else {
        addUrlInput(listId, inputClassName);
        const rows = list.querySelectorAll('.url-input-row');
        row = rows[rows.length - 1];
        input = row.querySelector('input');
        btn = row.querySelector('.upload-btn');
      }

      input.value = file.name;
      btn.classList.add('uploading');
      btn.innerHTML = '&#8987;';

      try {
        const formData = new FormData();
        formData.append('file', file);
        const resp = await fetch(state.API_BASE + '/api/upload', { method: 'POST', headers: authHeaders(), body: formData });
        if (!resp.ok) {
          const err = await resp.json().catch(() => ({ detail: resp.statusText }));
          throw new Error(err.detail || 'Upload failed');
        }
        const data = await resp.json();
        input.value = data.url.startsWith('http') ? data.url : state.API_BASE + data.url;
        btn.classList.remove('uploading');
        btn.classList.add('done');
        btn.innerHTML = '&#10003;';
        setTimeout(() => { btn.classList.remove('done'); btn.innerHTML = '&#8682;'; updateRowButtons(row); }, 2000);
        updateRowButtons(row);
      } catch (e) {
        btn.classList.remove('uploading');
        btn.innerHTML = '&#8682;';
        input.value = '';
        input.placeholder = 'Upload failed: ' + e.message;
        updateRowButtons(row);
      }
    });

    await Promise.all(uploads);
    } finally {
      popWaiting();
    }
    filePicker.remove();
  };

  document.body.appendChild(filePicker);
  filePicker.click();
}

export function uploadMultipleAssets() {
  const filePicker = document.createElement('input');
  filePicker.type = 'file';
  filePicker.accept = '.jpg,.jpeg,.png,.gif,.webp,.mp4,.mov,.webm';
  filePicker.multiple = true;
  filePicker.style.display = 'none';

  filePicker.onchange = async function() {
    const files = Array.from(filePicker.files);
    if (!files.length) return;

    pushWaiting('upload');
    try {
    const uploads = files.map(async (file) => {
      const list = document.getElementById('assetList');
      let row, input, btn;
      const firstEmpty = list.querySelector('input.asset-url:placeholder-shown');
      if (firstEmpty && !firstEmpty.value) {
        row = firstEmpty.closest('.url-input-row');
        input = firstEmpty;
        btn = row.querySelector('.upload-btn');
      } else {
        addAssetInput();
        const rows = list.querySelectorAll('.url-input-row');
        row = rows[rows.length - 1];
        input = row.querySelector('input');
        btn = row.querySelector('.upload-btn');
      }

      input.value = file.name;
      btn.classList.add('uploading');
      btn.innerHTML = '&#8987;';

      try {
        const formData = new FormData();
        formData.append('file', file);
        const resp = await fetch(state.API_BASE + '/api/upload', { method: 'POST', headers: authHeaders(), body: formData });
        if (!resp.ok) {
          const err = await resp.json().catch(() => ({ detail: resp.statusText }));
          throw new Error(err.detail || 'Upload failed');
        }
        const data = await resp.json();
        input.value = data.url.startsWith('http') ? data.url : state.API_BASE + data.url;
        btn.classList.remove('uploading');
        btn.classList.add('done');
        btn.innerHTML = '&#10003;';
        setTimeout(() => { btn.classList.remove('done'); btn.innerHTML = '&#8682;'; updateRowButtons(row); }, 2000);
        updateRowButtons(row);
      } catch (e) {
        btn.classList.remove('uploading');
        btn.innerHTML = '&#8682;';
        input.value = '';
        input.placeholder = 'Upload failed: ' + e.message;
        updateRowButtons(row);
      }
    });

    await Promise.all(uploads);
    } finally {
      popWaiting();
    }
    filePicker.remove();
  };

  document.body.appendChild(filePicker);
  filePicker.click();
}

export function removeUrlRow(btnElement) {
  const row = btnElement.closest('.url-input-row');
  if (!row) return;
  const input = row.querySelector('input[type="url"]');
  const url = input ? input.value.trim() : '';
  // If the URL is an upload (GCS or local), delete it from the server
  if (url && (url.includes('storage.googleapis.com/automatiq/uploads/') || url.includes('/api/uploads/'))) {
    fetch(state.API_BASE + '/api/upload?url=' + encodeURIComponent(url), {
      method: 'DELETE',
      headers: authHeaders()
    }).catch(() => {});
  }
  row.remove();
}

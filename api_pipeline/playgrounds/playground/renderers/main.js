import { SVG_IN, SVG_OUT, stages } from '../data/stages.js';
import { STEP_INPUTS, STEP_OUTPUTS } from '../data/flow.js';
import { state } from '../state.js';
import { getVisibleStages } from './sidebar.js';

export function renderTags(items, cls) {
  if (!items || !items.length) return '<span class="none-text">None &mdash; all data from memory</span>';
  return items.map(c => `<span class="col-tag">${c}</span>`).join(' ');
}

export function renderMain() {
  const s = state.activeStage;
  const catColors = {
    'cat-parse': '#8957e5', 'cat-image': '#da3633', 'cat-analyze': '#d29922',
    'cat-vo': '#3fb950', 'cat-scene': '#1f6feb', 'cat-parallel': '#f78166',
    'cat-combine': '#bc8cff', 'cat-subtitle': '#39d353', 'cat-upload': '#f778ba'
  };
  const color = catColors[s.category] || '#8b949e';

  let html = `
    <div class="stage-header">
      <div class="big-num" style="background:${color};color:#fff;">${s.num}</div>
      <div>
        <h2>${s.title}</h2>
        <span class="svc-badge">${s.service}</span>
        ${s.optional ? '<span class="svc-badge" style="color:#d29922;border-color:#d2992244;">Optional</span>' : ''}
        ${s.isParallel ? '<span class="svc-badge" style="color:#f78166;border-color:#f7816644;">Parallel</span>' : ''}
      </div>
    </div>
    <div class="stage-description">${s.description}</div>
    <div class="io-section">
      <div class="io-box input">
        <h3>${SVG_IN} Inputs</h3>
        <ul>${s.inputs.map((inp, idx) => {
          const flows = (STEP_INPUTS[String(s.id)] || []).filter(f => f.idx === idx);
          const badges = flows.map(f => {
            if (f.source === 'user') return '<span class="flow-tag flow-user">user input</span>';
            const src = stages.find(st => String(st.id) === String(f.source));
            return src ? '<span class="flow-tag flow-from" onclick="event.stopPropagation();selectStage(\'' + f.source + '\')">\u2190 Step ' + src.num + '</span>' : '';
          }).join('');
          return '<li>' + inp + ' ' + badges + '</li>';
        }).join('')}</ul>
      </div>
      <div class="io-box output">
        <h3>${SVG_OUT} Outputs</h3>
        <ul>${s.outputs.map((out, idx) => {
          const flows = (STEP_OUTPUTS[String(s.id)] || []).filter(f => f.idx === idx);
          const badges = flows.flatMap(f => {
            if (!f.targets.length) return [];
            return f.targets.map(t => {
              const tgt = stages.find(st => String(st.id) === String(t));
              return tgt ? '<span class="flow-tag flow-to" onclick="event.stopPropagation();selectStage(\'' + t + '\')">\u2192 Step ' + tgt.num + '</span>' : '';
            });
          }).join('');
          return '<li>' + out + ' ' + badges + '</li>';
        }).join('')}</ul>
      </div>
    </div>
  `;

  // Implementation comparison with INPUT / OUTPUT sub-sections
  if (s.sheets && s.supabase) {
    html += `<div class="impl-section">
      <div class="impl-box sheets">
        <h3><span class="impl-icon">S</span> Google Sheets Implementation</h3>
        <div class="impl-sub">
          <div class="impl-sub-title input-title">${SVG_IN} How Inputs Are Read</div>
          <div class="impl-label">Reads from columns</div>
          <div class="impl-row">${renderTags(s.sheets.input.columns, 'sheets')}</div>
          <div class="impl-label">How</div>
          <div class="impl-row">${s.sheets.input.how}</div>
        </div>
        <div class="impl-sub">
          <div class="impl-sub-title output-title">${SVG_OUT} How Outputs Are Written</div>
          <div class="impl-label">Writes to columns</div>
          <div class="impl-row">${renderTags(s.sheets.output.columns, 'sheets')}</div>
          <div class="impl-label">How</div>
          <div class="impl-row">${s.sheets.output.how}</div>
        </div>
      </div>
      <div class="impl-box supabase">
        <h3><span class="impl-icon">A</span> Supabase API Implementation</h3>
        <div class="impl-sub">
          <div class="impl-sub-title input-title">${SVG_IN} How Inputs Are Read</div>
          <div class="impl-label">Reads from job</div>
          <div class="impl-row">${renderTags(s.supabase.input.fields, 'supabase')}</div>
          <div class="impl-label">How</div>
          <div class="impl-row">${s.supabase.input.how}</div>
        </div>
        <div class="impl-sub">
          <div class="impl-sub-title output-title">${SVG_OUT} How Outputs Are Written</div>
          <div class="impl-label">Saves to job</div>
          <div class="impl-row">${renderTags(s.supabase.output.fields, 'supabase')}</div>
          <div class="impl-label">How</div>
          <div class="impl-row">${s.supabase.output.how}</div>
          <div class="impl-label">Progress</div>
          <div class="impl-row"><span class="progress-tag">${s.supabase.progress}</span></div>
        </div>
      </div>
    </div>`;
  } else if (s.supabase) {
    html += `<div class="impl-section impl-section-single">
      <div class="impl-box supabase" style="grid-column:1/-1;">
        <h3><span class="impl-icon">A</span> Supabase API Implementation <span class="svc-badge" style="color:#d29922;border-color:#d2992244;margin-left:8px;">API Only</span></h3>
        <div class="impl-sub">
          <div class="impl-sub-title input-title">${SVG_IN} How Inputs Are Read</div>
          <div class="impl-label">Reads from job</div>
          <div class="impl-row">${renderTags(s.supabase.input.fields, 'supabase')}</div>
          <div class="impl-label">How</div>
          <div class="impl-row">${s.supabase.input.how}</div>
        </div>
        <div class="impl-sub">
          <div class="impl-sub-title output-title">${SVG_OUT} How Outputs Are Written</div>
          <div class="impl-label">Saves to job</div>
          <div class="impl-row">${renderTags(s.supabase.output.fields, 'supabase')}</div>
          <div class="impl-label">How</div>
          <div class="impl-row">${s.supabase.output.how}</div>
          <div class="impl-label">Progress</div>
          <div class="impl-row"><span class="progress-tag">${s.supabase.progress}</span></div>
        </div>
      </div>
    </div>`;
  }

  if (s.example) {
    html += `<div class="example-box">
      <h3><svg width="14" height="14" viewBox="0 0 16 16" fill="#d29922"><path d="M0 1.75C0 .784.784 0 1.75 0h12.5C15.216 0 16 .784 16 1.75v12.5A1.75 1.75 0 0114.25 16H1.75A1.75 1.75 0 010 14.25zm1.75-.25a.25.25 0 00-.25.25v12.5c0 .138.112.25.25.25h12.5a.25.25 0 00.25-.25V1.75a.25.25 0 00-.25-.25zM5 6.25a.75.75 0 01.75-.75h4.5a.75.75 0 010 1.5h-4.5A.75.75 0 015 6.25zm0 4a.75.75 0 01.75-.75h4.5a.75.75 0 010 1.5h-4.5A.75.75 0 015 10.25z"/></svg> Example</h3>
      <pre>${s.example}</pre>
    </div>`;
  }

  if (s.notes) {
    html += `<div class="detail-note">${s.notes}</div>`;
  }

  // Navigation
  const visible = getVisibleStages();
  const idx = visible.findIndex(st => st.id === s.id);
  const prev = idx > 0 ? visible[idx - 1] : null;
  const next = idx < visible.length - 1 ? visible[idx + 1] : null;

  html += `<div style="display:flex;justify-content:space-between;margin-top:24px;">`;
  if (prev) html += `<button onclick="selectStage('${prev.id}')" style="padding:8px 16px;background:#21262d;border:1px solid #30363d;color:#c9d1d9;border-radius:8px;cursor:pointer;font-size:13px;">&larr; Step ${prev.num}: ${prev.title}</button>`;
  else html += `<div></div>`;
  if (next) html += `<button onclick="selectStage('${next.id}')" style="padding:8px 16px;background:#21262d;border:1px solid #30363d;color:#c9d1d9;border-radius:8px;cursor:pointer;font-size:13px;">Step ${next.num}: ${next.title} &rarr;</button>`;
  html += `</div>`;

  document.getElementById('mainContent').innerHTML = html;
  document.getElementById('mainContent').scrollTop = 0;
}

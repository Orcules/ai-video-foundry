import { stages } from '../data/stages.js';
import { PRESETS } from '../data/flow.js';
import { state } from '../state.js';

export function getVisibleStages() {
  if (PRESETS[state.activePreset]) return stages.filter(s => PRESETS[state.activePreset].includes(s.id));
  return stages;
}

export function renderSidebar() {
  const list = document.getElementById('stageList');
  const visible = getVisibleStages();
  list.innerHTML = visible.map(s => `
    <li class="stage-item ${s.category} ${s.id === state.activeStage.id ? 'active' : ''}" onclick="selectStage('${s.id}')">
      <span style="display:flex;align-items:center;">
        <span class="stage-num">${s.num}</span>
        <span class="stage-title">${s.title}${s.optional ? ' <span style="color:#6e7681;font-size:10px;">(opt)</span>' : ''}${s.isParallel ? ' <span class="parallel-badge">PARALLEL</span>' : ''}</span>
      </span>
      <div class="stage-service">${s.service}</div>
      <div class="connector"></div>
    </li>
  `).join('');
}

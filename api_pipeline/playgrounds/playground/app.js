import { stages, switchPipeline } from './data/stages.js';
import { state } from './state.js';
import { renderSidebar, getVisibleStages } from './renderers/sidebar.js';
import { renderMain } from './renderers/main.js';
import { renderFlowDiagram } from './renderers/diagram.js';
import { updatePrompt } from './renderers/prompt.js';

const pipelineTitles = { product: 'TVD X1 Product Video Pipeline', influencer: 'TVD X1 Influencer Video Pipeline', 'personal-brand': 'TVD X1 Personal-Brand Video Pipeline' };

function selectPipeline(name) {
  state.activePipeline = name;
  switchPipeline(name);
  state.activeStage = stages[0];
  state.activePreset = 'all';
  if (state.diagramMode) {
    state.diagramMode = false;
    document.querySelector('.layout').classList.remove('diagram-mode');
    document.getElementById('diagramBtn').classList.remove('active');
  }
  document.getElementById('pipelineTitle').textContent = pipelineTitles[name] || pipelineTitles.product;
  document.querySelectorAll('.pipeline-btn').forEach(b => b.classList.toggle('active', b.dataset.pipeline === name));
  document.querySelectorAll('.preset-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.preset-btn')[0].classList.add('active');
  renderSidebar(); renderMain(); updatePrompt();
}

function selectStage(id) {
  const found = stages.find(s => String(s.id) === String(id));
  if (found) {
    if (state.diagramMode) {
      state.diagramMode = false;
      document.querySelector('.layout').classList.remove('diagram-mode');
      document.getElementById('diagramBtn').classList.remove('active');
      document.querySelectorAll('.preset-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.preset-btn')[0].classList.add('active');
      state.activePreset = 'all';
    }
    state.activeStage = found;
    renderSidebar(); renderMain(); updatePrompt();
  }
}

function showPreset(preset) {
  state.activePreset = preset;
  state.diagramMode = false;
  document.querySelector('.layout').classList.remove('diagram-mode');
  document.getElementById('diagramBtn').classList.remove('active');
  document.querySelectorAll('.preset-btn').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  const visible = getVisibleStages();
  if (!visible.find(s => s.id === state.activeStage.id)) state.activeStage = visible[0];
  renderSidebar(); renderMain(); updatePrompt();
}

function toggleFlowDiagram() {
  state.diagramMode = !state.diagramMode;
  const layout = document.querySelector('.layout');
  const btn = document.getElementById('diagramBtn');
  if (state.diagramMode) {
    layout.classList.add('diagram-mode');
    document.querySelectorAll('.preset-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    renderFlowDiagram();
    document.getElementById('promptOutput').textContent = 'Flow Diagram mode \u2014 click any node to view its details. Dashed borders = optional steps.';
  } else {
    layout.classList.remove('diagram-mode');
    btn.classList.remove('active');
    document.querySelectorAll('.preset-btn')[0].classList.add('active');
    state.activePreset = 'all';
    renderSidebar(); renderMain(); updatePrompt();
  }
}

function copyPrompt() {
  navigator.clipboard.writeText(document.getElementById('promptOutput').textContent).then(() => {
    const btn = document.getElementById('copyBtn');
    btn.textContent = 'Copied!'; btn.classList.add('copied');
    setTimeout(() => { btn.textContent = 'Copy'; btn.classList.remove('copied'); }, 2000);
  });
}

// Expose to inline onclick handlers
window.selectStage = selectStage;
window.showPreset = showPreset;
window.toggleFlowDiagram = toggleFlowDiagram;
window.copyPrompt = copyPrompt;
window.selectPipeline = selectPipeline;

// Keyboard navigation
document.addEventListener('keydown', (e) => {
  const visible = getVisibleStages();
  const idx = visible.findIndex(s => s.id === state.activeStage.id);
  if ((e.key === 'ArrowDown' || e.key === 'ArrowRight') && idx < visible.length - 1) { selectStage(String(visible[idx + 1].id)); e.preventDefault(); }
  else if ((e.key === 'ArrowUp' || e.key === 'ArrowLeft') && idx > 0) { selectStage(String(visible[idx - 1].id)); e.preventDefault(); }
});

// Init
renderSidebar(); renderMain(); updatePrompt();

export function onSimulationToggle() {
  const sim = document.getElementById('simulationMode').checked;
  document.getElementById('simOptionsRow').style.display = sim ? 'block' : 'none';
  const toggle = document.getElementById('simToggle');
  const startBtn = document.getElementById('startBtn');
  if (sim) {
    toggle.classList.add('active');
    onSimTypeChange(); // update button text/color based on sim type
  } else {
    toggle.classList.remove('active');
    startBtn.textContent = 'Start Pipeline';
    startBtn.style.background = '';
  }
}

export function onSimTypeChange() {
  const isMonolith = document.getElementById('simTypeMonolith').checked;
  const startBtn = document.getElementById('startBtn');
  const hint = document.getElementById('simTypeHint');
  const wrapperLabel = document.getElementById('simTypeWrapperLabel');
  const monolithLabel = document.getElementById('simTypeMonolithLabel');
  if (isMonolith) {
    startBtn.textContent = 'Start End-to-End Simulation';
    startBtn.style.background = '#f97316';
    hint.textContent = 'Runs the real monolith pipeline code via the wrapper with mock API calls. Requires tvd_pipeline installed on the server. Tests real pipeline logic, control flow, and wrapper integration.';
    monolithLabel.style.borderColor = '#f97316';
    monolithLabel.style.background = 'rgba(249,115,22,0.1)';
    wrapperLabel.style.borderColor = '';
    wrapperLabel.style.background = '';
  } else {
    startBtn.textContent = 'Start Wrapper Simulation';
    startBtn.style.background = '#a855f7';
    hint.textContent = 'Mock services in the wrapper layer. Monolith is not touched. Fastest option for dashboard/SSE testing.';
    wrapperLabel.style.borderColor = '#a855f7';
    wrapperLabel.style.background = 'rgba(168,85,247,0.1)';
    monolithLabel.style.borderColor = '';
    monolithLabel.style.background = '';
  }
}

export function onSimDurationChange() {
  const sel = document.getElementById('simDuration');
  const customInput = document.getElementById('simDurationCustom');
  customInput.style.display = sel.value === 'custom' ? '' : 'none';
}

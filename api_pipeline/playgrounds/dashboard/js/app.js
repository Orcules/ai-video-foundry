/**
 * Entry point: imports all modules, assigns window.* for onclick handlers, calls init.
 */

// -- Import all modules --
import { initServer, onServerSelect, onCustomServer, onApiKeyChange, loadAnimationConfig, updateVideoProviderOptions, updateImageProviderOptions } from './config.js';
import { onSimulationToggle, onSimTypeChange, onSimDurationChange } from './simulation.js';
import { updatePipelineVisibility, addUrlInput, addAssetInput, onAssetModeChange, toggleBeatSyncStrategy, updatePromptCounter, initVideoTypeListener, initRowButtonStates, updateStyleDefault, updateTierDefaults } from './form.js';
import { uploadFile, uploadMultipleFiles, uploadMultipleAssets, removeUrlRow } from './upload.js';
import { toggleSection, copyJobId, escapeHtml } from './ui.js';
import { openLightbox, closeLightbox } from './lightbox.js';
import { startJob, pauseJob, abortJob, resumeJob, restartJob } from './job.js';
import { monitorJob } from './monitor.js';
import { refreshActiveJobs, selectActiveJob } from './active-jobs.js';

// Import side-effect modules (they self-register listeners)
import './output.js';

// -- Assign to window for inline onclick handlers --
window.startJob = startJob;
window.monitorJob = monitorJob;
window.refreshActiveJobs = refreshActiveJobs;
window.selectActiveJob = selectActiveJob;
window.pauseJob = pauseJob;
window.abortJob = abortJob;
window.resumeJob = resumeJob;
window.restartJob = restartJob;

window.onServerSelect = onServerSelect;
window.onCustomServer = onCustomServer;
window.onApiKeyChange = onApiKeyChange;

window.onSimulationToggle = onSimulationToggle;
window.onSimTypeChange = onSimTypeChange;
window.onSimDurationChange = onSimDurationChange;

window.uploadFile = uploadFile;
window.uploadMultipleFiles = uploadMultipleFiles;
window.uploadMultipleAssets = uploadMultipleAssets;
window.removeUrlRow = removeUrlRow;

window.addUrlInput = addUrlInput;
window.addAssetInput = addAssetInput;
window.onAssetModeChange = onAssetModeChange;
window.toggleBeatSyncStrategy = toggleBeatSyncStrategy;
window.updatePromptCounter = updatePromptCounter;

window.onAnimationModelChange = () => { updateVideoProviderOptions(); updateTierDefaults(); };
window.onImageModelChange = () => { updateImageProviderOptions(); updateTierDefaults(); };
window.updateTierDefaults = updateTierDefaults;
window.toggleSection = toggleSection;
window.copyJobId = copyJobId;

window.openLightbox = openLightbox;
window.closeLightbox = closeLightbox;

// -- Initialize --
initVideoTypeListener();
initServer();
loadAnimationConfig();
updatePipelineVisibility(document.getElementById('videoType').value);
updateStyleDefault(document.getElementById('videoType').value);
toggleBeatSyncStrategy();
initRowButtonStates();

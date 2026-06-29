import { stages } from './data/stages.js';

export const state = {
  activeStage: stages[0],
  activePreset: 'all',
  diagramMode: false,
  activePipeline: 'product',
};

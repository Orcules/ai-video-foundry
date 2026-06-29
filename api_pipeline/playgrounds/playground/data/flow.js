import * as pf from './product-flow.js';
import * as uf from './ugc-flow.js';
import * as pbf from './personal_brand-flow.js';

const FLOWS = { product: pf, influencer: uf, 'personal-brand': pbf };

export const NW = pf.NW, NH = pf.NH;
export let STEP_INPUTS = pf.STEP_INPUTS;
export let STEP_OUTPUTS = pf.STEP_OUTPUTS;
export let DIAGRAM_NODES = pf.DIAGRAM_NODES;
export let DIAGRAM_EDGES = pf.DIAGRAM_EDGES;
export let USER_EDGES = pf.USER_EDGES;
export let PRESETS = pf.PRESETS;

export function setPipelineFlow(name) {
  const src = FLOWS[name] || pf;
  STEP_INPUTS = src.STEP_INPUTS;
  STEP_OUTPUTS = src.STEP_OUTPUTS;
  DIAGRAM_NODES = src.DIAGRAM_NODES;
  DIAGRAM_EDGES = src.DIAGRAM_EDGES;
  USER_EDGES = src.USER_EDGES;
  PRESETS = src.PRESETS;
}

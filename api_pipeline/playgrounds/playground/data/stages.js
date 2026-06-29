import { stages as productStages, SVG_IN, SVG_OUT } from './product-stages.js';
import { stages as ugcStages } from './ugc-stages.js';
import { stages as personalBrandStages } from './personal_brand-stages.js';
import { setPipelineFlow } from './flow.js';

export { SVG_IN, SVG_OUT };
export let stages = productStages;

const PIPELINES = { product: productStages, influencer: ugcStages, 'personal-brand': personalBrandStages };

export function switchPipeline(name) {
  stages = PIPELINES[name] || productStages;
  setPipelineFlow(name);
}

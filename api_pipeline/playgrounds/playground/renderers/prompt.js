import { stages } from '../data/stages.js';
import { state } from '../state.js';

const pipelineNames = { product: 'Product', influencer: 'Influencer', 'personal-brand': 'Personal-Brand' };

export function updatePrompt() {
  const s = state.activeStage;
  const plName = pipelineNames[state.activePipeline] || 'Product';
  let prompt = `TVD X1 ${plName} Video Pipeline \u2014 Step ${s.num}: "${s.title}" [${s.service}]\n\n`;

  prompt += `PIPELINE OVERVIEW (${stages.length} stages):\n`;
  stages.forEach(st => {
    const marker = st.id === s.id ? '>>>' : '   ';
    prompt += `${marker} Step ${st.num}: ${st.title} [${st.service}]${st.optional ? ' (optional)' : ''}\n`;
  });

  prompt += `\n--- SELECTED STAGE ---\n`;
  prompt += `Step ${s.num}: ${s.title}\n`;
  prompt += `Service: ${s.service}\n`;
  prompt += `Inputs: ${s.inputs.map(i => i.replace(/<[^>]+>/g, '')).join('; ')}\n`;
  prompt += `Outputs: ${s.outputs.map(o => o.replace(/<[^>]+>/g, '')).join('; ')}\n`;

  if (s.sheets) {
    prompt += `\nSHEETS INPUT:  reads [${s.sheets.input.columns.join(', ') || 'memory only'}]`;
    prompt += `\nSHEETS OUTPUT: writes [${s.sheets.output.columns.join(', ') || 'memory only'}]`;
  }
  if (s.supabase) {
    prompt += `\nAPI INPUT:  reads [${s.supabase.input.fields.join(', ') || 'memory only'}]`;
    prompt += `\nAPI OUTPUT: writes [${s.supabase.output.fields.join(', ') || 'memory only'}] (progress: ${s.supabase.progress})`;
  }

  document.getElementById('promptOutput').textContent = prompt;
}

/**
 * Shared mutable state object — imported by all modules.
 */
export const state = {
  API_BASE: window.location.origin,
  API_KEY: 'your-internal-token',
  currentJobId: null,
  eventSource: null,
  eventCount: 0,
  supabaseClient: null,
  muxRealtimeChannel: null,
  _fetchingFinalOutput: false,
  muxPollInterval: null,
  costPollInterval: null,
  queuePollTimer: null,

  // Resolution tier config fetched from /api/config (null until loaded).
  tiersConfig: null,

  // Animation model config fetched from /api/config (fallback hardcoded).
  // NOTE: This fallback is overridden by /api/config on startup when the server is reachable.
  // "Auto" label is updated dynamically by updateTierDefaults() once tier data loads.
  animationModelsConfig: {
    product: [{ value: 'auto', label: 'Auto' }, { value: 'google', label: 'Veo 3.1 Fast (Vertex AI)' }, { value: 'kling', label: 'Kling 2.5 (Kie.ai)' }, { value: 'runway', label: 'Runway Gen4 Turbo (Kie.ai)' }, { value: 'none', label: 'None' }],
    influencer: [{ value: 'auto', label: 'Auto' }, { value: 'google', label: 'Veo 3.1 Fast (Vertex AI)' }, { value: 'kling', label: 'Kling 2.5 (Kie.ai)' }, { value: 'runway', label: 'Runway Gen4 Turbo (Kie.ai)' }, { value: 'none', label: 'None' }],
    personal_brand: [{ value: 'auto', label: 'Auto' }, { value: 'google', label: 'Veo 3.1 Fast (Vertex AI)' }, { value: 'kling', label: 'Kling 2.5 (Kie.ai)' }, { value: 'runway', label: 'Runway Gen4 Turbo (Kie.ai)' }, { value: 'none', label: 'None' }],
  },
};

// Flow dependency maps: which step each input comes from, which steps each output feeds
export const STEP_INPUTS = {
  '0': [{ idx: 0, source: 'user' }, { idx: 1, source: 'user' }],
  '1': [{ idx: 0, source: 'user' }, { idx: 1, source: 'user' }],
  '2': [{ idx: 0, source: 'user' }, { idx: 1, source: 1 }],
  '2.5': [{ idx: 0, source: 'user' }],
  '2.7': [{ idx: 0, source: 1 }, { idx: 1, source: 'user' }, { idx: 2, source: 'user' }, { idx: 3, source: 'user' }],
  '3': [{ idx: 0, source: 1 }, { idx: 1, source: '2.7' }, { idx: 2, source: 'user' }, { idx: 3, source: 0 }, { idx: 4, source: '2.5' }, { idx: 5, source: 'user' }],
  '4-7': [{ idx: 0, source: 3 }, { idx: 1, source: 2 }, { idx: 2, source: 'user' }, { idx: 3, source: 'user' }, { idx: 4, source: 3 }, { idx: 5, source: 'user' }, { idx: 6, source: 'user' }],
  '7.5': [{ idx: 0, source: '4-7' }, { idx: 1, source: 3 }, { idx: 2, source: 3 }],
  '8': [{ idx: 0, source: '7.5' }, { idx: 1, source: 3 }, { idx: 2, source: '2.7' }, { idx: 3, source: '4-7' }],
  '9': [{ idx: 0, source: 8 }, { idx: 1, source: 'user' }, { idx: 2, source: '2.7' }],
  '10': [{ idx: 0, source: 9 }, { idx: 1, source: '2.7' }, { idx: 2, source: '4-7' }],
};

export const STEP_OUTPUTS = {
  '0': [{ idx: 0, targets: [3] }],
  '1': [{ idx: 0, targets: [2, '2.7', 3] }, { idx: 1, targets: ['2.7', 3] }, { idx: 2, targets: ['2.7', 3] }, { idx: 3, targets: [3] }],
  '2': [{ idx: 0, targets: ['4-7'] }],
  '2.5': [{ idx: 0, targets: [3] }],
  '2.7': [{ idx: 0, targets: [] }, { idx: 1, targets: [8, 10] }, { idx: 2, targets: [3, 9] }, { idx: 3, targets: [8] }],
  '3': [{ idx: 0, targets: ['4-7', '7.5', 8] }, { idx: 1, targets: ['4-7'] }],
  '4-7': [{ idx: 0, targets: [] }, { idx: 1, targets: ['7.5', 8] }, { idx: 2, targets: [8, 10] }],
  '7.5': [{ idx: 0, targets: [8] }],
  '8': [{ idx: 0, targets: [] }, { idx: 1, targets: [9, 10] }],
  '9': [{ idx: 0, targets: [10] }],
  '10': [{ idx: 0, targets: [] }],
};

// Flow diagram layout
export const NW = 170, NH = 44;

export const DIAGRAM_NODES = [
  { id: '0', x: 230, y: 50, color: '#d29922' },
  { id: '1', x: 600, y: 50, color: '#8957e5' },
  { id: '2', x: 170, y: 160, color: '#da3633' },
  { id: '2.5', x: 430, y: 160, color: '#d29922' },
  { id: '2.7', x: 730, y: 160, color: '#3fb950' },
  { id: '3', x: 440, y: 275, color: '#1f6feb' },
  { id: '4-7', x: 440, y: 375, color: '#f78166' },
  { id: '7.5', x: 440, y: 470, color: '#bc8cff' },
  { id: '8', x: 440, y: 555, color: '#bc8cff' },
  { id: '9', x: 440, y: 640, color: '#39d353' },
  { id: '10', x: 440, y: 730, color: '#f778ba' },
];

export const DIAGRAM_EDGES = [
  { from: '0', to: '3', label: 'character' },
  { from: '1', to: '2', label: 'text_1' },
  { from: '1', to: '2.7', label: 'text_1_2_3' },
  { from: '1', to: '3', label: 'text_1_2_3_4' },
  { from: '2', to: '4-7', label: 'clean product' },
  { from: '2.5', to: '3', label: 'structure' },
  { from: '2.7', to: '3', label: 'VO timing' },
  { from: '2.7', to: '8', label: 'VO audio' },
  { from: '2.7', to: '9', label: 'word segs' },
  { from: '2.7', to: '10', label: 'VO URL' },
  { from: '3', to: '4-7', label: 'scenes + style' },
  { from: '3', to: '7.5', label: 'exact durations' },
  { from: '4-7', to: '7.5', label: 'scene videos' },
  { from: '4-7', to: '8', label: 'music' },
  { from: '4-7', to: '10', label: 'music URL' },
  { from: '7.5', to: '8', label: 'trimmed videos' },
  { from: '8', to: '9', label: 'video' },
  { from: '8', to: '10', label: 'video (no subs)' },
  { from: '9', to: '10', label: 'subtitled' },
];

// User input arrows: short local arrows entering from the left of each node
export const USER_EDGES = [
  { to: '0', label: 'char_url, description' },
  { to: '1', label: 'prompt, images' },
  { to: '2', label: 'product images' },
  { to: '2.5', label: 'video reference' },
  { to: '2.7', label: 'duration, lang, voice' },
  { to: '3', label: 'duration, logo, slogan' },
  { to: '4-7', label: 'style, model, quality' },
  { to: '7.5', label: 'sound_sync_method' },
  { to: '9', label: 'subtitles, lang' },
];

// Preset filters
export const PRESETS = {
  generation: [0, 1, 2, "2.5", "2.7", 3, "4-7", "7.5"],
  assembly: ["7.5", 8, 9, 10],
  quick: [1, "2.7", 3, "4-7", "7.5", 8, 9, 10],
};

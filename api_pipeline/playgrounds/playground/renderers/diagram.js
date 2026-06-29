import { stages } from '../data/stages.js';
import { NW, NH, DIAGRAM_NODES, DIAGRAM_EDGES, USER_EDGES } from '../data/flow.js';

function getNodeById(id) { return DIAGRAM_NODES.find(n => n.id === id); }

function edgePath(fromId, toId) {
  const f = getNodeById(fromId), t = getNodeById(toId);
  const x1 = f.x, y1 = f.y + NH / 2;
  const x2 = t.x, y2 = t.y - NH / 2;
  const dy = y2 - y1, dx = x2 - x1;
  // Short vertical: straight line
  if (Math.abs(dx) < 10 && dy < 115) {
    return { path: `M${x1},${y1} L${x2},${y2}`, mx: x1 + 12, my: (y1 + y2) / 2 };
  }
  // Long vertical (skips nodes): bulge left to avoid overlapping nodes
  if (Math.abs(dx) < 10 && dy >= 115) {
    const bulge = -70 - dy / 8;
    const cx = x1 + bulge;
    return {
      path: `M${x1},${y1} C${cx},${y1 + dy * 0.3} ${cx},${y2 - dy * 0.3} ${x2},${y2}`,
      mx: cx - 5, my: (y1 + y2) / 2
    };
  }
  // Standard diagonal bezier
  return {
    path: `M${x1},${y1} C${x1},${y1 + dy * 0.4} ${x2},${y2 - dy * 0.4} ${x2},${y2}`,
    mx: (x1 + x2) / 2, my: (y1 + y2) / 2
  };
}

export function renderFlowDiagram() {
  const content = document.getElementById('mainContent');
  const optIds = new Set(stages.filter(s => s.optional).map(s => String(s.id)));
  const maxY = Math.max(...DIAGRAM_NODES.map(n => n.y));
  const svgH = maxY + NH / 2 + 60;
  let svg = `<div class="flow-diagram"><svg viewBox="0 0 960 ${svgH}" preserveAspectRatio="xMidYMid meet">`;
  svg += `<defs>`;
  svg += `<marker id="arrowhead" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#484f58"/></marker>`;
  svg += `<marker id="arrowhead-user" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#d29922"/></marker>`;
  svg += `</defs>`;
  // Step-to-step edges (draw first so nodes render on top)
  DIAGRAM_EDGES.forEach(e => {
    const { path, mx, my } = edgePath(e.from, e.to);
    svg += `<path d="${path}" fill="none" stroke="#484f58" stroke-width="1.5" marker-end="url(#arrowhead)" opacity="0.6"/>`;
    svg += `<text x="${mx}" y="${my - 4}" text-anchor="middle" fill="#6e7681" font-size="9" font-family="-apple-system,system-ui,sans-serif">${e.label}</text>`;
  });
  // User input edges: short arrows entering from the left of each target node
  const ARROW_LEN = 75;
  USER_EDGES.forEach(ue => {
    const t = getNodeById(ue.to);
    const nodeLeft = t.x - NW / 2;
    const startX = nodeLeft - ARROW_LEN;
    const endX = nodeLeft - 2;
    const y = t.y;
    // Amber dot at arrow start
    svg += `<circle cx="${startX}" cy="${y}" r="3" fill="#d29922" opacity="0.6"/>`;
    // Dashed arrow line
    svg += `<line x1="${startX + 4}" y1="${y}" x2="${endX}" y2="${y}" stroke="#d29922" stroke-width="1.3" stroke-dasharray="5,3" marker-end="url(#arrowhead-user)" opacity="0.55"/>`;
    // Label above the arrow
    svg += `<text x="${startX}" y="${y - 7}" text-anchor="start" fill="#d29922" font-size="8.5" font-family="-apple-system,system-ui,sans-serif" opacity="0.8">${ue.label}</text>`;
  });
  // Nodes
  DIAGRAM_NODES.forEach(n => {
    const stg = stages.find(s => String(s.id) === n.id);
    const title = stg ? stg.title : n.id;
    const shortTitle = title.length > 22 ? title.substring(0, 20) + '..' : title;
    const isOpt = optIds.has(n.id);
    svg += `<g class="flow-node" onclick="selectStage('${n.id}')" style="cursor:pointer">`;
    svg += `<rect x="${n.x - NW / 2}" y="${n.y - NH / 2}" width="${NW}" height="${NH}" rx="8" fill="${n.color}18" stroke="${n.color}" stroke-width="1.5" ${isOpt ? 'stroke-dasharray="5,3"' : ''}/>`;
    svg += `<text x="${n.x}" y="${n.y - 5}" text-anchor="middle" fill="${n.color}" font-size="10" font-weight="700" font-family="-apple-system,system-ui,sans-serif">${n.id}</text>`;
    svg += `<text x="${n.x}" y="${n.y + 10}" text-anchor="middle" fill="#c9d1d9" font-size="10" font-family="-apple-system,system-ui,sans-serif">${shortTitle}</text>`;
    svg += `</g>`;
  });
  // Bottom labels
  const bottomY = maxY + NH / 2 + 30;
  svg += `<text x="480" y="${bottomY}" text-anchor="middle" fill="#f778ba" font-size="10" font-weight="600" font-family="-apple-system,system-ui,sans-serif">Final Video URL (permanent GCS link)</text>`;
  // Legend (bottom-right)
  const lx = 760, ly = bottomY - 42;
  svg += `<line x1="${lx}" y1="${ly + 4}" x2="${lx + 35}" y2="${ly + 4}" stroke="#484f58" stroke-width="1.5" marker-end="url(#arrowhead)" opacity="0.6"/>`;
  svg += `<text x="${lx + 40}" y="${ly + 7}" fill="#6e7681" font-size="9" font-family="-apple-system,system-ui,sans-serif">step data</text>`;
  svg += `<circle cx="${lx}" cy="${ly + 18}" r="3" fill="#d29922" opacity="0.6"/>`;
  svg += `<line x1="${lx + 4}" y1="${ly + 18}" x2="${lx + 35}" y2="${ly + 18}" stroke="#d29922" stroke-width="1.3" stroke-dasharray="5,3" marker-end="url(#arrowhead-user)" opacity="0.55"/>`;
  svg += `<text x="${lx + 40}" y="${ly + 21}" fill="#d29922" font-size="9" font-family="-apple-system,system-ui,sans-serif" opacity="0.8">user input</text>`;
  svg += `<rect x="${lx}" y="${ly + 26}" width="30" height="12" rx="4" fill="none" stroke="#6e7681" stroke-width="1" stroke-dasharray="5,3"/>`;
  svg += `<text x="${lx + 40}" y="${ly + 35}" fill="#6e7681" font-size="9" font-style="italic" font-family="-apple-system,system-ui,sans-serif">optional step</text>`;
  svg += `</svg></div>`;
  content.innerHTML = svg;
  content.scrollTop = 0;
}

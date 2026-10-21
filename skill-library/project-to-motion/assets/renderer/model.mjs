// Pure data and timeline functions shared by the browser and the render command.
export const SCENE_KINDS = ['hook', 'pipeline', 'routing', 'parallel', 'feedback', 'compare', 'outcome'];

const fail = (message) => { throw new Error(message); };
const text = (value, name, max, optional = false) => {
  if (optional && value === undefined) return;
  if (typeof value !== 'string' || (!optional && !value.trim()) || Array.from(value).length > max || /[\u0000-\u0008\u000b\u000c\u000e-\u001f]/u.test(value)) {
    fail(`${name} must be ${optional ? 'a' : 'a nonempty'} string of at most ${max} characters`);
  }
};

export function validateSpec(spec) {
  if (!spec || typeof spec !== 'object' || Array.isArray(spec)) fail('Spec must be an object');
  if (spec.schema_version !== 1) fail('schema_version must be 1');
  if (!['landscape', 'portrait'].includes(spec.format)) fail('format must be landscape or portrait');
  if (!['paper', 'midnight'].includes(spec.theme)) fail('theme must be paper or midnight');
  if (spec.fps !== 30) fail('fps must be 30');
  if (spec.language !== undefined && !['en', 'zh'].includes(spec.language)) fail('language must be en or zh');
  text(spec.title, 'title', 70);
  text(spec.audience, 'audience', 240);
  text(spec.goal, 'goal', 500);
  if (!Array.isArray(spec.entities) || spec.entities.length > 30) fail('entities must contain at most 30 objects');
  const ids = new Set();
  for (const entity of spec.entities) {
    text(entity?.id, 'entity.id', 64);
    if (!/^[a-z][a-z0-9-]*$/.test(entity.id) || ids.has(entity.id)) fail('Entity IDs must be unique lowercase slugs');
    ids.add(entity.id);
    text(entity.label, `${entity.id}.label`, 30);
    text(entity.detail, `${entity.id}.detail`, 90, true);
  }
  if (!Array.isArray(spec.scenes) || spec.scenes.length < 3 || spec.scenes.length > 8) fail('Use 3–8 scenes');
  const sceneIds = new Set();
  for (const scene of spec.scenes) {
    text(scene?.id, 'scene.id', 64);
    if (!/^[a-z][a-z0-9-]*$/.test(scene.id) || sceneIds.has(scene.id)) fail('Scene IDs must be unique lowercase slugs');
    sceneIds.add(scene.id);
    if (!SCENE_KINDS.includes(scene.kind)) fail(`Unsupported scene kind: ${scene.kind}`);
    if (!Number.isFinite(scene.duration) || scene.duration < 3 || scene.duration > 18 || Math.abs(scene.duration * spec.fps - Math.round(scene.duration * spec.fps)) > 1e-6) fail(`${scene.id}.duration must be 3–18 seconds on an exact frame boundary`);
    text(scene.title, `${scene.id}.title`, 70);
    text(scene.caption, `${scene.id}.caption`, 150);
    text(scene.narration, `${scene.id}.narration`, 500, true);
    if (!Array.isArray(scene.entities) || new Set(scene.entities).size !== scene.entities.length || scene.entities.some((id) => !ids.has(id))) fail(`${scene.id}: entity references must be unique and known`);
    const count = scene.entities.length;
    const bounds = {hook: [0, 0], outcome: [0, 0], pipeline: [2, 5], routing: [4, 4], parallel: [4, 5], feedback: [3, 3], compare: [2, 2]}[scene.kind];
    if (count < bounds[0] || count > bounds[1]) fail(`${scene.id}: ${scene.kind} requires ${bounds[0]}–${bounds[1]} entities`);
    if (scene.kind === 'routing' && !scene.entities.slice(2).includes(scene.active_branch)) fail(`${scene.id}: routing requires one active_branch from the final two entities`);
    if (scene.kind !== 'routing' && scene.active_branch !== undefined) fail(`${scene.id}: active_branch only applies to routing`);
    if (scene.kind === 'compare' && scene.entities.some((id) => !spec.entities.find((entity) => entity.id === id).detail?.trim())) fail(`${scene.id}: both comparison entities need a detail`);
    if (!Array.isArray(scene.claim_ids) || scene.claim_ids.some((id) => typeof id !== 'string')) fail(`${scene.id}.claim_ids must be an array`);
  }
  const duration = spec.scenes.reduce((sum, scene) => sum + scene.duration, 0);
  if (duration < 15 || duration > 90) fail('Total duration must be 15–90 seconds');
  return spec;
}

export function timeline(spec) {
  let from = 0;
  return spec.scenes.map((scene, index) => {
    const durationInFrames = Math.round(scene.duration * spec.fps);
    const entry = {scene, index, from, durationInFrames};
    from += durationInFrames;
    return entry;
  });
}

export function videoConfig(spec) {
  return {
    width: spec.format === 'portrait' ? 1080 : 1920,
    height: spec.format === 'portrait' ? 1920 : 1080,
    fps: spec.fps,
    durationInFrames: timeline(spec).reduce((sum, entry) => sum + entry.durationInFrames, 0),
  };
}

export function clamp(value, min = 0, max = 1) { return Math.max(min, Math.min(max, value)); }
export function ease(value) { const t = clamp(value); return t * t * (3 - 2 * t); }

// Durations explain event order, not measured latency. A final hold occupies 22%.
export function mechanismTime(frame, durationInFrames) {
  return clamp((frame - 18) / Math.max(1, durationInFrames * 0.72 - 18));
}

export function cardSize(kind, count, portrait) {
  if (kind === 'compare') return portrait ? {w: 810, h: 350} : {w: 700, h: 370};
  if (portrait && kind === 'pipeline') return {w: 660, h: 174};
  if (portrait && kind === 'parallel') return count === 5 ? {w: 280, h: 230} : {w: 340, h: 230};
  if (portrait && kind === 'routing') return {w: 390, h: 235};
  if (portrait && kind === 'feedback') return {w: 360, h: 235};
  if (kind === 'pipeline') return {w: count === 5 ? 276 : count === 4 ? 320 : 420, h: 260};
  if (kind === 'parallel' && count === 5) return {w: 370, h: 160};
  if (kind === 'parallel' && count === 4) return {w: 370, h: 260};
  return {w: 370, h: 225};
}

export function graphLayout(kind, count, portrait) {
  if (kind === 'pipeline') {
    const inset = 104 + cardSize(kind, count, false).w / 2;
    return Array.from({length: count}, (_, i) => portrait
      ? {x: 540, y: 600 + i * (780 / Math.max(1, count - 1))}
      : {x: inset + i * ((1920 - inset * 2) / Math.max(1, count - 1)), y: 565});
  }
  if (kind === 'routing') return portrait
    ? [{x: 540, y: 600}, {x: 540, y: 915}, {x: 290, y: 1230}, {x: 790, y: 1230}]
    : [{x: 290, y: 590}, {x: 790, y: 590}, {x: 1510, y: 445}, {x: 1510, y: 745}];
  if (kind === 'parallel') {
    const workers = count - 2;
    return portrait
      ? [{x: 540, y: 600}, ...Array.from({length: workers}, (_, i) => ({x: workers === 2 ? 240 + i * 600 : 210 + i * 330, y: 950})), {x: 540, y: 1320}]
      : [{x: 290, y: 600}, ...Array.from({length: workers}, (_, i) => ({x: 960, y: workers === 3 ? 420 + i * 180 : 460 + i * 280})), {x: 1630, y: 600}];
  }
  if (kind === 'feedback') return portrait
    ? [{x: 540, y: 600}, {x: 760, y: 1180}, {x: 320, y: 1180}]
    : [{x: 430, y: 450}, {x: 1490, y: 450}, {x: 960, y: 735}];
  if (kind === 'compare') return portrait
    ? [{x: 540, y: 690}, {x: 540, y: 1160}]
    : [{x: 515, y: 565}, {x: 1405, y: 565}];
  return [];
}

export function graphEdges(scene) {
  const n = scene.entities.length;
  if (scene.kind === 'pipeline') return Array.from({length: n - 1}, (_, i) => ({from: i, to: i + 1, start: i / (n - 1), end: (i + 1) / (n - 1), active: true}));
  if (scene.kind === 'routing') return [{from: 0, to: 1, start: 0, end: 0.4, active: true}, ...[2, 3].map((to) => ({from: 1, to, start: 0.5, end: 1, active: scene.entities[to] === scene.active_branch}))];
  if (scene.kind === 'parallel') {
    const workers = Array.from({length: n - 2}, (_, i) => i + 1);
    return [...workers.map((to) => ({from: 0, to, start: 0, end: 0.35, active: true})), ...workers.map((from) => ({from, to: n - 1, start: 0.66, end: 1, active: true}))];
  }
  if (scene.kind === 'feedback') return [{from: 0, to: 1, start: 0, end: 0.3, active: true}, {from: 1, to: 2, start: 0.36, end: 0.66, active: true}, {from: 2, to: 0, start: 0.72, end: 1, active: true}];
  return [];
}

export function nodeActivation(scene, index, progress) {
  if (scene.kind === 'pipeline') return progress >= index / (scene.entities.length - 1);
  if (scene.kind === 'routing') return index === 0 || (index === 1 && progress >= 0.4) || (scene.entities[index] === scene.active_branch && progress >= 1);
  if (scene.kind === 'parallel') return index === 0 || (index === scene.entities.length - 1 ? progress >= 1 : progress >= 0.35);
  if (scene.kind === 'feedback') return index === 0 || (index === 1 ? progress >= 0.3 : progress >= 0.66);
  return progress >= 0.15;
}

export function storyboardFrames(spec) {
  return timeline(spec).flatMap(({scene, index, from, durationInFrames}) =>
    [['early', 0.18], ['middle', 0.48], ['late', 0.86]].map(([phase, fraction]) => ({
      scene: scene.id, index, phase, frame: from + Math.min(durationInFrames - 1, Math.floor(durationInFrames * fraction)),
      filename: `${String(index + 1).padStart(2, '0')}-${scene.id}-${phase}.png`,
    })));
}

export function renderProps(spec) {
  return {spec: {schema_version: spec.schema_version, title: spec.title, audience: spec.audience, goal: spec.goal, format: spec.format, fps: spec.fps, theme: spec.theme, language: spec.language || 'en', entities: spec.entities, scenes: spec.scenes}};
}

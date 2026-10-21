import assert from 'node:assert/strict';
import {test} from 'node:test';
import {cardSize, graphEdges, graphLayout, nodeActivation, renderProps, storyboardFrames, validateSpec, videoConfig} from '../../../skill-library/project-to-motion/assets/renderer/model.mjs';

function spec(kind = 'routing') {
  const entities = ['input', 'router', 'left', 'right'].map((id) => ({id, label: id, detail: `${id} detail`}));
  return {schema_version: 1, title: 'Choose one route', audience: 'Developers', goal: 'Explain selection', format: 'landscape', theme: 'paper', fps: 30, entities, scenes: [
    {id: 'question', kind: 'hook', duration: 3, title: 'Where does this request go?', caption: 'A selected route follows the request.', entities: [], claim_ids: []},
    {id: 'mechanism', kind, duration: 9, title: 'Select a branch', caption: 'Only the chosen branch receives this request.', entities: entities.map((entity) => entity.id), active_branch: 'left', claim_ids: ['selection']},
    {id: 'result', kind: 'outcome', duration: 3, title: 'One request, one route', caption: 'The other route remains available.', entities: [], claim_ids: ['selection']},
  ]};
}

test('routing sends a token to exactly the selected branch', () => {
  const scene = spec().scenes[1];
  const branches = graphEdges(scene).filter((edge) => edge.from === 1 && edge.active);
  assert.equal(branches.length, 1);
  assert.equal(scene.entities[branches[0].to], 'left');
  assert.equal(nodeActivation(scene, 2, 1), true);
  assert.equal(nodeActivation(scene, 3, 1), false);
});

test('parallel join remains inactive until all branch arrivals complete', () => {
  const scene = {kind: 'parallel', entities: ['input', 'a', 'b', 'c', 'join']};
  const returns = graphEdges(scene).filter((edge) => edge.to === 4);
  assert.equal(returns.length, 3);
  assert.ok(returns.every((edge) => edge.start > 0.35 && edge.end === 1));
  assert.equal(nodeActivation(scene, 4, 0.99), false);
  assert.equal(nodeActivation(scene, 4, 1), true);
});

test('portrait changes geometry without changing narrative duration', () => {
  const movie = validateSpec(spec());
  const wide = videoConfig(movie);
  movie.format = 'portrait';
  const tall = videoConfig(movie);
  assert.deepEqual([wide.width, wide.height], [1920, 1080]);
  assert.deepEqual([tall.width, tall.height], [1080, 1920]);
  assert.equal(tall.durationInFrames, wide.durationInFrames);
});

test('storyboard samples every scene inside its frame interval', () => {
  const movie = spec();
  const frames = storyboardFrames(movie);
  assert.equal(frames.length, 9);
  const spans = [[0, 90], [90, 360], [360, 450]];
  for (const frame of frames) assert.ok(frame.frame > spans[frame.index][0] && frame.frame < spans[frame.index][1]);
});

test('malformed routing, unknown entities and sub-frame durations are rejected', () => {
  const missingRoute = spec();
  missingRoute.scenes[1].active_branch = 'router';
  assert.throws(() => validateSpec(missingRoute), /active_branch/);
  const unknown = spec();
  unknown.scenes[1].entities[0] = 'missing';
  assert.throws(() => validateSpec(unknown), /known/);
  const fractional = spec();
  fractional.scenes[1].duration = 9.001;
  assert.throws(() => validateSpec(fractional), /frame boundary/);
});

test('comparison requires the actual states being compared', () => {
  const movie = spec();
  movie.scenes[1] = {...movie.scenes[1], kind: 'compare', entities: ['left', 'right']};
  delete movie.scenes[1].active_branch;
  movie.entities[2].detail = '';
  assert.throws(() => validateSpec(movie), /comparison entities/);
});

test('all graph nodes remain inside the canvas and clear heading/caption bands', () => {
  for (const [kind, counts] of Object.entries({pipeline: [2, 3, 4, 5], routing: [4], parallel: [4, 5], feedback: [3], compare: [2]})) {
    for (const count of counts) for (const portrait of [false, true]) {
      const size = cardSize(kind, count, portrait);
      const width = portrait ? 1080 : 1920;
      const headingBottom = portrait ? 460 : 320;
      const captionTop = portrait ? 1590 : 890;
      for (const node of graphLayout(kind, count, portrait)) {
        assert.ok(node.x - size.w / 2 >= 50, `${kind}/${count}: left margin`);
        assert.ok(node.x + size.w / 2 <= width - 50, `${kind}/${count}: right margin`);
        assert.ok(node.y - size.h / 2 >= headingBottom, `${kind}/${count}: heading clearance`);
        assert.ok(node.y + size.h / 2 <= captionTop - 20, `${kind}/${count}: caption clearance`);
      }
    }
  }
});

test('language survives stripped browser props without source text', () => {
  const movie = spec();
  movie.language = 'zh';
  movie.sources = [{text: 'Do not send source text to the browser'}];
  const props = renderProps(validateSpec(movie));
  assert.equal(props.spec.language, 'zh');
  assert.equal(props.spec.sources, undefined);
  movie.language = 'unknown';
  assert.throws(() => validateSpec(movie), /language/);
});

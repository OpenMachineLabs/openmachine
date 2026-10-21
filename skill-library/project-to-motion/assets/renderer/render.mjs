#!/usr/bin/env node
import {createHash} from 'node:crypto';
import {readFile, writeFile, mkdir, mkdtemp, rename, rm, stat, lstat} from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawnSync} from 'node:child_process';
import {renderProps, storyboardFrames, timeline, validateSpec, videoConfig} from './model.mjs';

const directory = path.dirname(fileURLToPath(import.meta.url));
const digest = (value) => createHash('sha256').update(value).digest('hex');
const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (character) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[character]));

function options(argv) {
  const result = {concurrency: 2, stillsOnly: false, pythonExecutable: 'python3'};
  for (let index = 0; index < argv.length; index += 1) {
    const flag = argv[index];
    if (flag === '--help') return {help: true};
    if (flag === '--stills-only') { result.stillsOnly = true; continue; }
    if (!['--spec', '--output', '--concurrency', '--browser-executable', '--python-executable'].includes(flag) || !argv[index + 1] || argv[index + 1].startsWith('--')) throw new Error(`Unknown or incomplete argument: ${flag}`);
    const value = argv[++index];
    if (flag === '--spec') result.spec = path.resolve(value);
    if (flag === '--output') result.output = path.resolve(value);
    if (flag === '--browser-executable') result.browserExecutable = path.resolve(value);
    if (flag === '--python-executable') result.pythonExecutable = value;
    if (flag === '--concurrency') result.concurrency = Number(value);
  }
  if (!result.spec || !result.output) throw new Error('Both --spec and --output are required');
  if (!Number.isInteger(result.concurrency) || result.concurrency < 1 || result.concurrency > 8) throw new Error('--concurrency must be an integer from 1 to 8');
  return result;
}

function stamp(frames, fps, separator = ',') {
  const ms = Math.round(frames / fps * 1000);
  return `${String(Math.floor(ms / 3600000)).padStart(2, '0')}:${String(Math.floor(ms / 60000) % 60).padStart(2, '0')}:${String(Math.floor(ms / 1000) % 60).padStart(2, '0')}${separator}${String(ms % 1000).padStart(3, '0')}`;
}

function storyboardHtml(spec, frames, stillsOnly) {
  const rows = spec.scenes.map((scene, index) => `<section><h2>${String(index + 1).padStart(2, '0')} · ${escapeHtml(scene.title)}</h2><p>${escapeHtml(scene.caption)}</p><div class="frames">${frames.filter((frame) => frame.scene === scene.id).map((frame) => `<figure><img src="storyboard/${escapeHtml(frame.filename)}" alt="${escapeHtml(scene.title)} — ${frame.phase}"><figcaption>${frame.phase} · frame ${frame.frame}</figcaption></figure>`).join('')}</div><p class="note">Claims: ${scene.claim_ids.map(escapeHtml).join(', ') || 'No factual claim'} · ${scene.duration}s · ${escapeHtml(scene.kind)}</p></section>`).join('\n');
  return `<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${escapeHtml(spec.title)} — storyboard</title><style>body{margin:0 auto;max-width:1500px;padding:40px;font:18px/1.5 system-ui;background:#f3f0e8;color:#252c2a}h1{font:48px Georgia,serif}h2{font:30px Georgia,serif}.frames{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px}figure{margin:0}img{max-width:100%;border:1px solid #d2d4c9}figcaption,.note{font-size:14px;color:#646b65}section{padding:28px 0;border-top:1px solid #d2d4c9}video{max-width:100%;max-height:75vh}.review{padding:20px;background:#fffdf7;border-left:4px solid #c96548}@media(max-width:700px){body{padding:20px}.frames{grid-template-columns:1fr}}</style><h1>${escapeHtml(spec.title)}</h1><p>${escapeHtml(spec.format)} · ${spec.scenes.reduce((sum, scene) => sum + scene.duration, 0)} seconds · silent-first</p>${stillsOnly ? '<p>Storyboard-only render; no MP4 generated.</p>' : '<video src="video.mp4" controls preload="metadata"></video>'}<p class="review">Render completion does not certify the story. Review every scene for factual support, readable text, correct routing/join order, object continuity, and pacing. Motion timing is illustrative, not measured performance. System fonts can differ between machines.</p>${rows}</html>\n`;
}

async function absent(location) {
  try { await lstat(location); throw new Error(`Output already exists: ${location}. Choose a new output directory.`); }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
}

async function run() {
  const args = options(process.argv.slice(2));
  if (args.help) {
    console.log('node render.mjs --spec SPEC.json --output NEW_DIRECTORY [--stills-only] [--concurrency 1..8] [--browser-executable PATH] [--python-executable PATH]');
    return;
  }
  const metadata = await stat(args.spec);
  if (!metadata.isFile() || metadata.size > 8 * 1024 * 1024) throw new Error('Spec must be a regular JSON file no larger than 8 MiB');
  const bytes = await readFile(args.spec);
  const spec = validateSpec(JSON.parse(bytes.toString('utf8')));
  // Both public entry points use one canonical evidence validator. No input code runs.
  const validation = spawnSync(args.pythonExecutable, [path.resolve(directory, '../../scripts/motion.py'), 'validate', '-'], {input: bytes, encoding: 'utf8', maxBuffer: 1024 * 1024, timeout: 30000});
  if (validation.error || validation.status !== 0) throw new Error(`Canonical evidence validation failed: ${validation.error?.message || validation.stderr || validation.stdout || `exit ${validation.status}`}`);
  await absent(args.output);
  await mkdir(path.dirname(args.output), {recursive: true});
  const staging = await mkdtemp(path.join(path.dirname(args.output), '.project-to-motion-'));
  let browser;
  try {
    const [{bundle}, {openBrowser, selectComposition, renderStill, renderMedia}] = await Promise.all([import('@remotion/bundler'), import('@remotion/renderer')]);
    const packageInfo = JSON.parse(await readFile(path.join(directory, 'package.json'), 'utf8'));
    const serveUrl = await bundle({entryPoint: path.join(directory, 'entry.jsx'), outDir: path.join(staging, '.bundle')});
    browser = await openBrowser('chrome', {browserExecutable: args.browserExecutable, logLevel: 'warn'});
    const inputProps = renderProps(spec);
    const common = {serveUrl, inputProps, puppeteerInstance: browser, logLevel: 'warn'};
    const composition = await selectComposition({...common, id: 'ProjectToMotion'});
    const config = videoConfig(spec);
    for (const field of ['width', 'height', 'fps', 'durationInFrames']) {
      if (composition[field] !== config[field]) throw new Error(`Composition ${field} does not match the validated spec`);
    }
    const frames = storyboardFrames(spec);
    await mkdir(path.join(staging, 'storyboard'));
    for (const frame of frames) {
      await renderStill({...common, composition, frame: frame.frame, imageFormat: 'png', output: path.join(staging, 'storyboard', frame.filename)});
      console.log(`Storyboard ${frame.index + 1}/${spec.scenes.length} ${frame.phase}`);
    }
    if (!args.stillsOnly) {
      let lastPercent = -10;
      await renderMedia({...common, composition, codec: 'h264', pixelFormat: 'yuv420p', crf: 18, concurrency: args.concurrency, outputLocation: path.join(staging, 'video.mp4'), onProgress: ({progress}) => {
        const percent = Math.floor(progress * 100 / 10) * 10;
        if (percent > lastPercent) { lastPercent = percent; console.log(`Video ${percent}%`); }
      }});
    }
    await browser.close({silent: true});
    browser = null;
    await rm(path.join(staging, '.bundle'), {recursive: true, force: true});
    const entries = timeline(spec);
    const captions = entries.map(({scene, index, from, durationInFrames}) => `${index + 1}\n${stamp(from, spec.fps)} --> ${stamp(from + durationInFrames, spec.fps)}\n${scene.caption.split(/\r?\n/).map((line) => line.trim()).filter(Boolean).join('\n')}\n`).join('\n');
    const narration = entries.map(({scene, from}) => `[${stamp(from, spec.fps, '.')}] ${scene.title}\n${scene.narration || scene.caption}\n`).join('\n');
    await writeFile(path.join(staging, 'captions.srt'), captions, 'utf8');
    await writeFile(path.join(staging, 'narration.txt'), narration, 'utf8');
    await writeFile(path.join(staging, 'motion.json'), bytes);
    await writeFile(path.join(staging, 'storyboard.html'), storyboardHtml(spec, frames, args.stillsOnly), 'utf8');
    const names = ['motion.json', 'captions.srt', 'narration.txt', 'storyboard.html', ...frames.map((frame) => `storyboard/${frame.filename}`)];
    if (!args.stillsOnly) names.unshift('video.mp4');
    const files = [];
    for (const name of names) {
      const data = await readFile(path.join(staging, name));
      if (data.length === 0) throw new Error(`Empty render artifact: ${name}`);
      files.push({path: name, bytes: data.length, sha256: digest(data)});
    }
    const manifest = {
      schema_version: 1,
      renderer: 'project-to-motion', renderer_version: packageInfo.version,
      remotion_version: packageInfo.dependencies.remotion,
      spec_sha256: digest(bytes), editable_spec: 'motion.json', created_at: new Date().toISOString(),
      mode: args.stillsOnly ? 'storyboard' : 'video', audio: 'none',
      ...config, duration_seconds: config.durationInFrames / config.fps,
      fonts: {heading: 'Georgia / Times New Roman / Songti SC', body: 'Arial / PingFang SC / Microsoft YaHei / Noto Sans CJK SC', portability: 'System fallbacks; review after changing the render host.'},
      scenes: entries.map(({scene, from, durationInFrames}) => ({id: scene.id, kind: scene.kind, from, durationInFrames, claim_ids: scene.claim_ids})),
      storyboard_frames: frames, files,
      qa: {render_completed: true, text_overflow: args.stillsOnly ? 'checked while rendering sampled storyboard frames' : 'checked while rendering sampled storyboard frames and all video frames', human_visual_review: 'required', semantic_review: 'required', note: 'Motion durations are illustrative, not measured latency or throughput.'},
    };
    await writeFile(path.join(staging, 'render-manifest.json'), `${JSON.stringify(manifest, null, 2)}\n`, 'utf8');
    await absent(args.output);
    await rename(staging, args.output);
    console.log(JSON.stringify({output: args.output, mode: manifest.mode, frames: config.durationInFrames, seconds: manifest.duration_seconds, files: files.length + 1}));
  } catch (error) {
    if (browser) await browser.close({silent: true}).catch(() => {});
    await rm(staging, {recursive: true, force: true});
    throw error;
  }
}

run().catch((error) => { console.error(`Render failed: ${error.message}`); process.exitCode = 1; });

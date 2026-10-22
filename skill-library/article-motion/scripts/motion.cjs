#!/usr/bin/env node
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { parseArgs } = require('node:util');
const { spawn, execFileSync } = require('node:child_process');

const HELP = `Article Motion — local, silent Canvas films

  node scripts/motion.cjs still FILM.cjs --at 2 --out frame.png
  node scripts/motion.cjs render FILM.cjs --out film.mp4
  node scripts/motion.cjs render FILM.cjs --start 2 --end 4 --scale 0.5 --out clip.mp4
  node scripts/motion.cjs inspect film.mp4 --under 40

FILM exports width, height, fps, duration (seconds), draw(ctx, time), and optional
setup({loadImage, GlobalFonts}). Time is absolute film time, including in clips.
Use --overwrite to replace an existing output. FFMPEG and FFPROBE may name local
executables; otherwise they are resolved from PATH. Inspection checks technical
properties and decodes the video; it cannot approve content or visual quality.
`;

function number(value, name) {
  if (value === '' || !Number.isFinite(Number(value))) throw new Error(`${name} must be a finite number`);
  return Number(value);
}

function checkFilm(film) {
  if (!film || typeof film !== 'object') throw new Error('Film must export a metadata object');
  for (const name of ['width', 'height']) {
    if (!Number.isSafeInteger(film[name]) || film[name] < 2 || film[name] % 2) {
      throw new Error(`${name} must be a positive even integer`);
    }
  }
  if (!Number.isSafeInteger(film.fps) || film.fps < 1) throw new Error('fps must be a positive integer');
  if (typeof film.duration !== 'number' || !Number.isFinite(film.duration) || film.duration <= 0) {
    throw new Error('duration must be a positive number of seconds');
  }
  if (!Number.isSafeInteger(Math.ceil(film.duration * film.fps))) {
    throw new Error('duration * fps exceeds the supported frame count');
  }
  if (typeof film.draw !== 'function' || (film.setup != null && typeof film.setup !== 'function')) {
    throw new Error('film needs draw(ctx, time) and an optional setup function');
  }
}

function options(argv) {
  const { values, positionals } = parseArgs({
    args: argv, allowPositionals: true, strict: true,
    options: {
      out: { type: 'string' }, at: { type: 'string' },
      start: { type: 'string' }, end: { type: 'string' },
      scale: { type: 'string' }, under: { type: 'string' },
      overwrite: { type: 'boolean', default: false }, help: { type: 'boolean' },
    },
  });
  if (values.help) return { help: true };
  const [command, input] = positionals;
  const allowed = { still: ['out', 'at', 'scale', 'overwrite'],
    render: ['out', 'start', 'end', 'scale', 'overwrite'], inspect: ['under'] };
  if (!allowed[command] || positionals.length !== 2) throw new Error(HELP);
  for (const name of Object.keys(values)) {
    if (name === 'overwrite' && !values[name]) continue;
    if (!allowed[command].includes(name)) throw new Error(`--${name} is not valid for ${command}`);
  }
  return { command, input: path.resolve(input), ...values };
}

// Publish only a completed output. Failed replacements preserve the prior file.
async function outputFile(destination, overwrite, produce) {
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  if (!overwrite && fs.existsSync(destination)) throw new Error('Output exists; choose another path or use --overwrite');
  const temporary = fs.mkdtempSync(path.join(path.dirname(destination), '.article-motion-'));
  const partial = path.join(temporary, path.basename(destination));
  try {
    const result = await produce(partial);
    if (!fs.statSync(partial).size) throw new Error('Renderer produced an empty output');
    if (overwrite) fs.renameSync(partial, destination);
    else fs.linkSync(partial, destination); // Exclusive publication, including a destination race.
    return result;
  } finally {
    fs.rmSync(temporary, { recursive: true, force: true });
  }
}

async function encode(filename, width, height, fps, first, last, draw) {
  const child = spawn(process.env.FFMPEG || 'ffmpeg', [
    '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
    '-f', 'rawvideo', '-pixel_format', 'rgba', '-video_size', `${width}x${height}`,
    '-framerate', String(fps), '-i', 'pipe:0', '-an', '-c:v', 'libx264',
    '-preset', 'fast', '-crf', '17', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', filename,
  ], { stdio: ['pipe', 'ignore', 'pipe'] });
  let stderr = '', failure = null, interrupted = false;
  child.stderr.on('data', data => { stderr = (stderr + data.toString()).slice(-8192); });
  child.stdin.on('error', error => { failure ||= error; });
  child.on('error', error => { failure ||= error; });
  const closed = new Promise(resolve => child.once('close', resolve));
  const cancel = () => { interrupted = true; child.stdin.destroy(); child.kill('SIGTERM'); };
  process.once('SIGINT', cancel);
  process.once('SIGTERM', cancel);
  try {
    for (let frame = first; frame < last; frame++) {
      if (interrupted) throw new Error('Render interrupted');
      if (failure) throw failure;
      const pixels = await draw(frame / fps);
      await new Promise((resolve, reject) => {
        child.stdin.write(Buffer.from(pixels.buffer, pixels.byteOffset, pixels.byteLength),
          error => error ? reject(error) : resolve());
      });
    }
    child.stdin.end();
    const code = await closed;
    if (interrupted || failure || code !== 0) {
      throw new Error(interrupted ? 'Render interrupted' : `FFmpeg failed: ${failure?.message || stderr || code}`);
    }
  } catch (error) {
    child.stdin.destroy();
    child.kill('SIGTERM');
    await closed;
    throw new Error(`${failure?.message || error.message}${stderr ? `\n${stderr}` : ''}`);
  } finally {
    process.removeListener('SIGINT', cancel);
    process.removeListener('SIGTERM', cancel);
  }
}

function inspect(input, under) {
  const limit = under == null ? null : number(under, '--under');
  if (limit != null && limit <= 0) throw new Error('--under must be positive');
  const metadata = JSON.parse(execFileSync(process.env.FFPROBE || 'ffprobe', [
    '-v', 'error', '-show_streams', '-show_format', '-of', 'json', input,
  ], { encoding: 'utf8', timeout: 15000, maxBuffer: 4 * 1024 * 1024 }));
  const streams = metadata.streams || [];
  const videos = streams.filter(stream => stream.codec_type === 'video');
  const audio = streams.filter(stream => stream.codec_type === 'audio');
  const video = videos[0] || {};
  const duration = Number(metadata.format?.duration);
  const [numerator, denominator] = String(video.avg_frame_rate || '').split('/').map(Number);
  const fps = numerator / denominator;
  const checks = {
    one_video: videos.length === 1,
    no_audio: audio.length === 0,
    mp4_container: String(metadata.format?.format_name || '').split(',').includes('mp4'),
    h264_yuv420p: video.codec_name === 'h264' && video.pix_fmt === 'yuv420p',
    dimensions: Number.isInteger(video.width) && video.width > 0 && video.width % 2 === 0
      && Number.isInteger(video.height) && video.height > 0 && video.height % 2 === 0,
    frame_rate: Number.isFinite(fps) && fps > 0,
    duration: Number.isFinite(duration) && duration > 0 && (limit == null || duration < limit),
  };
  if (Object.values(checks).every(Boolean)) {
    execFileSync(process.env.FFMPEG || 'ffmpeg', [
      '-v', 'error', '-xerror', '-nostdin', '-i', input, '-map', '0:v:0', '-f', 'null', '-',
    ], { stdio: ['ignore', 'ignore', 'pipe'], timeout: 120000 });
    checks.decode = true;
  } else checks.decode = null;
  return {
    ok: Object.values(checks).every(Boolean), input,
    width: video.width, height: video.height, fps, duration,
    audio_streams: audio.length, checks,
    semantic_review: 'not_assessed', visual_review: 'not_assessed',
  };
}

async function main(argv = process.argv.slice(2)) {
  const args = options(argv);
  if (args.help) { console.log(HELP); return; }
  if (args.command === 'inspect') {
    const report = inspect(args.input, args.under);
    console.log(JSON.stringify(report, null, 2));
    if (!report.ok) process.exitCode = 1;
    return;
  }
  if (path.extname(args.input) !== '.cjs') throw new Error('Film must be a trusted .cjs module');
  if (!args.out) throw new Error('--out is required');
  const destination = path.resolve(args.out);
  const extension = args.command === 'still' ? '.png' : '.mp4';
  if (path.extname(destination).toLowerCase() !== extension) throw new Error(`Output must end in ${extension}`);
  if (destination === args.input) throw new Error('Output must differ from the source');
  const film = require(args.input);
  checkFilm(film);
  const scale = args.scale == null ? 1 : number(args.scale, '--scale');
  const width = film.width * scale, height = film.height * scale;
  if (scale <= 0 || !Number.isSafeInteger(width) || !Number.isSafeInteger(height)
      || width < 2 || height < 2 || width % 2 || height % 2) {
    throw new Error('--scale must produce positive even integer dimensions');
  }
  let at, first, last;
  if (args.command === 'still') {
    at = number(args.at ?? 0, '--at');
    if (at < 0 || at >= film.duration) throw new Error('--at must be inside [0, duration)');
  } else {
    const start = number(args.start ?? 0, '--start');
    const end = number(args.end ?? film.duration, '--end');
    if (start < 0 || end > film.duration || end <= start) throw new Error('Need 0 <= start < end <= duration');
    first = Math.ceil(start * film.fps);
    last = Math.ceil(end * film.fps);
    if (first === last) throw new Error('Requested interval contains no frames');
  }
  if (!args.overwrite && fs.existsSync(destination)) throw new Error('Output exists; choose another path or use --overwrite');
  let canvasAPI;
  try { canvasAPI = require('@napi-rs/canvas'); }
  catch (error) { throw new Error(`Canvas unavailable. Run npm ci in the article-motion Skill directory. ${error.message}`); }
  await film.setup?.({ loadImage: canvasAPI.loadImage, GlobalFonts: canvasAPI.GlobalFonts });
  const canvas = canvasAPI.createCanvas(width, height);
  const context = canvas.getContext('2d');
  const draw = async time => {
    context.reset();
    context.scale(scale, scale);
    context.fillStyle = '#ffffff';
    context.fillRect(0, 0, film.width, film.height);
    await film.draw(context, time);
    return context.getImageData(0, 0, width, height).data;
  };
  await outputFile(destination, args.overwrite, async partial => {
    if (args.command === 'still') {
      await draw(at);
      fs.writeFileSync(partial, canvas.toBuffer('image/png'));
    } else await encode(partial, width, height, film.fps, first, last, draw);
  });
  console.log(JSON.stringify({
    output: destination, width, height, fps: film.fps,
    ...(args.command === 'still' ? { time: at } : {
      start: first / film.fps, frames: last - first, duration: (last - first) / film.fps,
    }),
  }, null, 2));
}

if (require.main === module) {
  main().catch(error => { console.error(`FAIL: ${error.message}`); process.exitCode = 1; });
}

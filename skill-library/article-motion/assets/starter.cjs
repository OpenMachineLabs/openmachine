// Adapt this film to the article. Source and claim boundaries: example-brief.md.
// It uses the same archive-review article as anthropic-style-illustrations.
'use strict';

const width = 1280, height = 720;
const paper = '#f5f1e7', ink = '#263945', accent = '#cd774e', line = '#bbc7c8';
const clamp = value => Math.max(0, Math.min(1, value));
const ease = value => { const x = clamp(value); return x * x * (3 - 2 * x); };
const lerp = (a, b, t) => a + (b - a) * t;
const stages = ['Request', 'Catalogue', 'Access', 'Response'];
const positions = [0, 430, 860, 1290];

function text(ctx, value, x, y, size, color = ink, align = 'center') {
  ctx.fillStyle = color;
  ctx.font = `${size}px sans-serif`;
  ctx.textAlign = align;
  ctx.fillText(value, x, y);
}

function path(ctx, points, color = line, strokeWidth = 2) {
  ctx.beginPath();
  ctx.moveTo(...points[0]);
  for (const point of points.slice(1)) ctx.lineTo(...point);
  ctx.strokeStyle = color;
  ctx.lineWidth = strokeWidth;
  ctx.lineJoin = 'round';
  ctx.stroke();
}

function sheet(ctx, x, y, scale, fill) {
  ctx.save();
  ctx.translate(x, y);
  ctx.scale(scale, scale);
  ctx.beginPath();
  ctx.roundRect(-65, -88, 130, 176, 8);
  ctx.fillStyle = fill;
  ctx.fill();
  ctx.strokeStyle = ink;
  ctx.lineWidth = 1.5;
  ctx.stroke();
  for (let i = 0; i < 4; i++) path(ctx, [[-42, -47 + i * 25], [i === 3 ? 16 : 42, -47 + i * 25]], '#8c9d9e', 2);
  ctx.restore();
}

module.exports = {
  width, height, fps: 60, duration: 12,
  draw(ctx, time) {
    ctx.fillStyle = paper;
    ctx.fillRect(0, 0, width, height);
    const progress = clamp((time - 1) / 6.6) * 3;
    const index = Math.min(2, Math.floor(progress));
    const local = ease(progress - index);
    const recordX = lerp(positions[index], positions[index + 1], local);
    const reveal = ease((time - 7.8) / 1.7);
    const cameraX = lerp(recordX, 645, reveal);
    const zoom = lerp(1.25, 0.72, reveal);

    // A stable world and one record; the camera moves instead of swapping pages.
    ctx.save();
    ctx.translate(width / 2, 310);
    ctx.scale(zoom, zoom);
    ctx.translate(-cameraX, 0);
    path(ctx, [[-100, 110], [1390, 110]]);
    for (let i = 0; i < positions.length; i++) {
      const x = positions[i];
      ctx.beginPath();
      ctx.arc(x, 110, 6, 0, Math.PI * 2);
      ctx.fillStyle = line;
      ctx.fill();
      text(ctx, stages[i], x, 160, 24);
    }
    const clarify = ease((time - 2.6) / 0.4) * (1 - ease((time - 4.6) / 0.4));
    ctx.save();
    ctx.globalAlpha = clarify;
    path(ctx, [[430, -92], [430, -102], [160, -102]], accent);
    text(ctx, 'Missing details? Clarify.', 300, -122, 20, accent);
    ctx.restore();

    sheet(ctx, recordX, 0, 1, '#fffdf8');
    sheet(ctx, recordX + 61, 44, 0.48, '#e2e9e5');
    text(ctx, 'Evidence', recordX + 62, 56, 12);
    ctx.beginPath();
    ctx.arc(recordX, 110, 9, 0, Math.PI * 2);
    ctx.fillStyle = accent;
    ctx.fill();
    ctx.restore();

    const title = time < 7.8 ? 'A request travels. Its evidence stays attached.' : 'Return the decision with its notes.';
    text(ctx, title, width / 2, 100, 35);
    const captions = ['Record the research question.', 'Check the collection catalogue.', 'Review access conditions.', 'Keep unresolved conditions visible.'];
    const active = Math.min(3, Math.round(progress));
    text(ctx, captions[active], width / 2, 604, 28, '#6d7d85');
    text(ctx, 'Illustrative workflow, not a performance measurement', width / 2, 673, 16, '#6d7d85');
  },
};

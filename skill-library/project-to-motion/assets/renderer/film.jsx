import React, {useLayoutEffect, useRef} from 'react';
import {AbsoluteFill, Sequence, useCurrentFrame, useVideoConfig} from 'remotion';
import {cardSize, clamp, ease, graphEdges, graphLayout, mechanismTime, nodeActivation, timeline} from './model.mjs';

const THEMES = {
  paper: {background: '#f3f0e8', ink: '#252c2a', muted: '#646b65', card: '#fffdf7', line: '#d2d4c9', coral: '#c96548', pale: '#e7e9dd'},
  midnight: {background: '#192421', ink: '#f1efe4', muted: '#b2bfb4', card: '#24352f', line: '#506258', coral: '#ee9a7b', pale: '#30453b'},
};
const ACCENTS = ['#c96548', '#608d78', '#527e9f', '#a38746', '#947291', '#638d8e'];
const SANS = 'Arial, "PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif';
const SERIF = 'Georgia, "Times New Roman", "Songti SC", serif';
const MONO = 'ui-monospace, "SFMono-Regular", Consolas, monospace';
const MICROCOPY = {
  en: {closer: 'A CLOSER LOOK', takeaway: 'THE TAKEAWAY', routing: 'ONE SELECTED PATH', fanOut: 'FAN OUT', wait: 'WAIT FOR EVERY BRANCH', joined: 'ALL BRANCHES JOINED', feedback: 'ONE ILLUSTRATIVE CYCLE'},
  zh: {closer: '看清工作机制', takeaway: '核心结论', routing: '只进入选中的路径', fanOut: '分发到独立分支', wait: '等待各个分支完成', joined: '合并所有分支的结果', feedback: '一个示意性的循环'},
};
const words = (value) => Array.from(value).reduce((sum, char) => sum + (/[^\x00-\x7f]/.test(char) ? 1.8 : 1), 0);
const reveal = (frame, delay = 0, length = 24) => ease((frame - delay) / length);

function TextFit({children, style, ...props}) {
  const ref = useRef(null);
  useLayoutEffect(() => {
    const node = ref.current;
    if (node && (node.scrollHeight > node.clientHeight + 2 || node.scrollWidth > node.clientWidth + 2)) {
      throw new Error(`Text overflows its frame (${node.scrollWidth}x${node.scrollHeight} in ${node.clientWidth}x${node.clientHeight}): ${node.textContent.slice(0, 90)}. Shorten the text or revise the layout.`);
    }
  });
  return <div ref={ref} style={{overflowWrap: 'anywhere', paddingBottom: 8, boxSizing: 'border-box', ...style}} {...props}>{children}</div>;
}

function Background({theme, portrait}) {
  const {width, height} = useVideoConfig();
  return <AbsoluteFill style={{background: theme.background}}>
    <svg width={width} height={height} style={{position: 'absolute'}}>
      <defs><pattern id="paper-grid" width="32" height="32" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r="0.8" fill={theme.muted} opacity="0.14"/></pattern></defs>
      <rect width="100%" height="100%" fill="url(#paper-grid)"/>
      <path d={portrait ? 'M 70 155 L 1010 155' : 'M 88 118 L 1832 118'} stroke={theme.line} strokeWidth="2"/>
    </svg>
  </AbsoluteFill>;
}

function Header({spec, index, count, theme, portrait}) {
  return <div style={{position: 'absolute', top: portrait ? 84 : 60, left: portrait ? 70 : 88, right: portrait ? 70 : 88, display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 30, color: theme.muted, fontFamily: MONO, fontSize: portrait ? 23 : 20}}>
    <TextFit style={{maxHeight: 34, maxWidth: portrait ? 720 : 1380, whiteSpace: 'nowrap', fontSize: words(spec.title) > 56 ? 17 : undefined}}>{spec.title}</TextFit>
    <span style={{flexShrink: 0, letterSpacing: 3}}>{String(index + 1).padStart(2, '0')} / {String(count).padStart(2, '0')}</span>
  </div>;
}

function Caption({scene, frame, portrait, theme, hero = false}) {
  const show = reveal(frame, 14, 22);
  return <div style={{position: 'absolute', left: portrait ? 86 : 104, right: portrait ? 86 : 104, top: portrait ? 1590 : 890, display: 'flex', alignItems: 'flex-start', gap: 25, opacity: show, transform: `translateY(${12 * (1 - show)}px)`}}>
    <div style={{width: 50, height: 4, marginTop: portrait ? 25 : 21, background: theme.coral, flexShrink: 0}}/>
    <TextFit style={{fontFamily: SANS, fontSize: portrait ? 36 : 34, lineHeight: 1.35, letterSpacing: -0.3, color: hero ? theme.ink : theme.muted, maxHeight: portrait ? 210 : 145, width: portrait ? 815 : 1500, flexShrink: 0}}>{scene.caption}</TextFit>
  </div>;
}

function Hero({scene, frame, durationInFrames, portrait, theme, copy}) {
  const enter = reveal(frame, 2, 28);
  const stroke = ease(frame / Math.max(1, durationInFrames * 0.62));
  const isOutcome = scene.kind === 'outcome';
  const fontSize = portrait ? (words(scene.title) > 65 ? 88 : 106) : (words(scene.title) > 65 ? 100 : 120);
  const dialX = portrait ? 540 : 1500;
  const dialY = portrait ? 1260 : 520;
  const radius = portrait ? 140 : 170;
  return <>
    <div style={{position: 'absolute', top: portrait ? 260 : 238, left: portrait ? 88 : 104, width: portrait ? 904 : 1120, opacity: enter, transform: `translateY(${24 * (1 - enter)}px)`}}>
      <div style={{fontFamily: SANS, letterSpacing: 4, fontSize: 24, color: theme.coral, marginBottom: 36}}>{isOutcome ? copy.takeaway : copy.closer}</div>
      <TextFit style={{fontFamily: SERIF, color: theme.ink, fontSize, lineHeight: 1.08, letterSpacing: -3, maxHeight: portrait ? 650 : 550, fontWeight: 400}}>{scene.title}</TextFit>
    </div>
    <svg width={portrait ? 1080 : 1920} height={portrait ? 1920 : 1080} style={{position: 'absolute', opacity: enter}}>
      <circle cx={dialX} cy={dialY} r={radius + 30} fill="none" stroke={theme.line} strokeWidth="1.5"/>
      <circle cx={dialX} cy={dialY} r={radius} fill="none" stroke={theme.line} strokeWidth="3"/>
      <circle cx={dialX} cy={dialY} r={radius} fill="none" stroke={theme.coral} strokeWidth="6" pathLength="1" strokeDasharray={`${stroke * 0.82} 1`} transform={`rotate(-90 ${dialX} ${dialY})`}/>
      <path d={`M ${dialX - 65} ${dialY} L ${dialX + 65} ${dialY} M ${dialX + 30} ${dialY - 35} L ${dialX + 65} ${dialY} L ${dialX + 30} ${dialY + 35}`} fill="none" stroke={theme.ink} strokeWidth="5" strokeLinecap="round" strokeLinejoin="round"/>
      <circle cx={dialX + Math.cos(-Math.PI / 2 + stroke * Math.PI * 1.64) * radius} cy={dialY + Math.sin(-Math.PI / 2 + stroke * Math.PI * 1.64) * radius} r="12" fill={theme.coral}/>
    </svg>
    <Caption scene={scene} frame={frame} portrait={portrait} theme={theme} hero/>
  </>;
}

function endpoints(a, b, size) {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const fraction = Math.min(dx === 0 ? Infinity : size.w / 2 / Math.abs(dx), dy === 0 ? Infinity : size.h / 2 / Math.abs(dy));
  return [{x: a.x + dx * fraction, y: a.y + dy * fraction}, {x: b.x - dx * fraction, y: b.y - dy * fraction}];
}

function Connector({edge, positions, size, progress, theme, portrait, kind}) {
  const [a, b] = endpoints(positions[edge.from], positions[edge.to], size);
  const bend = ['routing', 'parallel'].includes(kind);
  const c1 = bend ? (portrait ? {x: a.x, y: (a.y + b.y) / 2} : {x: (a.x + b.x) / 2, y: a.y}) : a;
  const c2 = bend ? (portrait ? {x: b.x, y: (a.y + b.y) / 2} : {x: (a.x + b.x) / 2, y: b.y}) : b;
  const path = `M ${a.x} ${a.y} C ${c1.x} ${c1.y}, ${c2.x} ${c2.y}, ${b.x} ${b.y}`;
  const move = clamp((progress - edge.start) / (edge.end - edge.start));
  const t = ease(move);
  const point = {x: (1 - t) ** 3 * a.x + 3 * (1 - t) ** 2 * t * c1.x + 3 * (1 - t) * t ** 2 * c2.x + t ** 3 * b.x, y: (1 - t) ** 3 * a.y + 3 * (1 - t) ** 2 * t * c1.y + 3 * (1 - t) * t ** 2 * c2.y + t ** 3 * b.y};
  const angle = Math.atan2(b.y - c2.y || b.y - a.y, b.x - c2.x || b.x - a.x);
  const arrow = `${b.x - Math.cos(angle - 0.5) * 17},${b.y - Math.sin(angle - 0.5) * 17} ${b.x},${b.y} ${b.x - Math.cos(angle + 0.5) * 17},${b.y - Math.sin(angle + 0.5) * 17}`;
  return <g>
    <path d={path} fill="none" stroke={theme.line} strokeWidth="4" strokeDasharray={edge.active ? undefined : '9 11'}/>
    <polyline points={arrow} fill="none" stroke={move >= 1 && edge.active ? theme.coral : theme.line} strokeWidth="4" strokeLinecap="round" strokeLinejoin="round"/>
    {edge.active && <>
      <path d={path} fill="none" stroke={theme.coral} strokeWidth="4" pathLength="1" strokeDasharray={`${t} 1`}/>
      {move > 0 && move < 1 && <><circle cx={point.x} cy={point.y} r="21" fill={theme.background}/><circle cx={point.x} cy={point.y} r="11" fill={theme.coral}/></>}
    </>}
  </g>;
}

function EntityCard({entity, position, size, index, allEntities, active, dimmed, frame, theme, scene, portrait}) {
  const enter = reveal(frame, 3 + index * 3, 20);
  const color = ACCENTS[allEntities.findIndex((candidate) => candidate.id === entity.id) % ACCENTS.length];
  const narrow = size.w < 350;
  const compare = scene.kind === 'compare';
  const compact = (portrait && scene.kind === 'pipeline') || (!portrait && scene.kind === 'parallel' && scene.entities.length === 5);
  const labelSize = compare ? 46 : compact ? (portrait ? 34 : 30) : narrow ? 29 : 34;
  const detailSize = compare ? 34 : compact ? (portrait ? 25 : 23) : narrow ? 23 : 25;
  const stripeWidth = active ? size.w - 2 : 0;
  return <div style={{position: 'absolute', left: position.x - size.w / 2, top: position.y - size.h / 2, width: size.w, height: size.h, border: `2px solid ${active ? color : theme.line}`, background: theme.card, borderRadius: compare ? 22 : 16, color: theme.ink, opacity: enter * (dimmed ? 0.5 : 1), transform: `translateY(${18 * (1 - enter)}px)`, boxSizing: 'border-box', boxShadow: `0 ${active ? 10 : 3}px 0 ${theme.pale}`, overflow: 'hidden'}}>
    <div style={{position: 'absolute', top: 0, left: 0, height: 5, width: stripeWidth, background: color}}/>
    <div style={{padding: compare ? '34px 40px' : compact ? (portrait ? '23px 30px' : '16px 22px') : narrow ? '25px 22px' : '27px 28px', display: 'flex', flexDirection: 'column', height: '100%', boxSizing: 'border-box', gap: compact ? 7 : 15}}>
      {!compact && <div style={{fontFamily: MONO, fontSize: 19, color, letterSpacing: 2}}>{String(allEntities.findIndex((candidate) => candidate.id === entity.id) + 1).padStart(2, '0')}<span style={{float: 'right', width: 9, height: 9, border: `2px solid ${active ? color : theme.line}`, background: active ? color : 'transparent', borderRadius: '50%', marginTop: 3}}/></div>}
      <TextFit style={{fontFamily: SANS, fontSize: labelSize, fontWeight: 600, letterSpacing: -0.7, lineHeight: 1.15, maxHeight: compact ? 76 : compare ? 112 : 80, flexShrink: 0}}>{entity.label}</TextFit>
      {entity.detail && <TextFit style={{fontFamily: SANS, fontSize: detailSize, lineHeight: 1.3, color: theme.muted, maxHeight: compare ? 170 : compact ? 67 : 112, minHeight: 0}}>{entity.detail}</TextFit>}
    </div>
  </div>;
}

function Mechanism({spec, scene, frame, durationInFrames, portrait, theme}) {
  const copy = MICROCOPY[spec.language || 'en'];
  const progress = mechanismTime(frame, durationInFrames);
  const positions = graphLayout(scene.kind, scene.entities.length, portrait);
  const size = cardSize(scene.kind, scene.entities.length, portrait);
  const edges = graphEdges(scene);
  const enter = reveal(frame, 0, 20);
  const isCompare = scene.kind === 'compare';
  return <>
    <TextFit style={{position: 'absolute', left: portrait ? 86 : 104, top: portrait ? 225 : 172, right: portrait ? 86 : 104, fontFamily: SERIF, color: theme.ink, fontSize: portrait ? 66 : 62, letterSpacing: -1.8, lineHeight: 1.13, maxHeight: portrait ? 235 : 180, opacity: enter, transform: `translateY(${12 * (1 - enter)}px)`}}>{scene.title}</TextFit>
    <svg width={portrait ? 1080 : 1920} height={portrait ? 1920 : 1080} style={{position: 'absolute', opacity: enter}}>
      {edges.map((edge) => <Connector key={`${edge.from}-${edge.to}`} edge={edge} positions={positions} size={size} progress={progress} theme={theme} portrait={portrait} kind={scene.kind}/>)}
      {isCompare && (portrait
        ? <line x1="210" y1="920" x2="870" y2="920" stroke={theme.line} strokeWidth="2" strokeDasharray="6 10"/>
        : <line x1="960" y1="380" x2="960" y2="750" stroke={theme.line} strokeWidth="2" strokeDasharray="6 10"/>)}
    </svg>
    {scene.entities.map((id, index) => <EntityCard key={id} entity={spec.entities.find((entity) => entity.id === id)} position={positions[index]} size={size} index={index} allEntities={spec.entities} active={nodeActivation(scene, index, progress)} dimmed={scene.kind === 'routing' && index >= 2 && id !== scene.active_branch} frame={frame} theme={theme} scene={scene} portrait={portrait}/>)}
    {scene.kind === 'parallel' && <div style={{position: 'absolute', top: portrait ? 1460 : 830, left: portrait ? 0 : 104, right: portrait ? 0 : undefined, width: portrait ? undefined : 600, textAlign: portrait ? 'center' : 'left', fontFamily: SANS, fontSize: portrait ? 21 : 20, color: theme.muted, opacity: enter, letterSpacing: 2}}>{progress >= 1 ? copy.joined : progress < 0.35 ? copy.fanOut : copy.wait}</div>}
    {scene.kind === 'routing' && <div style={{position: 'absolute', top: portrait ? 1460 : 835, left: 0, right: 0, textAlign: 'center', fontFamily: SANS, fontSize: 20, color: theme.muted, opacity: enter, letterSpacing: 2}}>{copy.routing}</div>}
    {scene.kind === 'feedback' && <div style={{position: 'absolute', top: portrait ? 1460 : 855, left: 0, right: 0, textAlign: 'center', fontFamily: SANS, fontSize: 18, color: theme.muted, opacity: enter, letterSpacing: 2}}>{copy.feedback}</div>}
    <Caption scene={scene} frame={frame} portrait={portrait} theme={theme}/>
  </>;
}

function Scene({spec, entry, count, theme, portrait}) {
  const frame = useCurrentFrame();
  const scene = entry.scene;
  const fade = 1 - ease((frame - entry.durationInFrames + 7) / 7);
  return <AbsoluteFill style={{opacity: fade}}>
    <Header spec={spec} index={entry.index} count={count} theme={theme} portrait={portrait}/>
    {['hook', 'outcome'].includes(scene.kind)
      ? <Hero scene={scene} frame={frame} durationInFrames={entry.durationInFrames} portrait={portrait} theme={theme} copy={MICROCOPY[spec.language || 'en']}/>
      : <Mechanism spec={spec} scene={scene} frame={frame} durationInFrames={entry.durationInFrames} portrait={portrait} theme={theme}/>}
  </AbsoluteFill>;
}

export function MotionFilm({spec}) {
  const frame = useCurrentFrame();
  const {width, height, durationInFrames} = useVideoConfig();
  if (!spec) return null;
  const portrait = spec.format === 'portrait';
  const theme = THEMES[spec.theme];
  const entries = timeline(spec);
  return <AbsoluteFill style={{fontFamily: SANS, width, height}}>
    <Background theme={theme} portrait={portrait}/>
    {entries.map((entry) => <Sequence key={entry.scene.id} from={entry.from} durationInFrames={entry.durationInFrames}><Scene spec={spec} entry={entry} count={entries.length} theme={theme} portrait={portrait}/></Sequence>)}
    <div style={{position: 'absolute', left: 0, bottom: 0, height: 5, width: width * (frame + 1) / durationInFrames, background: theme.coral}}/>
    <div style={{position: 'absolute', bottom: portrait ? 80 : 38, left: portrait ? 86 : 104, right: portrait ? 86 : 104, display: 'flex', gap: 8}}>{entries.map((entry) => <div key={entry.scene.id} style={{height: 3, flex: entry.durationInFrames, background: frame >= entry.from ? theme.coral : theme.line}}/>)}</div>
  </AbsoluteFill>;
}

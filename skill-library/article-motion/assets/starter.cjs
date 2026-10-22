// A mechanism study, not a fixed trailer layout. See example-brief.md.
'use strict';
const width = 1280, height = 720;
const P = {paper:'#f5f1e7', ink:'#263945', accent:'#cd774e', line:'#bbc7c8', white:'#fffdf8', sage:'#e2e9df', gold:'#ecdfc5'};
const clamp = x => Math.max(0, Math.min(1, x));
const ease = x => { x = clamp(x); return x*x*x*(x*(x*6-15)+10); };
const phase = (t, a, b) => ease((t-a)/(b-a));
const mix = (a, b, p) => a+(b-a)*p;

function text(c, value, x, y, size, color=P.ink, align='left') {
  c.fillStyle=color; c.font=`${size}px sans-serif`; c.textAlign=align; c.fillText(value,x,y);
}
function line(c, points, color=P.line, width=2) {
  c.beginPath(); c.moveTo(...points[0]); for(const p of points.slice(1)) c.lineTo(...p);
  c.strokeStyle=color; c.lineWidth=width; c.stroke();
}
function sheet(c, x, y, angle, fill, title, detail, unresolved=false) {
  c.save(); c.translate(x,y); c.rotate(angle);
  // Offset faces give depth without requiring a second renderer or 3D engine.
  c.fillStyle='#d3dbce'; c.strokeStyle=P.ink; c.lineWidth=1.3;
  c.beginPath(); c.moveTo(-105,-132); c.lineTo(-91,-145); c.lineTo(119,-145);
  c.lineTo(119,119); c.lineTo(105,132); c.closePath(); c.fill(); c.stroke();
  c.beginPath(); c.roundRect(-105,-132,210,264,7); c.fillStyle=fill; c.fill(); c.stroke();
  text(c,title,-82,-85,22);
  for(let i=0;i<4;i++) line(c,[[-82,-48+i*26],[i===3?30:75,-48+i*26]]);
  text(c,detail,-82,88,16,P.accent);
  if(unresolved) { c.beginPath(); c.arc(75,103,11,0,Math.PI*2); c.strokeStyle=P.accent; c.stroke(); text(c,'?',75,109,16,P.accent,'center'); }
  c.restore();
}

module.exports = {
  width,height,fps:60,duration:12,
  draw(c,t) {
    c.fillStyle=P.paper; c.fillRect(0,0,width,height);
    const open=phase(t,2,4.4), review=phase(t,5,6.8), gather=phase(t,8,10.2);
    const zoom=mix(mix(1.22,.88,open),1.12,gather);
    const center=mix(780,640,open)*(1-gather)+810*gather;
    const leftX=mix(mix(20,-300,open),18,gather);
    const rightX=mix(mix(38,300,review),36,gather);
    const leftY=mix(mix(-18,12,open),-18,gather);
    const rightY=mix(mix(-36,12,review),-36,gather);
    const angle=mix(0,-.055,open)*(1-gather);

    c.save(); c.translate(center,370); c.scale(zoom,zoom);
    // Each note has one owner through fan-out and reassembly.
    line(c,[[0,90],[leftX,leftY+90]],P.accent,1.5);
    line(c,[[0,90],[rightX,rightY+90]],P.line,1.5);
    sheet(c,rightX,rightY,-angle,P.sage,'Access','Condition unresolved',true);
    sheet(c,leftX,leftY,angle,P.gold,'Catalogue','Clarify missing detail',true);
    sheet(c,0,0,0,P.white,'Request','Original wording');
    c.restore();

    const first=1-phase(t,1.7,2.2), last=phase(t,9.8,10.4);
    c.save(); c.globalAlpha=first;
    text(c,'Keep the',75,220,47); text(c,'evidence.',75,276,47); c.restore();
    c.save(); c.globalAlpha=open*(1-gather);
    text(c,'Open the record. Preserve the unknowns.',640,95,36,P.ink,'center');
    text(c,'Catalogue details and access conditions remain distinct.',640,640,22,P.ink,'center');
    c.restore();
    c.save(); c.globalAlpha=last;
    text(c,'Return the decision',75,240,38); text(c,'with its notes.',75,290,38);
    text(c,'Unresolved does not mean approved.',75,365,21,P.accent);
    c.restore();
    text(c,'Illustrative workflow, not a performance measurement',640,695,16,'#6d7d85','center');
  },
};

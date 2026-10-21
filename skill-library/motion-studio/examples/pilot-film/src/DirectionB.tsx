import React from 'react';
import {P, Type, Eyebrow, PathDraw} from './Art';

/** Rejected consequence-first keyframe: the search question is the protagonist.
 * A distinct composition for art-direction comparison, not a second renderer.
 */
export const DirectionB:React.FC = () => {
 const scraps = [
  {x:1010,y:198,w:190,h:138,rot:-10,k:'FILING / 2021',active:false},
  {x:1266,y:133,w:197,h:146,rot:5,k:'REVENUE',active:false},
  {x:1508,y:210,w:222,h:157,rot:8,k:'Q2 2023',active:true},
  {x:1052,y:456,w:218,h:151,rot:5,k:'INVOICE',active:false},
  {x:1340,y:430,w:215,h:143,rot:-9,k:'ACME CORP',active:false},
  {x:1598,y:515,w:193,h:142,rot:9,k:'REPORT',active:false},
  {x:1024,y:740,w:197,h:127,rot:-6,k:'GROWTH',active:false},
  {x:1305,y:694,w:196,h:153,rot:7,k:'Q3 2022',active:false},
  {x:1594,y:772,w:204,h:135,rot:-7,k:'STATEMENT',active:false},
 ];
 return <svg width="100%" height="100%" viewBox="0 0 1920 1080" xmlns="http://www.w3.org/2000/svg">
  <rect width="1920" height="1080" fill={P.forest}/>
  <path d="M121 106H1810 M121 958H1810" stroke="#789287" strokeWidth="1.6"/>
  <Eyebrow x={126} y={80} dark>CONSEQUENCE-FIRST / DIRECTION B / REJECTED</Eyebrow>
  <Type x={120} y={285} size={90} serif fill={P.white}>The question is clear.</Type>
  <Type x={120} y={390} size={90} serif fill={P.white}>The evidence isn't.</Type>
  <Type x={126} y={471} size={30} fill="#CDDBCF">Where was ACME's Q2 2023 growth recorded?</Type>
  <circle cx="780" cy="619" r="157" fill="none" stroke="#B4CAC0" strokeDasharray="5 14" strokeWidth="2"/>
  <circle cx="780" cy="619" r="124" fill="#304940" stroke="#AAC1B4" strokeWidth="2.5"/>
  <Type x={780} y={592} size={22} fill="#BDD1C4" anchor="middle" spacing={1.7}>QUERY</Type>
  <Type x={780} y={636} size={48} serif fill={P.white} anchor="middle">ACME / Q2</Type>
  <Type x={780} y={668} size={18} fill="#BDD1C4" anchor="middle">REVENUE GROWTH</Type>
  {[0,1,2,3,4,5,6,7].map(i=>{
   const yy=205+i*88;
   const endX=1100+(i%3)*195, endY=yy+(i%2)*36;
   return <PathDraw key={i} d={'M913 619C1001 '+(558+(i-4)*52)+' 1010 '+endY+' '+endX+' '+endY}
     amount={1} color={i===2?P.coral:'#748F85'} width={i===2?3:1.6} opacity={i===2?.9:.48}/>;
  })}
  {scraps.map((s,i)=><g key={i} transform={'translate('+s.x+' '+s.y+') rotate('+s.rot+' '+s.w/2+' '+s.h/2+')'}>
    <rect width={s.w} height={s.h} fill={s.active?'#E4ECE5':'#F7F5EE'} stroke={s.active?P.coral:P.line} strokeWidth={s.active?4:1.5}/>
    <rect x="18" y="20" width="70" height="5" fill={s.active?P.rust:P.line}/>
    <rect x="18" y="37" width={s.w-38} height="2" fill={P.line}/>
    {[0,1,2].map(k=><path key={k} d={'M18 '+(60+k*14)+'H'+(s.w-23-k*7)} stroke={P.line} strokeWidth="2"/>)}
    <Type x={19} y={s.h-18} size={18} fill={s.active?P.rust:P.muted}>{s.k}</Type>
    {s.active&&<circle cx={s.w-22} cy="22" r="9" fill={P.rust}/>}
  </g>)}
  <Type x={127} y={852} size={28} serif fill={P.white}>A search-field-first metaphor.</Type>
  <Type x={127} y={892} size={22} fill="#BDCCC2">Rejected: the page's lost identity stays invisible.</Type>
  <Eyebrow x={127} y={1011} dark>STATIC ALTERNATE FRAME · CONCEPT TEST · NOT A PRODUCT RESULT</Eyebrow>
 </svg>;
};

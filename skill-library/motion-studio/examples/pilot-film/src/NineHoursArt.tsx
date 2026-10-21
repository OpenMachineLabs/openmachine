import React from 'react';

/**
 * Original vector materials for the Nine Hours of Autonomous Engineering film.
 * Physical engineering objects and figures are illustrative, not photographs or
 * instrument recordings from the internal repository described in the source.
 */
export const C={
  paper:'#F2F0E9',white:'#FFFEFA',ink:'#20352F',night:'#162A27',
  rust:'#CD7551',copper:'#BBA577',mint:'#D9EAE0',leaf:'#719B85',
  blue:'#72A8B8',paleBlue:'#DBEAF0',line:'#C6CCC1',
  mute:'#6B7770',cream:'#E9E6DC',fog:'#AFC3B6',warm:'#F9E7D9',
};
export const clamp=(n:number)=>Math.min(1,Math.max(0,n));
export const lerp=(a:number,b:number,n:number)=>a+(b-a)*n;
export const ease=(v:number)=>{const x=clamp(v);return x*x*(3-2*x)};
export const between=(f:number,a:number,b:number)=>ease((f-a)/(b-a));
export const over=(f:number,start:number,end:number,fadeIn=18,fadeOut=14)=>between(f,start,start+fadeIn)*(1-between(f,end-fadeOut,end));

export const Text:React.FC<{
 x:number;y:number;children:React.ReactNode;size?:number;fill?:string;weight?:number;
 serif?:boolean;spacing?:number;opacity?:number;anchor?:'start'|'end'|'middle';
}> = ({x,y,children,size=32,fill=C.ink,weight=400,serif=false,spacing=0,opacity=1,anchor='start'}) =>
 <text x={x} y={y} fill={fill} opacity={opacity} fontSize={size} fontWeight={weight}
  letterSpacing={spacing} textAnchor={anchor}
  fontFamily={serif?'Georgia, Times New Roman, serif':'Arial, Helvetica, sans-serif'}>{children}</text>;

export const Small:React.FC<{x:number;y:number;children:React.ReactNode;dark?:boolean;fill?:string}> =
 ({x,y,children,dark=false,fill})=>
 <Text x={x} y={y} fill={fill??(dark?C.fog:C.mute)} size={20} weight={700} spacing={2.8}>{children}</Text>;

export const Wire:React.FC<{
 d:string;amount?:number;stroke?:string;width?:number;opacity?:number;dash?:boolean
}> =({d,amount=1,stroke=C.leaf,width=3,opacity=1,dash=false})=>
 <path d={d} fill="none" stroke={stroke} strokeWidth={width} opacity={opacity}
  strokeDasharray={dash?'8 11':'2100'} strokeDashoffset={dash?0:2100*(1-clamp(amount))}
  strokeLinecap="round" strokeLinejoin="round"/>;

export const Dot:React.FC<{x:number;y:number;r?:number;fill?:string;opacity?:number}> =
 ({x,y,r=5,fill=C.rust,opacity=1})=><circle cx={x} cy={y} r={r} fill={fill} opacity={opacity}/>;

export const Glyph:React.FC<{x:number;y:number;kind:'chat'|'issue'|'paper'|'note'|'git'|'docs'|'test';scale?:number;stroke?:string}> =
 ({x,y,kind,scale=1,stroke=C.ink})=>{
 const basis='translate('+x+' '+y+') scale('+scale+')';
 return <g transform={basis} fill="none" stroke={stroke} strokeWidth={2.6} strokeLinecap="round" strokeLinejoin="round">
 {kind==='chat'&&<><rect x="3" y="4" width="48" height="34" rx="8"/><path d="M14 38L9 49L27 38M15 16H39M15 25H32"/></>}
 {kind==='issue'&&<><circle cx="27" cy="26" r="23"/><path d="M27 13V30M27 38V39"/></>}
 {kind==='paper'&&<><path d="M7 3H38L49 14V49H7Z M38 3V14H49 M15 23H40M15 32H40M15 40H33"/></>}
 {kind==='note'&&<><rect x="7" y="5" width="42" height="43" rx="2"/><path d="M17 5V1M29 5V1M40 5V1M14 20H40M14 30H37M14 40H33"/></>}
 {kind==='git'&&<><circle cx="12" cy="10" r="6"/><circle cx="45" cy="22" r="6"/><circle cx="12" cy="48" r="6"/><path d="M12 16V42M18 10H30V22H39"/></>}
 {kind==='docs'&&<><rect x="5" y="3" width="46" height="50" rx="3"/><path d="M15 16H39M15 26H42M15 36H40M15 45H30"/></>}
 {kind==='test'&&<><circle cx="27" cy="27" r="24"/><path d="M13 28L23 38L42 18"/></>}
 </g>;
};

export const Knowledge:React.FC<{
 x:number;y:number;label:string;kind:'chat'|'issue'|'paper'|'note'|'git'|'docs'|'test';
 scale?:number;angle?:number;opacity?:number;accent?:string
}> = ({x,y,label,kind,scale=1,angle=0,opacity=1,accent=C.blue}) =>
 <g transform={'translate('+x+' '+y+') rotate('+angle+' 117 52) scale('+scale+')'} opacity={opacity}>
   <rect x="7" y="8" width="232" height="104" rx="6" fill="#CBD4CA" opacity=".45"/>
   <rect x="0" y="0" width="232" height="104" rx="6" fill={C.white} stroke={C.line} strokeWidth="2"/>
   <rect x="0" y="0" width="10" height="104" rx="4" fill={accent}/>
   <Glyph x={20} y={23} kind={kind} scale={.9} stroke={accent}/>
   <Text x={86} y={45} size={22} weight={700} fill={C.ink}>{label}</Text>
   <path d="M86 61H204M86 74H180M86 87H197" stroke={C.line} strokeWidth="2"/>
 </g>;

export const Skill:React.FC<{x:number;y:number;scale?:number;opacity?:number;fill?:string;active?:number}> =
 ({x,y,scale=1,opacity=1,active=1})=>
 <g transform={'translate('+x+' '+y+') scale('+scale+')'} opacity={opacity}>
   <rect x="10" y="11" width="445" height="230" rx="13" fill="#A9BAB1" opacity=".5"/>
   <rect x="0" y="0" width="445" height="230" rx="13" fill={C.ink} stroke={C.leaf} strokeWidth="3"/>
   <rect x="18" y="20" width="409" height="191" rx="5" fill="#27493E" stroke="#719A83" strokeWidth="1.5"/>
   <path d="M33 74H412 M33 171H412" stroke="#668D79" strokeWidth="1.5"/>
   <Text x={40} y={54} size={20} spacing={3.4} fill="#A5C2B1" weight={700}>REUSABLE SKILL / 01</Text>
   <Text x={40} y={121} size={46} serif fill={C.white}>Engineering</Text>
   <Text x={40} y={158} size={24} fill="#BED4C7">context → procedure → tests</Text>
   {[0,1,2,3,4,5].map(i=><rect key={i} x={61+i*55} y="215" width="29" height="28" rx="2"
     fill={i<Math.round(active*6)?C.copper:'#667A6A'} stroke={C.ink} strokeWidth="1.5"/>)}
   <circle cx="395" cy="42" r="10" fill={active>.5?C.leaf:C.rust}/>
 </g>;

export const MiniChip:React.FC<{x:number;y:number;scale?:number;f?:number;active?:number;label?:string}> =
 ({x,y,scale=1,f=0,active=1,label='CUDA'})=>
 <g transform={'translate('+x+' '+y+') scale('+scale+')'}>
  <rect x="29" y="32" width="253" height="246" rx="13" fill="#31594B" opacity=".18"/>
  <rect x="19" y="18" width="244" height="244" rx="10" fill={C.ink} stroke={C.leaf} strokeWidth="3"/>
  {[0,1,2,3,4,5].map(i=><g key={i}>
   <path d={'M'+(49+i*32)+' 0V18M'+(49+i*32)+' 262V281'} stroke={C.copper} strokeWidth="8"/>
   <path d={'M0 '+(49+i*32)+'H19M263 '+(49+i*32)+'H283'} stroke={C.copper} strokeWidth="8"/>
  </g>)}
  <rect x="50" y="49" width="184" height="184" fill="#234136" stroke="#94B4A2" strokeWidth="2"/>
  <path d="M65 135H218M144 64V217" stroke={C.leaf} strokeWidth="2" opacity=".6"/>
  <circle cx="144" cy="140" r={40+3*Math.sin(f*.07)} fill="none" stroke={C.leaf} strokeWidth="2"/>
  <circle cx="144" cy="140" r="28" fill={C.rust} opacity={active}/>
  <Text x={144} y={145} size={20} fill={C.white} weight={700} anchor="middle">{label}</Text>
 </g>;

export const ComputeRack:React.FC<{x:number;y:number;scale?:number;f?:number;lit?:number;opacity?:number;label?:string}> =
 ({x,y,scale=1,f=0,lit=1,opacity=1,label='REMOTE GPU CLUSTER'})=>
 <g transform={'translate('+x+' '+y+') scale('+scale+')'} opacity={opacity}>
  <path d="M0 55L68 10L350 10L411 58V435L347 478H66L0 435Z" fill="#253F37" stroke="#6E9482" strokeWidth="3"/>
  <path d="M66 10V478M350 10V478M0 55H411M0 435H411" stroke="#8CAB9A" strokeWidth="2" opacity=".6"/>
  <path d="M350 10L411 58V435L350 478" fill="#39574A" stroke="#8CAB9A" strokeWidth="2"/>
  <rect x="86" y="50" width="240" height="396" rx="6" fill="#142A24"/>
  {[0,1,2,3,4,5,6].map(i=>{
   const yy=71+i*51;
   return <g key={i}>
    <rect x="102" y={yy} width="212" height="42" rx="3" fill="#294D3F" stroke="#6B907D"/>
    <path d={'M118 '+(yy+12)+'H204 M118 '+(yy+25)+'H186'} stroke="#7AA08A" strokeWidth="2"/>
    <circle cx="286" cy={yy+20} r={5+Math.sin(f*.06+i)*.7} fill={i/7<lit?C.leaf:'#466454'}/>
    <circle cx="270" cy={yy+20} r="3.5" fill={i/7<lit?C.copper:'#466454'}/>
   </g>;
  })}
  <path d="M84 463H326" stroke={C.copper} strokeWidth="5"/>
  <Text x={205} y={510} size={24} weight={700} fill={C.ink} anchor="middle">{label}</Text>
 </g>;

export const Laptop:React.FC<{x:number;y:number;scale?:number;opacity?:number;f?:number}> =
 ({x,y,scale=1,opacity=1,f=0})=><g transform={'translate('+x+' '+y+') scale('+scale+')'} opacity={opacity}>
  <path d="M46 0H415L439 19V269H24V19Z" fill="#355246" stroke="#7E9A8D" strokeWidth="3"/>
  <rect x="43" y="22" width="378" height="232" fill={C.night}/>
  <path d="M0 276H461L502 313H-39Z" fill="#8BA498" stroke="#557568" strokeWidth="3"/>
  <path d="M70 279H394" stroke="#C7D3C8" strokeWidth="3"/>
  <path d="M197 296H309" stroke={C.ink} strokeWidth="3"/>
  <Text x={64} y={63} size={18} fill={C.copper} spacing={2}>LOCAL TEST / CUDA</Text>
  {[0,1,2,3,4,5].map(i=><g key={i}>
   <rect x={65} y={90+i*25} width={i===4?186:i%2?262:298} height="8" rx="2"
     fill={i<3?C.blue:i===4?C.rust:C.leaf} opacity={.45+i*.045}/>
   <circle cx={381} cy={92+i*25} r="3.2" fill={i===4?C.rust:C.leaf}/>
  </g>)}
  <Text x={63} y={234} size={19} fill="#ADCDBA">SMOKE TEST  {f>0?'PASS':'READY'}</Text>
 </g>;

export const SoundTape:React.FC<{x:number;y:number;f:number;w?:number;rows?:number;opacity?:number}> =
 ({x,y,f,w=1510,rows=9,opacity=1})=>
 <g transform={'translate('+x+' '+y+')'} opacity={opacity}>
  {[...Array(rows)].map((_,row)=>{
   const points:string[]=[];
   for(let px=0;px<=w;px+=7){
    const env=.25+.6*Math.pow(Math.sin(px*.006+row*.3),2);
    const amp=(9+row*1.7)*env;
    const dy=amp*Math.sin(px*.055+row*.7+f*.045)+3*Math.sin(px*.107-row*.4-f*.025);
    points.push((px?'L':'M')+px+' '+dy.toFixed(2));
   }
   const yy=row*48;
   return <g key={row}>
    <path d={'M0 '+yy+'H'+w} stroke={C.line} strokeWidth="1.5"/>
    <path d={points.join(' ')} transform={'translate(0 '+yy+')'} fill="none" stroke={row%3===0?C.rust:row%3===1?C.blue:C.leaf}
      strokeWidth={row%3===0?2.8:2} opacity={.78}/>
   </g>;
  })}
 </g>;

export const Utilization:React.FC<{x:number;y:number;f:number;value:number;label:string;color:string;amount:number}> =
 ({x,y,f,value,label,color,amount})=>{
 const width=930;
 return <g transform={'translate('+x+' '+y+')'}>
  <Text x={0} y={0} size={28} fill="#C7D7CC" weight={700}>{label}</Text>
  <Text x={-2} y={91} size={87} serif fill={C.white}>{value}%</Text>
  <rect x="300" y={24} width={width} height="67" fill="#35554A"/>
  <rect x="300" y={24} width={width*(value/100)*clamp(amount)} height="67" fill={color}/>
  <path d={'M300 24H'+(300+width)+'M300 91H'+(300+width)} stroke="#8EB09C" strokeWidth="1.5" opacity=".5"/>
  {Array.from({length:11},(_,i)=>i).map(i=><path key={i} d={'M'+(300+width*i/10)+' 96V116'} stroke="#98B5A4" strokeWidth="2"/>)}
 </g>;
};

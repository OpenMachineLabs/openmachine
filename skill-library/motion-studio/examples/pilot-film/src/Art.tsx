import React from 'react';

export const P = {
  paper:'#F4F1E9', warm:'#E6E0D3', white:'#FFFEFA', ink:'#20342E', muted:'#69756E',
  line:'#C9CDC3', forest:'#1B2E28', rust:'#CB6A47', coral:'#E59A79', sage:'#769687',
  mint:'#DDEAE0', sky:'#89ADBB', blue:'#4E8191', paleBlue:'#E0EAED', yellow:'#DBC79B',
};
export const clip=(n:number)=>Math.max(0,Math.min(1,n));
export const mix=(a:number,b:number,t:number)=>a+(b-a)*t;
export const smooth=(t:number)=>{const x=clip(t);return x*x*(3-2*x);};
export const reveal=(f:number,a:number,b:number)=>smooth((f-a)/(b-a));
export const fadeBand=(f:number,start:number,end:number,fi=16,fo=16)=>reveal(f,start,start+fi)*(1-reveal(f,end-fo,end));
const serif='Georgia, Times New Roman, serif';
const sans='Arial, Helvetica, sans-serif';

type TypeProps = {
  x:number;y:number;children:React.ReactNode;size?:number;fill?:string;serif?:boolean;
  weight?:number;spacing?:number;opacity?:number;anchor?:'start'|'middle'|'end';
};
export const Type:React.FC<TypeProps> = ({x,y,children,size=28,fill=P.ink,serif:useSerif=false,weight=400,spacing=0,opacity=1,anchor='start'}) =>
 <text x={x} y={y} fontFamily={useSerif?serif:sans} fontSize={size} fontWeight={weight} letterSpacing={spacing}
   fill={fill} opacity={opacity} textAnchor={anchor}>{children}</text>;

export const PathDraw:React.FC<{d:string;amount?:number;color?:string;width?:number;dashed?:boolean;opacity?:number}> =
 ({d,amount=1,color=P.sage,width=2.5,dashed=false,opacity=1}) =>
 <path d={d} fill="none" stroke={color} strokeWidth={width} opacity={opacity}
   strokeDasharray={dashed?'8 9':'1800'} strokeDashoffset={dashed?0:1800*(1-clip(amount))}
   strokeLinecap="round" strokeLinejoin="round"/>;

export const Eyebrow:React.FC<{x:number;y:number;children:React.ReactNode;dark?:boolean;fill?:string}> =
 ({x,y,children,dark=false,fill}) => <Type x={x} y={y} size={20} fill={fill??(dark?'#B0C2B7':P.muted)} spacing={3.2} weight={700}>{children}</Type>;

export const PaperCorners:React.FC<{x:number;y:number;w:number;h:number;fill?:string}> =
 ({x,y,w,h,fill=P.line})=><g stroke={fill} strokeWidth={1.6} fill="none" opacity=".9">
 <path d={'M'+(x-13)+' '+(y+25)+' V'+y+' H'+(x+25)} />
 <path d={'M'+(x+w-25)+' '+y+' H'+(x+w+13)+' V'+(y+25)} />
 <path d={'M'+(x-13)+' '+(y+h-25)+' V'+(y+h)+' H'+(x+25)} />
 <path d={'M'+(x+w-25)+' '+(y+h)+' H'+(x+w+13)+' V'+(y+h-25)} />
 </g>;

export const Dot:React.FC<{x:number;y:number;r?:number;color?:string;opacity?:number}> =
 ({x,y,r=5,color=P.rust,opacity=1})=><circle cx={x} cy={y} r={r} fill={color} opacity={opacity}/>;

export const Archive:React.FC<{x:number;y:number;scale?:number;turn?:number;emphasis?:number;opacity?:number;f?:number}> =
 ({x,y,scale=1,turn=0,emphasis=1,opacity=1})=>{
  const lines=[0,1,6]; // Keep ruled texture away from the quoted excerpt.
  return <g transform={'translate('+x+' '+y+') rotate('+turn+' 350 335) scale('+scale+')'} opacity={opacity}>
    <rect x="22" y="35" width="694" height="686" rx="5" fill="#DADDD1" stroke="#B1B6A9"/>
    <rect x="9" y="18" width="694" height="686" rx="4" fill="#EAEBE4" stroke="#B1B6A9"/>
    <rect x="0" y="0" width="694" height="686" rx="4" fill={P.white} stroke="#B4BFB2" strokeWidth="2"/>
    <rect x="0" y="0" width="694" height="12" fill={P.forest}/>
    <Type x={46} y={78} size={18} spacing={3.4} fill={P.muted}>ARCHIVE NO. 004 / SEC FILING</Type>
    <Type x={46} y={146} size={52} serif>ACME CORP.</Type>
    <Type x={48} y={185} size={24} fill={P.muted}>Q2 2023  ·  FINANCIAL SUMMARY</Type>
    <path d="M46 211H644" stroke={P.ink} strokeWidth="2"/>
    <Type x={48} y={258} size={19} fill={P.muted} spacing={2.2}>04.2    PERFORMANCE &amp; GROWTH</Type>
    {lines.map(i=>{
      const wide=[590,526,567,485,588,530,569][i];
      const yy=300+i*36;
      return <path key={i} d={'M48 '+yy+' H'+(48+wide)} stroke={P.line} strokeWidth={i===3?3:2} opacity=".8"/>;
    })}
    <rect x="36" y="359" width="621" height="128" fill={P.rust} opacity={0.065*emphasis}/>
    <path d="M36 359H657 M36 487H657" stroke={P.rust} strokeWidth={2} opacity={emphasis}/>
    <circle cx="46" cy="421" r="5" fill={P.rust} opacity={emphasis}/>
    <Type x={62} y={413} size={26} serif>The company's revenue grew by 3%</Type>
    <Type x={62} y={450} size={26} serif>over the previous quarter.</Type>
    <path d="M46 523H644" stroke={P.line}/>
    <Type x={46} y={561} size={20} fill={P.muted}>Source document / illustration only</Type>
    <path d="M46 594H307 M340 594H648 M46 621H276 M312 621H634" stroke={P.line} strokeWidth="2"/>
    <Type x={46} y={662} size={18} fill={P.muted} spacing={1}>DOC-ID ACME-Q2-2023</Type>
    <Type x={643} y={662} size={18} fill={P.muted} anchor="end">PAGE 4 / 12</Type>
    <path d="M610 38L657 85 M657 38L610 85" stroke={P.yellow} strokeWidth="2.2" opacity=".52"/>
  </g>;
};

export const Fragment:React.FC<{x:number;y:number;scale?:number;context?:number;opacity?:number;angle?:number;compact?:boolean;highlight?:number}> =
 ({x,y,scale=1,context=0,opacity=1,angle=0,compact=false,highlight=0})=>{
  const c=clip(context),height=compact?235:267+100*c;
  const body=compact?108:148;
  return <g transform={'translate('+x+' '+y+') rotate('+angle+' 365 135) scale('+scale+')'} opacity={opacity}>
    <rect x="12" y="11" width="742" height={height} fill="#D2D9D0" opacity=".45"/>
    <rect x="0" y="0" width="742" height={height} fill={P.white} stroke="#AEB9AD" strokeWidth="2"/>
    <path d={'M0 0H742 M0 '+height+'H742'} stroke={P.ink} strokeWidth="2"/>
    <rect x="0" y="0" width="9" height={height} fill={P.rust} opacity={1-c}/>
    <rect x="0" y="0" width="9" height={height} fill={P.sage} opacity={c}/>
    {[40,80,120,160,200].map(yy=><circle key={yy} cx="26" cy={yy} r="3" fill={P.paper} stroke={P.line}/>)}
    <rect x="38" y="0" width="704" height={100*c} fill={P.mint} opacity=".95"/>
    <path d={'M38 '+(99*c)+'H742'} stroke={P.sage} strokeWidth="2" opacity={c}/>
    <g opacity={c}>
      <Type x={58} y={34} size={17} spacing={2.1} fill={P.sage} weight={700}>GENERATED CONTEXT / PREPENDED</Type>
      <Type x={58} y={77} size={29} fill={P.forest} weight={700}>SEC filing  ·  ACME Corp  ·  Q2 2023</Type>
      <path d="M634 50H697" stroke={P.sage} strokeWidth="2" />
      <path d="M685 44L699 50L685 56" fill="none" stroke={P.sage} strokeWidth="2"/>
    </g>
    <Type x={56} y={body+100*c-60} size={compact?32:35} serif>“The company's revenue grew by 3%</Type>
    <Type x={56} y={body+100*c-11} size={compact?30:35} serif>over the previous quarter.”</Type>
    <path d={'M48 '+(body+100*c+19)+'H705'} stroke={P.line}/>
    <Type x={55} y={body+100*c+57} size={19} fill={P.muted} spacing={1.9}>EXCERPT 04 / 12  ·  ILLUSTRATIVE</Type>
    <circle cx="675" cy={body+100*c+43} r="11" fill={highlight?P.sage:P.paper} stroke={highlight?P.sage:P.line} strokeWidth="2"/>
    <path d={'M670 '+(body+100*c+43)+' l4 4 l8 -9'} fill="none" stroke={highlight?P.white:P.muted} strokeWidth="2"/>
  </g>;
};

export const MiniFragments:React.FC<{x:number;y:number;count?:number;highlight?:number;fade?:number}> =
 ({x,y,count=8,highlight=2,fade=1})=><g transform={'translate('+x+' '+y+')'} opacity={fade}>
 {Array.from({length:count},(_,i)=>{
   const row=Math.floor(i/4),col=i%4,xx=col*137+(row%2?15:0),yy=row*102;
   const active=i===highlight;
   return <g key={i} transform={'translate('+xx+' '+yy+') rotate('+((i%3)-1)*1.8+' 56 40)'}>
     <rect x="0" y="0" width="120" height="84" fill={active?'#F6E5DE':P.white} stroke={active?P.rust:P.line} strokeWidth={active?2:1}/>
     <rect x="10" y="13" width="48" height="4" fill={P.line}/><rect x="10" y="26" width="94" height="3" fill={P.line}/>
     <rect x="10" y="39" width={70+(i%3)*9} height="3" fill={P.line}/><rect x="10" y="52" width="95" height="3" fill={P.line}/>
     {active&&<circle cx="107" cy="12" r="5" fill={P.rust}/>}
   </g>;
 })}
 </g>;

const wave=(j:number,f:number,width=620)=>{
 const points:string[]=[];
 for(let x=0;x<=width;x+=8){
  const y=113+j*11+24*Math.sin(x/49+j*.63+f*.026)*Math.exp(-Math.pow((x-width*.52)/(width*.72),2))
   +12*Math.sin(x/27+j*.29-f*.014);
  points.push((x===0?'M':'L')+x.toFixed(0)+' '+y.toFixed(1));
 }
 return points.join(' ');
};
export const SemanticField:React.FC<{x:number;y:number;f:number;progress?:number;opacity?:number}> =
 ({x,y,f,progress=1,opacity=1})=><g transform={'translate('+x+' '+y+')'} opacity={opacity}>
   <Type x={0} y={0} size={21} fill={P.blue} weight={700} spacing={2.5}>01 / CONTEXTUAL EMBEDDINGS</Type>
   <Type x={0} y={62} size={48} serif>Meaning</Type>
   <path d="M0 92H620" stroke={P.line}/>
   {[0,1,2,3,4,5,6,7].map(i=><path key={i} d={wave(i,f)} fill="none" stroke={i%3===0?P.rust:i%3===1?P.blue:P.sage}
     opacity={(.24+(i%3)*.1)*clip(progress)} strokeWidth={i%3===0?2.7:1.8} strokeLinejoin="round"
     strokeDasharray="820" strokeDashoffset={820*(1-clip(progress))}/>)}
   {[0,1,2,3,4].map(i=><circle key={i} cx={77+i*113} cy={120+i*4+18*Math.sin(i*.9+f*.025)} r={6+(i%2)*2}
     fill={i%2?P.blue:P.sage} stroke={P.white} strokeWidth="2" opacity={progress}/>)}
   <path d="M0 251H620" stroke={P.line}/>
   <Type x={2} y={287} size={20} fill={P.muted}>Semantic similarity · illustrative field</Type>
 </g>;

export const LexicalIndex:React.FC<{x:number;y:number;f:number;progress?:number;opacity?:number}> =
 ({x,y,f,progress=1,opacity=1})=>{
 const tokens=[['ACME',.92],['Q2',.75],['2023',1],['revenue',.84],['growth',.62]];
 return <g transform={'translate('+x+' '+y+')'} opacity={opacity}>
   <Type x={0} y={0} size={21} fill={P.rust} weight={700} spacing={2.5}>02 / CONTEXTUAL BM25</Type>
   <Type x={0} y={62} size={48} serif>Exact words</Type>
   <path d="M0 92H630" stroke={P.line}/>
   {tokens.map(([token,val],i)=>{const yy=126+i*42,p=clip((progress-i*.05)/.75);return <g key={String(token)}>
      <Type x={2} y={yy} size={23} fill={i<3?P.rust:P.ink} weight={i<3?700:400}>{String(token)}</Type>
      <rect x="190" y={yy-19} width="410" height="12" fill="#E9E8E0" rx="2"/>
      <rect x="190" y={yy-19} width={410*Number(val)*p} height="12" fill={i<3?P.rust:P.sky} rx="2"/>
      <circle cx={190+410*Number(val)*p} cy={yy-13} r="4" fill={P.white} stroke={P.ink}/>
   </g>;})}
   <path d="M0 335H620" stroke={P.line}/>
   <Type x={2} y={371} size={20} fill={P.muted}>Token matching · illustrative indexes</Type>
 </g>;
};

export const IsometricIndex:React.FC<{x:number;y:number;f:number;color?:string;opacity?:number}> =
 ({x,y,f,color=P.sage,opacity=1}) => <g transform={'translate('+x+' '+y+')'} opacity={opacity}>
  {[0,1,2,3].map(i=><g key={i} transform={'translate(0 '+(-i*20)+')'}>
    <path d={'M30 '+(85+i*7)+'L170 '+(28+i*7)+'L342 '+(82+i*7)+'L197 '+(141+i*7)+'Z'} fill={i%2?'#E3E8E0':'#F8F6EF'} stroke={color} strokeWidth="1.6"/>
    {Array.from({length:8},(_,j)=><path key={j} d={'M'+(72+j*18)+' '+(85+i*7-j*1.7)+'l78 21'} stroke={color} strokeWidth=".8" opacity=".46"/>)}
  </g>)}
  <circle cx="344" cy="81" r="10" fill={color}/><circle cx="344" cy="81" r={15+3*Math.sin(f*.05)} fill="none" stroke={color} opacity=".5"/>
 </g>;
import React from 'react';
import {AbsoluteFill,Audio,staticFile,useCurrentFrame} from 'remotion';
import {
 C,Text,Small,Wire,Dot,Knowledge,Skill,MiniChip,ComputeRack,Laptop,SoundTape,
 Utilization,clamp,lerp,between,over
} from './NineHoursArt';

/**
 * "Nine Hours of Autonomous Engineering" — an illustrated case-study film.
 * Timeline is frame-driven at 30 fps. No stock media or undocumented metrics.
 * Source: https://luozhu.io/blogs/2026/10/21/nine-hours-of-autonomous-engineering.html
 */
export const NineHoursFilm:React.FC=()=>{
 const f=useCurrentFrame();
 const s0=over(f,0,146,10,22);
 const s1=over(f,128,310,22,22);
 const s2=over(f,292,504,24,20);
 const s3=over(f,486,704,20,21);
 const s4=over(f,685,866,21,20);
 const s5=over(f,846,1045,22,20);
 const s6=over(f,1023,1188,20,23);
 const s7=between(f,1160,1193);
 const gather=between(f,342,442);
 const connect=between(f,535,646);
 const research=between(f,713,825);
 const deployed=between(f,880,997);
 const measured=between(f,1055,1132);

 const contextData:[
   string,
   'chat'|'issue'|'paper'|'note'|'git'|'docs'|'test',
   number,number,number,string
 ][]=[
  ['TEAM NOTES','chat',118,378,-7,C.blue],
  ['DESIGN PRS','git',436,334,5,C.rust],
  ['ISSUES','issue',266,566,-5,C.rust],
  ['PAPERS','paper',98,722,4,C.leaf],
  ['EXPERIMENTS','note',500,736,8,C.blue],
 ];

 return <AbsoluteFill style={{background:C.paper}}>
  <Audio src={staticFile('nine-hours-score.wav')} volume={0.66}/>
  <svg width="100%" height="100%" viewBox="0 0 1920 1080" xmlns="http://www.w3.org/2000/svg">
   <defs>
    <pattern id="work-grid" width="84" height="84" patternUnits="userSpaceOnUse">
     <path d="M84 0H0V84" stroke={C.line} strokeWidth="1" fill="none" opacity=".33"/>
     <circle cx="0" cy="0" r="2.2" fill={C.leaf} opacity=".27"/>
    </pattern>
    <pattern id="dark-grid" width="89" height="89" patternUnits="userSpaceOnUse">
     <path d="M89 0H0V89" stroke="#789A87" strokeWidth="1" fill="none" opacity=".24"/>
    </pattern>
   </defs>

   {/* 00—04.8: a nine-hour clock, not an unrelated title animation. */}
   <g opacity={s0}>
    <rect width="1920" height="1080" fill={C.night}/>
    <rect width="1920" height="1080" fill="url(#dark-grid)" opacity=".45"/>
    <Small x={122} y={102} dark>AN ENGINEERING FIELD REPORT / OCT 21 2026</Small>
    <path d="M122 135H1780" stroke="#526D60" strokeWidth="2"/>
    <Text x={121} y={386} size={160} serif fill={C.white} opacity={between(f,2,35)}>NINE HOURS.</Text>
    <Text x={127} y={473} size={45} fill="#C3D4C9" opacity={between(f,26,62)}>One autonomous engineering run.</Text>
    <Text x={127} y={551} size={28} fill="#90A99B" opacity={between(f,46,74)}>
      Research. Kernels. GPU experiments. Deployment validation.
    </Text>
    <g transform="translate(1485 513)">
     <circle r="271" fill="#254438" stroke="#86AD96" strokeWidth="2"/>
     <circle r="227" fill="none" stroke="#638674" strokeWidth="1.5" strokeDasharray="2 14"/>
     {Array.from({length:9},(_,i)=>{
      const a=-Math.PI/2+i*Math.PI*2/9;
      return <g key={i}>
        <path d={'M'+(Math.cos(a)*243)+' '+(Math.sin(a)*243)+'L'+(Math.cos(a)*259)+' '+(Math.sin(a)*259)}
          stroke={i<6?C.blue:C.rust} strokeWidth={7} strokeLinecap="round"/>
        <Dot x={Math.cos(a)*210} y={Math.sin(a)*210} r={i<6?4:7} fill={i<6?C.blue:C.rust}/>
      </g>;
     })}
     <circle r="173" fill={C.night} stroke="#7FA891" strokeWidth="1.5"/>
     <path d="M0 -159A159 159 0 1 1 -137 81" stroke={C.leaf} strokeWidth="15" opacity=".8" fill="none"/>
     <path d="M-137 81A159 159 0 0 1 0 -159" stroke={C.rust} strokeWidth="15" fill="none"/>
     <Text x={0} y={29} size={126} fill={C.white} serif anchor="middle">9h</Text>
     <Text x={0} y={88} size={22} fill="#ADC4B4" anchor="middle" spacing={2.5}>ONE REPORTED RUN</Text>
    </g>
    <path d="M124 914H1755" stroke="#4D695B"/>
    <Small x={128} y={969} dark>OPEN MACHINE  /  CONTEXT + SKILLS + EXECUTION</Small>
    <Text x={128} y={1020} size={21} fill="#8FA99B">Internal case study · details and results reported by the author</Text>
   </g>

   {/* 04.2—10.3: the real-scale audio pipeline and its GPU pressure. */}
   <g opacity={s1}>
    <rect width="1920" height="1080" fill={C.paper}/>
    <rect width="1920" height="1080" fill="url(#work-grid)" opacity=".26"/>
    <Small x={128} y={107}>01 / THE SCALE OF THE WORK</Small>
    <Text x={126} y={222} size={93} serif>10M+ hours of audio.</Text>
    <Text x={131} y={292} size={31} fill={C.mute}>A multilingual processing pipeline spanning multiple GPU systems.</Text>
    <SoundTape x={135} y={357} f={f} w={1210} rows={9} opacity={between(f,146,187)}/>
    <rect x="135" y="847" width="1270" height="2" fill={C.line}/>
    {['FILTER','CLASSIFY','SCORE','INFER','PACKAGE'].map((name,i)=>{
     const xx=153+i*254;
     const a=between(f,172+i*13,199+i*13);
     return <g key={name} opacity={a}>
       <circle cx={xx+9} cy="883" r="7" fill={i===4?C.rust:C.blue}/>
       <Text x={xx+33} y={891} size={23} weight={700} spacing={1.8}>{name}</Text>
       {i<4&&<path d={'M'+(xx+191)+' 882H'+(xx+238)} stroke={C.leaf} strokeWidth="2.5"/>}
     </g>;
    })}
    <ComputeRack x={1463} y={373} scale={.72} f={f} lit={.40+(f-145)*.0013} opacity={between(f,183,220)} label="DATA GPU WORKERS"/>
    <path d="M136 948H1386" stroke={C.line}/>
    <Text x={134} y={996} size={29} serif>Optimizing this system takes more than writing one function.</Text>
    <Small x={128} y={1041}>THE SOURCE DESCRIBES A SINGLE COMPLEX INTERNAL REPOSITORY</Small>
   </g>

   {/* 09.7—16.8: the real mechanism: context becomes an evaluated Skill. */}
   <g opacity={s2}>
    <rect width="1920" height="1080" fill={C.paper}/>
    <rect width="1920" height="1080" fill="url(#work-grid)" opacity=".23"/>
    <Small x={124} y={103}>02 / ENGINEERING CONTEXT</Small>
    <Text x={120} y={215} size={88} serif>Experience is scattered.</Text>
    <Text x={124} y={287} size={32} fill={C.mute}>Turn papers, discussions, code history and experiments into something reusable.</Text>
    {contextData.map(([label,kind,x,y,angle,accent],i)=>{
      const a=between(f,308+i*11,337+i*11);
      const ex=lerp(x,1017,gather);
      const ey=lerp(y,461+(i-2)*7,gather);
      return <Knowledge key={label} x={ex} y={ey} label={label} kind={kind}
        angle={lerp(angle,0,gather)} accent={accent}
        opacity={a*(1-between(f,411+i*4,461+i*4))} scale={lerp(1,.38,gather)}/>;
    })}
    <Wire d="M706 575C840 575 934 540 1050 540" amount={gather} stroke={C.rust} width={3}/>
    {[0,1,2,3,4].map(i=><Dot key={i} x={lerp(760+i*44,1060,gather)} y={548+(i-2)*6}
      fill={i%2?C.rust:C.blue} r={6} opacity={between(f,364+i*9,389+i*9)*(1-gather)}/>)}
    <Skill x={1032} y={431} scale={1.13} opacity={between(f,381,433)} active={gather}/>
    <Text x={1043} y={771} size={27} fill={C.leaf} weight={700} opacity={between(f,421,456)}>
      DISTILLED SKILL  ·  CHECKED AGAINST REAL TASKS
    </Text>
    <path d="M125 863H1798" stroke={C.line} strokeWidth="2"/>
    <Text x={130} y={925} size={41} serif opacity={between(f,419,453)}>Context becomes executable engineering experience.</Text>
    <Small x={130} y={1006}>AN ILLUSTRATED MODEL OF THE AUTHOR'S INTERNAL PROCESS</Small>
   </g>

   {/* 16.2—23.4: agent loop is separate from context/execution transport. */}
   <g opacity={s3}>
    <rect width="1920" height="1080" fill={C.night}/>
    <rect width="1920" height="1080" fill="url(#dark-grid)" opacity=".30"/>
    <Small x={124} y={104} dark>03 / SEPARATE INTELLIGENCE FROM INFRASTRUCTURE</Small>
    <Text x={122} y={213} size={82} serif fill={C.white}>Agent loop. Real machines.</Text>
    <Text x={129} y={276} size={29} fill="#AEC6B6">The model plans. Open Machine provides context and bounded execution.</Text>

    <g opacity={between(f,490,520)}>
     <rect x="123" y="364" width="474" height="233" rx="11" fill="#2B483C" stroke="#8AAF99" strokeWidth="2"/>
     <Small x={159} y={410} dark>AGENT LOOP</Small>
     <Text x={158} y={474} size={46} serif fill={C.white}>Plan → Act → Check</Text>
     <Text x={163} y={536} size={23} fill="#BDD0C3">Research · tools · revisions</Text>
     {[0,1,2].map(i=><circle key={i} cx={181+i*76} cy="568" r={7}
       fill={i<Math.round((f-490)/35)?C.leaf:'#527466'}/>)}
    </g>
    <Wire d="M605 480C685 480 694 480 755 480" stroke={C.copper} amount={connect} width={4}/>
    <g opacity={between(f,506,550)}>
     <rect x="751" y="386" width="433" height="191" rx="9" fill="#3D6050" stroke="#B7C6B0" strokeWidth="3"/>
     <Small x={786} y={423} dark>OPEN MACHINE</Small>
     <Text x={786} y={490} size={46} fill={C.white} serif>Context + Execution</Text>
     <Text x={787} y={539} size={22} fill="#BCD0C1">Internal Skills · managed machines</Text>
    </g>
    <Skill x={755} y={657} scale={.63} active={1} opacity={between(f,534,574)}/>
    <Wire d="M971 579V655" stroke={C.leaf} amount={connect} width={3.2}/>
    <Wire d="M1186 484C1285 484 1293 447 1390 447" stroke={C.blue} amount={connect} width={4}/>
    <Wire d="M1186 504C1300 545 1312 733 1448 733" stroke={C.rust} amount={between(f,563,658)} width={4}/>
    <Laptop x={1400} y={355} scale={.65} opacity={between(f,554,605)} f={f}/>
    <ComputeRack x={1475} y={632} scale={.53} lit={.71} opacity={between(f,580,645)} f={f} label="REMOTE CLUSTER"/>
    <Text x={1403} y={605} size={23} fill="#C3D5CA" opacity={connect}>LOCAL TEST GPU</Text>
    <Small x={130} y={996} dark>INTERNAL OM SETUP · ILLUSTRATIVE CONNECTIONS · NOT A PUBLIC FEATURE DEMO</Small>
   </g>

   {/* 22.8—28.9: approximately six hours of grounded research. */}
   <g opacity={s4}>
    <rect width="1920" height="1080" fill={C.paper}/>
    <rect width="1920" height="1080" fill="url(#work-grid)" opacity=".20"/>
    <Small x={122} y={106}>04 / RESEARCH PHASE</Small>
    <Text x={120} y={222} size={92} serif>Six hours of research.</Text>
    <Text x={128} y={286} size={30} fill={C.mute}>Papers, source discussions and existing engineering decisions.</Text>
    <g transform="translate(436 674)">
     <circle r="266" fill="#E6EAE1" stroke={C.line} strokeWidth="2"/>
     <circle r="223" fill="none" stroke={C.line} strokeWidth="2" strokeDasharray="6 13"/>
     <circle r="179" fill={C.white} stroke={C.line} strokeWidth="2"/>
     {[0,1,2,3,4,5,6,7,8].map(i=>{
       const a=-Math.PI/2+i*2*Math.PI/9;
       const active=i<6;
       return <path key={i} d={'M'+(Math.cos(a)*203)+' '+(Math.sin(a)*203)+'L'+(Math.cos(a)*251)+' '+(Math.sin(a)*251)}
        stroke={active?C.blue:C.rust} strokeWidth={active?22:12}
        strokeLinecap="round" opacity={active?(research*(.73+i*.035)):.32}/>;
     })}
     <Text x={0} y={-5} size={104} serif anchor="middle">6h</Text>
     <Text x={0} y={51} size={24} fill={C.mute} anchor="middle" spacing={2}>REPORTED</Text>
    </g>
    <Knowledge x={903} y={433} label="PAPERS" kind="paper" scale={1.1} accent={C.blue} opacity={between(f,699,746)}/>
    <Knowledge x={1231} y={563} label="TEAM NOTES" kind="chat" scale={1.1} accent={C.leaf} opacity={between(f,721,771)}/>
    <Knowledge x={1022} y={715} label="PR HISTORY" kind="git" scale={1.1} accent={C.rust} opacity={between(f,741,784)}/>
    <Wire d="M866 630C951 628 1050 580 1150 570" stroke={C.blue} amount={research}/>
    <Wire d="M862 696C979 738 1034 767 1120 768" stroke={C.rust} amount={research}/>
    <Wire d="M1373 694C1460 684 1548 651 1680 650" stroke={C.leaf} amount={research}/>
    <Skill x={1410} y={786} scale={.69} opacity={between(f,764,817)}/>
    <Small x={128} y={1020}>ONE REPORTED RUN · APPROXIMATE PHASE DURATIONS FROM THE ARTICLE</Small>
   </g>

   {/* 28.2—34.9: kernel, local smoke test and remote experiment feedback. */}
   <g opacity={s5}>
    <rect width="1920" height="1080" fill={C.paper}/>
    <rect width="1920" height="1080" fill="url(#work-grid)" opacity=".20"/>
    <Small x={120} y={105}>05 / BUILD · TEST · VALIDATE</Small>
    <Text x={119} y={217} size={89} serif>Three hours of iteration.</Text>
    <Text x={127} y={284} size={30} fill={C.mute}>CUDA kernels. Local smoke tests. Remote GPU experiments.</Text>
    <MiniChip x={147} y={405} scale={.85} f={f} active={between(f,869,912)} label="CUDA"/>
    <Text x={151} y={755} size={28} fill={C.ink} weight={700}>CUSTOM KERNEL</Text>
    <Wire d="M436 574C542 574 610 573 702 573" stroke={C.rust} width={4} amount={deployed}/>
    <Laptop x={679} y={402} scale={.81} opacity={between(f,863,914)} f={1}/>
    <Text x={722} y={770} size={28} fill={C.ink} weight={700}>LOCAL SMOKE TEST</Text>
    <Wire d="M1110 575C1188 575 1231 553 1305 553" stroke={C.blue} width={4} amount={deployed}/>
    <ComputeRack x={1296} y={356} scale={.87} f={f} lit={deployed} opacity={between(f,899,954)} label="REMOTE EXPERIMENTS"/>
    <Wire d="M1593 817C1495 938 774 931 468 798" stroke={C.leaf} width={3.5} amount={between(f,922,997)}/>
    <Dot x={lerp(1594,470,between(f,946,1003))} y={lerp(817,798,between(f,946,1003))} r={9} fill={C.rust} opacity={between(f,937,963)}/>
    <rect x="137" y="909" width="1400" height="2" fill={C.line}/>
    <Text x={136} y={969} size={35} serif>Observe the result. Adjust. Run it again.</Text>
    <Small x={140} y={1036}>THE ARTICLE REPORTS NO HUMAN ENGINEER INTERVENTION IN THIS RUN</Small>
   </g>

   {/* 34.1—39.6: a calibrated chart, not a fabricated dashboard. */}
   <g opacity={s6}>
    <rect width="1920" height="1080" fill={C.night}/>
    <rect width="1920" height="1080" fill="url(#dark-grid)" opacity=".32"/>
    <Small x={126} y={105} dark>06 / AUTHOR-REPORTED RESULT · ONE INTERNAL REPOSITORY</Small>
    <Text x={120} y={217} size={87} serif fill={C.white}>Average GPU utilization.</Text>
    <Text x={128} y={285} size={29} fill="#BBD0C2">Data processing after the nine-hour autonomous engineering run</Text>
    <Utilization x={153} y={475} f={f} value={43} label="BEFORE" color={C.rust} amount={measured}/>
    <Utilization x={153} y={735} f={f} value={93} label="AFTER" color={C.leaf} amount={measured}/>
    <path d="M448 864H1710" stroke="#6D8B7A" strokeWidth="2"/>
    {[0,1,2,3,4,5].map(i=>{
      const xx=453+i*186;
      return <Text key={i} x={xx} y={905} size={20} fill="#AFC5B6" anchor="middle">{i*20}%</Text>;
    })}
    <Text x={1762} y={997} size={62} fill="#D8E9DD" serif anchor="end" opacity={between(f,1090,1144)}>
      +50 percentage points
    </Text>
    <Text x={129} y={1027} size={20} fill="#A8BEAF">
      Source: luozhu.io · 2026-10-21 · author-reported before/after, not a controlled benchmark
    </Text>
   </g>

   {/* 38.3—42: the three responsibilities resolve into one concrete outcome. */}
   <g opacity={s7}>
    <rect width="1920" height="1080" fill={C.paper}/>
    <rect width="1920" height="1080" fill="url(#work-grid)" opacity=".2"/>
    <Small x={126} y={115}>NINE HOURS OF AUTONOMOUS ENGINEERING</Small>
    <Text x={125} y={344} size={103} serif>Better context.</Text>
    <Text x={125} y={470} size={103} serif>Reusable skills.</Text>
    <Text x={125} y={596} size={103} serif>Real execution.</Text>
    <Skill x={1230} y={305} scale={.83} active={1} opacity={between(f,1160,1196)}/>
    <Wire d="M1318 535C1417 650 1514 666 1626 742" stroke={C.leaf} amount={between(f,1184,1235)}/>
    <MiniChip x={1480} y={685} scale={.57} active={1} f={f}/>
    <path d="M126 727H1080" stroke={C.line} strokeWidth="3"/>
    <Text x={128} y={803} size={32} fill={C.mute}>One reported engineering run. A method worth studying.</Text>
    <Text x={129} y={941} size={23} fill={C.mute}>READ THE CASE STUDY  /  LUOZHU.IO  /  OCT 21 2026</Text>
    <path d="M126 980H1780" stroke={C.line} strokeWidth="2"/>
   </g>
  </svg>
 </AbsoluteFill>;
};

export const NineHoursAlt:React.FC=()=>(
 <svg width="100%" height="100%" viewBox="0 0 1920 1080" xmlns="http://www.w3.org/2000/svg">
  <rect width="1920" height="1080" fill={C.night}/>
  <Small x={122} y={105} dark>DIRECTION B · COMPUTE-FIRST · REJECTED</Small>
  <Text x={121} y={251} size={99} serif fill={C.white}>Nine hours.</Text>
  <Text x={121} y={356} size={99} serif fill={C.white}>Across the machine fleet.</Text>
  <Text x={128} y={417} size={29} fill="#AFC5B6">A hardware-led alternate composition considered before selecting the context route.</Text>
  <Wire d="M190 610C496 610 530 730 789 730" stroke={C.blue} width={4}/>
  <Wire d="M792 730C1057 730 1104 574 1420 574" stroke={C.rust} width={4}/>
  <Laptop x={120} y={558} scale={.79} f={1}/>
  <MiniChip x={795} y={582} scale={.62} f={0}/>
  <ComputeRack x={1420} y={419} scale={.82} f={0} lit={.9} label=""/>
  <Text x={122} y={974} size={26} fill="#AFC5B6">
    Rejected: machines dominated the story; the reusable-context mechanism was missing.
  </Text>
 </svg>
);

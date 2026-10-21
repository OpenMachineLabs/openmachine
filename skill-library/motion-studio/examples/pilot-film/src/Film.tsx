import React from 'react';
import {AbsoluteFill, Audio, staticFile, useCurrentFrame} from 'remotion';
import {
  P, Type, Eyebrow, PathDraw, Archive, Fragment, MiniFragments,
  SemanticField, LexicalIndex, IsometricIndex, Dot, PaperCorners,
  reveal, fadeBand, mix, clip
} from './Art';

/**
 * Contextual Retrieval / an editorial documentary in seven hand-directed shots.
 * The filing, paper excerpt, explanatory context and index materials retain their
 * identity across shots. All data claims come from Anthropic's 2024-09-19 article;
 * all named ACME examples and vector/token graphics are illustrations.
 */
export const Film:React.FC = () => {
 const f=useCurrentFrame();

 const open=1-reveal(f,140,163);
 const lost=fadeBand(f,143,347,18,20);
 const restored=fadeBand(f,328,568,21,18);
 const indexed=fadeBand(f,552,790,20,23);
 const searched=fadeBand(f,767,962,21,20);
 const measured=fadeBand(f,939,1138,17,18);
 const final=reveal(f,1116,1142);

 const cut=reveal(f,178,243);
 const dock=reveal(f,384,473);
 const split=reveal(f,587,667);
 const lookup=reveal(f,818,906);
 const evidence=reveal(f,977,1060);

 return <AbsoluteFill style={{backgroundColor:P.paper}}>
 <Audio src={staticFile('score.wav')} volume={0.65}/>
 <svg viewBox="0 0 1920 1080" width="100%" height="100%" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <pattern id="quiet-grid" width="88" height="88" patternUnits="userSpaceOnUse">
      <path d="M88 0H0V88" fill="none" stroke={P.line} strokeWidth=".8" opacity=".30"/>
      <circle cx="0" cy="0" r="2" fill={P.sage} opacity=".28"/>
    </pattern>
    <clipPath id="inside-page"><rect x="0" y="0" width="1920" height="1080"/></clipPath>
  </defs>

  {/* 0—5.5 s: a real-looking filing instead of a decorative title card. */}
  <g opacity={open}>
    <rect width="1920" height="1080" fill={P.forest}/>
    <path d="M105 900H1765 M105 915H1765" stroke="#496157" strokeWidth="1" opacity=".6"/>
    <Eyebrow x={132} y={111} dark>THE LOST ADDRESS / 01</Eyebrow>
    <Type x={130} y={351} size={87} serif fill={P.white} opacity={reveal(f,4,33)}>The answer is in there.</Type>
    <Type x={130} y={469} size={112} serif fill={P.white} opacity={reveal(f,26,63)}>But where?</Type>
    <Type x={134} y={576} size={32} fill="#CAD8CE" opacity={reveal(f,49,83)}>
      What happens when a document becomes fragments?
    </Type>
    <Archive x={mix(1190,1032,reveal(f,7,65))} y={173} scale={.82} turn={mix(6,-1,reveal(f,16,91))} opacity={reveal(f,6,42)}/>
    <PathDraw d="M132 717C430 717 570 763 1002 645" amount={reveal(f,44,108)} color={P.coral} opacity={.65} width={2}/>
    <Dot x={mix(132,1002,reveal(f,61,117))} y={mix(717,645,reveal(f,61,117))} r={6} color={P.coral} opacity={reveal(f,44,62)}/>
    <Eyebrow x={132} y={983} dark>ONE TECHNICAL STORY / CONTEXTUAL RETRIEVAL</Eyebrow>
  </g>

  {/* 5—11.5 s: a cut is spatial and irreversible unless provenance returns. */}
  <g opacity={lost}>
    <rect width="1920" height="1080" fill={P.paper}/>
    <rect width="1920" height="1080" fill="url(#quiet-grid)" opacity=".26"/>
    <Eyebrow x={126} y={108}>01 / CHUNKING</Eyebrow>
    <Type x={126} y={218} size={78} serif>The words survive.</Type>
    <Type x={126} y={304} size={78} serif>The address doesn't.</Type>
    <Archive x={mix(64,-135,cut)} y={401} scale={.59} turn={-3} opacity={.6+cut*.1}/>
    <PathDraw d="M394 565C535 528 645 528 822 530" amount={cut} color={P.rust} width={2.6} dashed/>
    <circle cx={mix(396,818,cut)} cy={530+35*(1-cut)} r="10" fill={P.paper} stroke={P.rust} strokeWidth="2" opacity={cut}/>
    <Fragment x={mix(1040,801,cut)} y={mix(446,395,cut)} scale={.98} context={0} opacity={reveal(f,166,213)} angle={mix(5,-1,cut)}/>
    <PaperCorners x={782} y={377} w={758} h={268} fill={P.rust}/>
    <MiniFragments x={132} y={826} count={4} highlight={2} fade={reveal(f,208,265)}/>
    <Type x={819} y={764} size={25} fill={P.rust} spacing={2} opacity={reveal(f,238,273)}>
      COMPANY?    QUARTER?    WHICH FILING?
    </Type>
    <Type x={819} y={834} size={30} serif opacity={reveal(f,248,286)}>
      A correct sentence. An incomplete clue.
    </Type>
    <Eyebrow x={126} y={1003}>ILLUSTRATIVE SEC-FILING EXCERPT / ANTHROPIC</Eyebrow>
  </g>

  {/* 11—19 s: the nine-second centerpiece. Whole document -> note -> same excerpt. */}
  <g opacity={restored}>
    <rect width="1920" height="1080" fill={P.paper}/>
    <rect width="1920" height="1080" fill="url(#quiet-grid)" opacity=".18"/>
    <Eyebrow x={124} y={104}>02 / CONTEXTUALIZE · PREPROCESSING</Eyebrow>
    <Type x={122} y={209} size={83} serif>Give the chunk its address.</Type>
    <Type x={128} y={282} size={30} fill={P.muted}>Use the whole document to write a short, chunk-specific note.</Type>
    <Archive x={114} y={410} scale={.49} turn={-4} emphasis={0}/>
    <Type x={134} y={823} size={22} fill={P.muted}>SOURCE DOCUMENT</Type>
    <Type x={134} y={860} size={21} fill={P.sage} weight={700}>ACME / Q2 2023</Type>
    <PathDraw d="M450 598C525 598 548 456 659 456" amount={dock} color={P.sage} width={3}/>
    <g opacity={clip(1-dock)}>
      <rect x={mix(460,660,dock)} y={mix(554,417,dock)} width={185} height={62} fill={P.mint} stroke={P.sage}/>
      <Type x={mix(477,677,dock)} y={mix(588,451,dock)} size={20} fill={P.forest}>Source context</Type>
    </g>
    <Fragment x={660} y={412} scale={1.12} context={dock} opacity={reveal(f,329,353)}/>
    <PaperCorners x={645} y={397} w={837} h={411} fill={P.sage}/>
    <Type x={663} y={901} size={29} serif opacity={reveal(f,451,485)}>
      Same words. Now searchable with their provenance.
    </Type>
    <Eyebrow x={663} y={962} fill={P.sage}>ADDED BEFORE EMBEDDING AND BM25 INDEXING</Eyebrow>
    <PathDraw d="M1501 604C1596 604 1653 558 1771 558" amount={reveal(f,502,552)} color={P.blue} width={2.5}/>
    <PathDraw d="M1501 620C1603 639 1664 707 1771 707" amount={reveal(f,519,559)} color={P.rust} width={2.5}/>
    <Dot x={1770} y={558} r={6} color={P.blue} opacity={reveal(f,542,557)}/>
    <Dot x={1770} y={707} r={6} color={P.rust} opacity={reveal(f,542,557)}/>
  </g>

  {/* 19—26 s: the two actual retrieval material families, not identical cards. */}
  <g opacity={indexed}>
    <rect width="1920" height="1080" fill={P.paper}/>
    <Eyebrow x={123} y={107}>03 / BUILD BOTH INDEXES</Eyebrow>
    <Type x={118} y={211} size={78} serif>One passage. Two ways in.</Type>
    <Type x={124} y={266} size={29} fill={P.muted}>The context travels with both representations.</Type>
    <path d="M786 310V990" stroke={P.line} strokeWidth="2"/>
    <Fragment x={123} y={408} scale={.72} context={1}/>
    <Type x={133} y={783} size={24} fill={P.muted}>ONE CONTEXTUALIZED CHUNK</Type>
    <IsometricIndex x={187} y={826} f={f} color={P.sage} opacity={.9}/>
    <PathDraw d="M660 536C754 536 773 459 850 459" amount={split} color={P.blue} width={3.3}/>
    <PathDraw d="M660 559C755 590 774 790 850 790" amount={reveal(f,620,716)} color={P.rust} width={3.3}/>
    <Dot x={mix(665,849,split)} y={mix(536,459,split)} r={8} color={P.blue} opacity={split}/>
    <Dot x={mix(665,849,split)} y={mix(559,790,split)} r={8} color={P.rust} opacity={split}/>
    <SemanticField x={870} y={318} f={f} progress={reveal(f,600,692)}/>
    <LexicalIndex x={870} y={662} f={f} progress={reveal(f,637,730)}/>
  </g>

  {/* 26—32 s: query opens a concrete archive fragment rather than a fictitious score. */}
  <g opacity={searched}>
    <rect width="1920" height="1080" fill={P.paper}/>
    <rect x="0" y="0" width="1920" height="397" fill="#E9EDE5"/>
    <Eyebrow x={126} y={108}>04 / RETRIEVE</Eyebrow>
    <Type x={123} y={221} size={74} serif>Ask a more specific question.</Type>
    <Type x={130} y={303} size={36}>What was ACME Corp's Q2 2023 revenue growth?</Type>
    <path d="M125 333H1769" stroke={P.line} strokeWidth="2"/>
    <path d="M125 349H742" stroke={P.rust} strokeWidth={3}/>
    <Dot x={mix(126,743,lookup)} y={349} r={7} color={P.rust}/>
    <Type x={126} y={452} size={24} fill={P.muted} spacing={1.5}>SEARCH THE CHUNKS</Type>
    <MiniFragments x={135} y={535} count={8} highlight={6} fade={reveal(f,777,825)}/>
    <PathDraw d="M680 654C822 654 831 664 984 664" amount={lookup} color={P.sage} width={3}/>
    <path d="M713 566L713 838" stroke={P.line} strokeWidth="1.5" strokeDasharray="6 11"/>
    <Type x={1002} y={479} size={23} weight={700} fill={P.sage} spacing={2.3} opacity={lookup}>MATCH / PROVENANCE RETAINED</Type>
    <Fragment x={990} y={545} scale={.78} context={1} opacity={lookup} highlight={1}/>
    <PaperCorners x={976} y={527} w={589} h={288} fill={P.sage}/>
    <Type x={1011} y={904} size={27} serif opacity={lookup}>A findable answer is still tied to its document.</Type>
    <Eyebrow x={128} y={1007}>DIAGRAM ILLUSTRATIVE · NO QUERY ACCURACY CLAIMED</Eyebrow>
  </g>

  {/* 32—37.5 s: a measured result, with the scale and scope beside it. */}
  <g opacity={measured}>
    <rect width="1920" height="1080" fill={P.forest}/>
    <path d="M125 106H1790 M125 965H1790" stroke="#59736A" strokeWidth="2" opacity=".75"/>
    <Eyebrow x={129} y={84} dark>05 / PUBLISHED EVALUATION</Eyebrow>
    <Type x={122} y={206} size={79} serif fill={P.white}>Fewer relevant passages missed.</Type>
    <Type x={128} y={284} size={29} fill="#C5D3C8">Retrieval failure rate · 1 − recall@20 · averaged across the evaluated domains</Type>
    {[0,1,2,3,4,5,6].map(i=> <g key={i}>
      <path d={'M'+(554+195*i)+' 361V795'} stroke="#617A6E" strokeWidth={i===0?2:1} opacity={.46} strokeDasharray={i===0?'0':'5 10'}/>
      <Type x={554+195*i} y={824} size={22} fill="#B8C9BF" anchor="middle">{i}%</Type>
    </g>)}
    <Type x={122} y={471} size={29} fill="#CCD8CF">BASELINE</Type>
    <Type x={122} y={541} size={76} fill={P.white} serif>5.7%</Type>
    <rect x="554" y="472" width={1170*5.7/6*evidence} height="63" fill={P.rust}/>
    <rect x="554" y="472" width={1170*5.7/6*evidence} height="2" fill="#F8B998"/>
    <Type x={122} y={644} size={28} fill="#CCD8CF">CONTEXTUAL</Type>
    <Type x={122} y={684} size={24} fill="#B3C6B8">EMBEDDINGS + BM25</Type>
    <Type x={122} y={759} size={76} fill={P.white} serif>2.9%</Type>
    <rect x="554" y="687" width={1170*2.9/6*evidence} height="63" fill="#98C0A4"/>
    <rect x="554" y="687" width={1170*2.9/6*evidence} height="2" fill="#D4E7D8"/>
    <Type x={1781} y={888} size={68} serif fill="#D6EAD7" anchor="end" opacity={reveal(f,1032,1081)}>49% lower</Type>
    <Type x={1781} y={927} size={23} fill="#C2D0C6" anchor="end">relative failure reduction in this evaluation</Type>
    <Type x={130} y={1018} size={18} fill="#B6C3B8">SOURCE: ANTHROPIC, INTRODUCING CONTEXTUAL RETRIEVAL · SEP 19 2024</Type>
  </g>

  {/* 37.5—40 s: a quiet end, with the same contextualized paper as protagonist. */}
  <g opacity={final}>
    <rect width="1920" height="1080" fill={P.paper}/>
    <rect width="1920" height="1080" fill="url(#quiet-grid)" opacity=".15"/>
    <Eyebrow x={132} y={165}>CONTEXTUAL RETRIEVAL / AN EDITORIAL FIELDNOTE</Eyebrow>
    <Type x={124} y={412} size={105} serif>Keep the passage.</Type>
    <Type x={124} y={546} size={105} serif>Keep its context.</Type>
    <Fragment x={1048} y={634} scale={.71} context={1}/>
    <path d="M130 668H923" stroke={P.line} strokeWidth="3"/>
    <Dot x={923} y={668} r={7} color={P.sage}/>
    <Type x={132} y={726} size={31} fill={P.muted}>A better address for information worth finding.</Type>
    <Type x={132} y={981} size={21} fill={P.muted}>READ THE RESEARCH  /  ANTHROPIC.COM/RESEARCH/CONTEXTUAL-RETRIEVAL</Type>
    <path d="M130 1014H1790" stroke={P.line} strokeWidth="2"/>
  </g>
 </svg>
 </AbsoluteFill>;
};

import React from 'react';
import {AbsoluteFill, Audio, Composition, interpolate, staticFile, useCurrentFrame} from 'remotion';

const ink='#23231F', paper='#F2EFE8', rust='#B95438', leaf='#607C68', blue='#8BA0A8', rule='#D2CEC4', white='#FFFEFA';
const show=(f:number,a:number,b:number)=>interpolate(f,[a,b],[0,1],{extrapolateLeft:'clamp',extrapolateRight:'clamp'});
const Label:React.FC<{x:number;y:number;children:React.ReactNode;color?:string;size?:number;serif?:boolean}> = ({x,y,children,color=ink,size=28,serif=false}) => <div style={{position:'absolute',left:x,top:y,fontFamily:serif?'Georgia,serif':'Arial,sans-serif',fontSize:size,color,lineHeight:1.15,letterSpacing:serif?'-0.03em':'-.01em'}}>{children}</div>;
const Line:React.FC<{x:number;y:number;w:number;color?:string}> = ({x,y,w,color=rule}) => <div style={{position:'absolute',left:x,top:y,width:w,height:2,background:color}}/>;
const Sheet:React.FC<{x:number;y:number;w:number;h:number;children:React.ReactNode;fill?:string;angle?:number}> = ({x,y,w,h,children,fill=white,angle=0}) => <div style={{position:'absolute',left:x,top:y,width:w,height:h,background:fill,border:'1px solid '+rule,boxSizing:'border-box',transform:'rotate('+angle+'deg)',boxShadow:'0 14px 28px rgba(35,35,31,.05)'}}>{children}</div>;
const Header:React.FC<{n:string;name:string}> = ({n,name}) => <><Label x={100} y={64} size={19} color="#65645E">{name.toUpperCase()}</Label><Label x={1680} y={64} size={19} color="#65645E">{n} / FIELDNOTE</Label><Line x={100} y={105} w={1720}/></>;
const Chunk:React.FC<{x:number;y:number;context:boolean;contextProgress?:number;scale?:number;opacity?:number}> = ({x,y,context,contextProgress,scale=1,opacity=1}) => {
 const cp = contextProgress ?? (context ? 1 : 0);
 return <div style={{position:'absolute',left:x,top:y,transform:'scale('+scale+')',transformOrigin:'top left',opacity}}>
 <Sheet x={0} y={0} w={640} h={248}>
 <div style={{position:'absolute',left:24,top:20,width:590,height:54*cp,background:'#E4E9E0',borderLeft:'5px solid '+leaf,opacity:cp}}/>
 <div style={{position:'absolute',left:42,top:34,opacity:cp}}><Label x={0} y={0} size={21} color={leaf}>ACME CORP. / Q2 2023 SEC FILING</Label></div>
 <Label x={30} y={38+62*cp} size={31} serif>“The company's revenue grew by 3% over the previous quarter.”</Label>
 <Line x={30} y={160+24*cp} w={575}/><Label x={30} y={179+24*cp} size={17} color="#65645E">ILLUSTRATIVE SOURCE EXCERPT</Label>
 </Sheet></div>;
};

export const Film:React.FC=()=>{
 const f=useCurrentFrame();
 const fade=(a:number,b:number)=>show(f,a,b);
 const scene=(a:number,b:number)=>f>=a&&f<b;
 return <AbsoluteFill style={{background:paper,color:ink,overflow:'hidden'}}>
 <Audio src={staticFile('score.wav')} volume={0.48}/>
 {scene(0,150)&&<><Header n="01" name="The context problem"/>
 <Label x={100} y={175} size={76} serif>A sentence can be true—<br/>and still be hard to find.</Label>
 <Label x={105} y={405} size={29} color="#65645E">Split a document into pieces. What happens when a piece<br/>forgets where it came from?</Label>
 <Chunk x={1010+90*(1-fade(0,24))} y={300} context={false} scale={.9} opacity={fade(0,20)}/>
 <Line x={105} y={710} w={680}/><Label x={105} y={744} size={23} color="#65645E">RETRIEVAL-AUGMENTED GENERATION</Label>
 <Label x={105} y={790} size={31} serif>A useful answer begins with a useful passage.</Label></>}
 {scene(150,300)&&<><Header n="02" name="01 / Chunking"/>
 <Label x={100} y={150} size={65} serif>Cut the page. Lose the frame.</Label>
 <Sheet x={135} y={285} w={550} h={450} angle={-2}>
 <Label x={35} y={35} size={23} color="#65645E">ACME CORP. / QUARTERLY FILING</Label><Line x={35} y={80} w={470}/>
 <Label x={35} y={130} size={31} serif>Q2 2023 revenue increased following a stronger enterprise quarter.</Label>
 <Line x={35} y={300} w={470}/><Label x={35} y={340} size={20} color="#65645E">SECTION 4.2 — PERFORMANCE</Label></Sheet>
 <div style={{position:'absolute',left:720,top:280,width:3,height:475,background:rust}}/>
 <Chunk x={800} y={350} context={false} scale={.83}/>
 <Label x={820} y={590} size={23} color={rust}>COMPANY? PERIOD? SOURCE?</Label>
 <Label x={100} y={850} size={31} color="#65645E">The words survive. The relationship may not.</Label></>}
 {scene(300,450)&&<><Header n="03" name="02 / Contextualize"/>
 <Label x={100} y={150} size={63} serif>Give the same sentence its address.</Label>
 <Label x={105} y={245} size={25} color="#65645E">Prepend a concise, chunk-specific note before indexing.</Label>
 <Sheet x={130} y={390+25*(1-fade(305,330))} w={360} h={125} fill="#E4E9E0" angle={-3}>
 <Label x={25} y={25} size={21} color={leaf}>DOCUMENT CONTEXT</Label><Label x={25} y={68} size={29} serif>ACME · Q2 2023</Label></Sheet>
 <Line x={500} y={455} w={110*fade(305,330)} color={leaf}/>
 <Chunk x={650} y={350} context={true} contextProgress={fade(305,345)}/>
 <Label x={130} y={590} size={24} color="#65645E">Context is attached to the chunk, not substituted for it.</Label>
 <Label x={100} y={865} size={19} color="#65645E">ILLUSTRATIVE EXCERPT ADAPTED FROM ANTHROPIC'S ARTICLE</Label></>}
 {scene(450,630)&&<><Header n="04" name="03 / Two indexes"/>
 <Label x={100} y={145} size={61} serif>One contextualized chunk. Two ways in.</Label>
 <Chunk x={100} y={350} context={true} scale={.67}/>
 <Line x={535} y={445} w={135*show(f,465,495)} color={blue}/>
 <div style={{position:'absolute',left:700,top:255,width:430,height:230,borderTop:'3px solid '+blue,borderBottom:'3px solid '+blue}}>
 <Label x={22} y={20} size={20} color={blue}>SEMANTIC INDEX</Label><Label x={22} y={70} size={37} serif>Meaning</Label><Label x={22} y={130} size={24} color="#65645E">Embeddings map contextual text to a vector.</Label></div>
 <div style={{position:'absolute',left:700,top:545,width:430,height:230,borderTop:'3px solid '+rust,borderBottom:'3px solid '+rust}}>
 <Label x={22} y={20} size={20} color={rust}>LEXICAL INDEX</Label><Label x={22} y={70} size={37} serif>Exact terms</Label><Label x={22} y={130} size={24} color="#65645E">BM25 keeps words and identifiers searchable.</Label></div>
 <Line x={1155} y={365} w={125*show(f,480,510)} color={blue}/><Line x={1155} y={655} w={125*show(f,495,525)} color={rust}/>
 <Sheet x={1310} y={410} w={420} h={230}><Label x={30} y={30} size={21} color="#65645E">COMBINED CANDIDATES</Label><Line x={30} y={75} w={350}/><Label x={30} y={100} size={30} serif>Rank · merge · dedupe</Label><Label x={30} y={160} size={22} color="#65645E">Complementary signals</Label></Sheet>
 <Label x={100} y={875} size={22} color="#65645E">CONTEXT IS PREPENDED BEFORE BOTH INDEXES ARE BUILT.</Label></>}
 {scene(630,780)&&<><Header n="05" name="04 / Retrieve"/>
 <Label x={100} y={150} size={64} serif>A query can find the right fragment.</Label>
 <Sheet x={120} y={335} w={620} h={160}><Label x={28} y={25} size={20} color={rust}>USER QUERY</Label><Label x={28} y={70} size={30} serif>What was ACME's Q2 2023 revenue growth?</Label></Sheet>
 <Line x={770} y={410} w={135} color={blue}/>
 <Chunk x={960} y={325} context={true} scale={.78} opacity={fade(650,680)}/>
 <Label x={110} y={575} size={30} color="#65645E">The passage arrives with its source context intact.</Label><Line x={110} y={675} w={650}/>
 <Label x={110} y={715} size={22} color="#65645E">A PREPROCESSING CHANGE</Label><Label x={110} y={760} size={31} serif>Not a longer prompt for every question.</Label></>}
 {scene(780,960)&&<><Header n="06" name="05 / Measured result"/>
 <Label x={100} y={145} size={63} serif>Fewer relevant passages missed.</Label>
 <Label x={105} y={245} size={25} color="#65645E">Anthropic evaluation · relevant chunk not retrieved in the top 20</Label>
 <Label x={145} y={340} size={20} color="#65645E">BASELINE</Label><Label x={145} y={385} size={75} serif>5.7%</Label>
 <div style={{position:'absolute',left:145,top:490,width:700,height:38,background:'#DEDAD1'}}/><div style={{position:'absolute',left:145,top:490,width:700*fade(790,840),height:38,background:rust}}/>
 <Label x={1000} y={340} size={20} color={leaf}>CONTEXTUAL EMBEDDINGS + BM25</Label><Label x={1000} y={385} size={75} serif>2.9%</Label>
 <div style={{position:'absolute',left:1000,top:490,width:700,height:38,background:'#DEDAD1'}}/><div style={{position:'absolute',left:1000,top:490,width:700*(2.9/5.7)*fade(790,840),height:38,background:leaf}}/>
 <Line x={145} y={600} w={1560}/><Label x={145} y={650} size={51} color={leaf} serif>49% relative reduction</Label>
 <Label x={145} y={735} size={25} color="#65645E">Failure rate in this evaluation—not a universal guarantee.</Label>
 <Label x={145} y={825} size={19} color="#65645E">SOURCE: ANTHROPIC, INTRODUCING CONTEXTUAL RETRIEVAL, 19 SEP 2024</Label></>}
 {scene(960,1200)&&<><Header n="07" name="The boundary"/>
 <Label x={105} y={170} size={68} serif>Context helps retrieval.<br/>It does not replace judgment.</Label>
 <Label x={110} y={390} size={31} color="#65645E">Chunk boundaries, prompts, and retrieval depth still matter.</Label>
 <Sheet x={1120} y={320} w={550} h={300} fill={white} angle={1}>
 <Label x={40} y={35} size={22} color={leaf}>KEEP THE SOURCE ATTACHED</Label><Line x={40} y={80} w={450}/>
 <Label x={40} y={115} size={29} serif>Meaning + exact terms</Label><Label x={40} y={175} size={25} color="#65645E">Context before indexing</Label><Label x={40} y={230} size={22} color="#65645E">Measured in one published setup</Label></Sheet>
 <Line x={110} y={720} w={850}/><Label x={110} y={765} size={23} color="#65645E">ANTHROPIC ENGINEERING · CONTEXTUAL RETRIEVAL · 2024</Label>
 <Label x={110} y={825} size={31} serif>Find the passage. Keep the frame.</Label></>}
 </AbsoluteFill>;
};

export const FilmRoot:React.FC=()=> <Composition id="ContextualRetrieval" component={Film} width={1920} height={1080} fps={30} durationInFrames={1200}/>;

"""Original 42-second stereo underscore for the Nine Hours case-study film.

Deterministic synthesis; no samples, licensed recordings, remote services, or TTS.
Cues support the represented stages rather than claiming actual experiment audio.
"""
from pathlib import Path
import math
import struct
import wave

RATE=24000
SECONDS=42
TAU=2*math.pi
OUT=Path(__file__).resolve().parents[1]/"public"/"nine-hours-score.wav"

# Soft clock/tactile notes at the intended story transitions.
EVENTS=[
 (0.3,146.83,0.12,-.3), (3.7,196.00,0.10,.25),
 (4.7,174.61,0.10,-.2), (7.2,246.94,.09,.3),
 (9.8,164.81,.14,-.4), (12.4,220.00,.12,.35),
 (15.3,329.63,.14,.10), (16.6,196.00,.11,-.2),
 (19.4,293.66,.15,.25), (22.8,174.61,.12,-.15),
 (25.5,246.94,.11,.3), (28.2,164.81,.13,-.3),
 (30.0,293.66,.17,.2), (31.8,392.00,.11,-.2),
 (34.2,174.61,.13,.10), (36.4,220.00,.16,.3),
 (38.5,293.66,.12,-.1), (40.1,246.94,.1,.10)
]

def fade(t:float)->float:
 return max(0.0,min(1.0,t/.5,(SECONDS-t)/1.3))

def sample(t:float)->tuple[float,float]:
 # Warm pulse train with an 84-s beat: sound does not pretend to record GPUs.
 sub=0.051*math.sin(TAU*73.42*t)+0.034*math.sin(TAU*110*t+.15)
 alto=0.018*math.sin(TAU*146.83*t + .25*math.sin(TAU*.16*t))
 airy=.008*math.sin(TAU*196.0*t) + .005*math.sin(TAU*293.66*t+.3)
 v=(sub+alto+airy)*(.89+.08*math.sin(TAU*.075*t))
 # The measured-result scene has a less dense bed for reading.
 if 34<=t<38.4:v*=.79
 left=right=v
 for start,pitch,amplitude,pan in EVENTS:
  dt=t-start
  if not 0<=dt<.55:continue
  env=min(1.0,dt/.009)*math.exp(-dt*12.5)
  tone=math.sin(TAU*pitch*dt)+.18*math.sin(TAU*pitch*2.002*dt)
  tap=.18*math.exp(-dt*27)*math.sin(TAU*(910+78*math.sin(dt*20))*dt)
  cue=amplitude*env*(tone+tap)
  left+=cue*(1.0-max(0.0,pan))
  right+=cue*(1.0+min(0.0,pan))
 gain=fade(t)
 drift=.035*math.sin(TAU*.034*t)
 return max(-.98,min(.98,left*(1-drift)*gain)),max(-.98,min(.98,right*(1+drift)*gain))

def render()->None:
 OUT.parent.mkdir(parents=True,exist_ok=True)
 with wave.open(str(OUT),"wb") as wav:
  wav.setnchannels(2);wav.setsampwidth(2);wav.setframerate(RATE)
  block=bytearray()
  for frame in range(RATE*SECONDS):
   l,r=sample(frame/RATE)
   block.extend(struct.pack('<hh',int(l*32767),int(r*32767)))
   if len(block)>=RATE*4:
    wav.writeframesraw(block);block.clear()
  if block:wav.writeframesraw(block)
 print(OUT)

if __name__=="__main__":
 render()

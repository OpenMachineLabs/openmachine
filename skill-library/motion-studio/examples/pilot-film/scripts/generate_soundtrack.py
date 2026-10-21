from pathlib import Path
import math, random, struct, wave

# Original, deterministic soft tonal bed with sparse paper-like taps; no samples.
OUT = Path(__file__).resolve().parents[1] / 'public' / 'score.wav'
RATE, SECONDS = 24000, 40
random.seed(204)
OUT.parent.mkdir(parents=True, exist_ok=True)
with wave.open(str(OUT), 'wb') as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
    buf = bytearray()
    taps = [int(t*RATE) for t in (0.7, 4.9, 9.1, 13.7, 18.2, 23.0, 28.1, 33.4, 38.1)]
    for i in range(RATE*SECONDS):
        t=i/RATE
        fade=min(1,t/1.4,(SECONDS-t)/1.6)
        bed=0.055*math.sin(2*math.pi*110*t)+0.026*math.sin(2*math.pi*164.81*t)+0.016*math.sin(2*math.pi*220*t)
        tap=0.0
        for start in taps:
            dt=(i-start)/RATE
            if 0 <= dt < .22:
                tap += .12*math.exp(-dt*24)*math.sin(2*math.pi*(720-1800*dt)*dt)
        sample=max(-1,min(1,(bed+tap)*fade))
        buf.extend(struct.pack('<h',int(sample*32767)))
    w.writeframes(buf)
print(OUT)

"""A small original procedural score for the 40-second editorial pilot.

No licensed recordings, sample libraries, services, or ambient audio files are used.
The same deterministic PCM is rendered from source on every machine.
"""
from pathlib import Path
import math
import struct
import wave

RATE = 24000
SECONDS = 40
OUTPUT = Path(__file__).resolve().parents[1] / "public" / "score.wav"
TAU = 2 * math.pi

# Each cue supports an observable edit or transition in the animation.
CUES = [
    (0.8, 220.0, 0.13, -0.5),   # archive enters
    (4.8, 165.0, 0.14, 0.1),    # paper is cut
    (7.6, 246.9, 0.10, 0.5),   # the lost identifier
    (10.8, 196.0, 0.15, -0.5), # document returns
    (12.8, 294.0, 0.11, -0.1), # contextual note arrives
    (15.3, 392.0, 0.15, 0.4),  # note attaches
    (18.5, 220.0, 0.13, -0.4), # semantic path branches
    (21.7, 329.6, 0.12, 0.5),  # lexical index fills
    (25.8, 246.9, 0.16, 0.1),  # query begins
    (29.2, 392.0, 0.17, 0.4),  # source is retrieved
    (31.8, 164.8, 0.15, -0.2), # measured chart
    (34.5, 220.0, 0.11, 0.2),  # reduction settles
    (37.7, 329.6, 0.12, 0.0),  # end mark
]


def fade(time: float) -> float:
    """Avoid clicks at the ends of the encoded track."""
    return min(1.0, max(0.0, time / 0.65), max(0.0, (SECONDS - time) / 1.15))


def score(time: float, frame: int) -> tuple[float, float]:
    # Restrained analogue-like bed with a slowly changing register.
    modulation = 0.90 + 0.08 * math.sin(TAU * 0.087 * time)
    bed = modulation * (
        0.033 * math.sin(TAU * 82.41 * time)
        + 0.025 * math.sin(TAU * 123.47 * time + 0.23)
        + 0.018 * math.sin(TAU * 164.81 * time + 0.7)
    )
    # Resolves slightly as the context re-attaches. The outcome scene relaxes.
    resolve = min(1.0, max(0.0, (time - 10.7) / 4.2))
    bed += 0.012 * resolve * math.sin(TAU * 196.0 * time)
    if 31.8 < time < 37.5:
        bed *= 0.77

    left = bed
    right = bed
    for start, pitch, level, pan in CUES:
        dt = time - start
        if dt < 0 or dt > 0.52:
            continue
        attack = min(1.0, dt / 0.008)
        envelope = attack * math.exp(-dt * 13.8)
        # A quiet glass/felt note plus a short, noise-like paper edge.
        tone = math.sin(TAU * pitch * dt) + 0.22 * math.sin(TAU * pitch * 2.01 * dt)
        rustle = (math.sin(TAU * (1171 + 83 * math.sin(dt * 21)) * dt)
                  * math.exp(-dt * 26) * 0.18)
        cue = level * envelope * (tone + rustle)
        left += cue * (1.0 - max(0.0, pan))
        right += cue * (1.0 + min(0.0, pan))

    # A tiny stereo shift adds width without simulated ambience or unlicensed reverb.
    drift = 0.038 * math.sin(TAU * 0.032 * time)
    gain = fade(time)
    return left * (1.0 - drift) * gain, right * (1.0 + drift) * gain


def render() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(OUTPUT), "wb") as track:
        track.setnchannels(2)
        track.setsampwidth(2)
        track.setframerate(RATE)
        block = bytearray()
        for frame in range(RATE * SECONDS):
            a, b = score(frame / RATE, frame)
            block.extend(struct.pack("<hh", int(max(-0.99, min(0.99, a)) * 32767),
                                     int(max(-0.99, min(0.99, b)) * 32767)))
            if len(block) >= RATE * 4:
                track.writeframesraw(block)
                block.clear()
        if block:
            track.writeframesraw(block)
    print(OUTPUT)


if __name__ == "__main__":
    render()

"""Review contact sheet for the source-specific Nine Hours film.

Reuses the pilot's bundled ffmpeg locator and creates a portable self-contained
SVG sheet from actual full-film frames. JPG copies for PR review are generated
outside the Skill package from these frames.
"""
from pathlib import Path
import base64

from review_sheet import ffmpeg_binary, run

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/"out"
FILM=OUT/"nine-hours-engineering.mp4"
TIMES=(2.7,7.9,14.9,21.6,25.5,31.2,37.6,40.5)

def main()->None:
 if not FILM.is_file():
  raise SystemExit(f"Render the film before making review images: {FILM}")
 ff=ffmpeg_binary()
 OUT.mkdir(exist_ok=True)
 cells=[]
 for i,second in enumerate(TIMES):
  png=OUT/f"nine-hours-frame-{round(second*10):03d}.png"
  run(ff,["-ss",str(second),"-i",str(FILM),"-frames:v","1","-update","1",str(png)])
  encoded=base64.b64encode(png.read_bytes()).decode("ascii")
  x,y=10+(i%4)*485,10+(i//4)*300
  cells.append(
   f'<image x="{x}" y="{y}" width="465" height="262" '
   f'href="data:image/png;base64,{encoded}"/>'
   f'<text x="{x}" y="{y+285}" font-family="Arial" font-size="20" '
   f'fill="#20352F">{second:04.1f}s</text>')
 svg='<svg xmlns="http://www.w3.org/2000/svg" width="1960" height="610" viewBox="0 0 1960 610">'
 svg+='<rect width="100%" height="100%" fill="#F2F0E9"/>'
 svg+="".join(cells)+"</svg>"
 target=OUT/"nine-hours-contact-sheet.svg"
 target.write_text(svg,encoding="utf-8")
 print(target)

if __name__=="__main__":
 main()

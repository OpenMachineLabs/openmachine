"""Generate a poster and an eight-frame SVG contact sheet without extra Python packages."""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out"
VIDEO = OUT / "contextual-retrieval.mp4"


def ffmpeg_binary() -> Path:
    system = shutil.which("ffmpeg")
    if system:
        return Path(system)
    matches = sorted((ROOT / "node_modules" / "@remotion").glob("compositor-*/ffmpeg"))
    if matches:
        return matches[0]
    raise SystemExit("ffmpeg was not found; install the project's locked Remotion dependencies first")


def run(binary: Path, args: list[str]) -> None:
    env = os.environ.copy()
    env["DYLD_LIBRARY_PATH"] = str(binary.parent) + os.pathsep + env.get("DYLD_LIBRARY_PATH", "")
    subprocess.run([str(binary), "-loglevel", "error", "-y", *args], check=True, env=env)


def main() -> None:
    if not VIDEO.is_file():
        raise SystemExit(f"Render the complete film first: {VIDEO}")
    OUT.mkdir(exist_ok=True)
    ffmpeg = ffmpeg_binary()
    run(ffmpeg, ["-ss", "11", "-i", str(VIDEO), "-frames:v", "1", "-update", "1", str(OUT / "poster.png")])

    cells = []
    for i, second in enumerate((0, 5, 10, 15, 20, 25, 30, 35)):
        frame = OUT / f"contact-{second:02d}.png"
        run(ffmpeg, ["-ss", str(second), "-i", str(VIDEO), "-frames:v", "1", "-update", "1", str(frame)])
        encoded = base64.b64encode(frame.read_bytes()).decode("ascii")
        x, y = 10 + (i % 2) * 500, 10 + (i // 2) * 300
        cells.append(
            f'<image x="{x}" y="{y}" width="480" height="270" '
            f'href="data:image/png;base64,{encoded}"/>'
            f'<text x="{x}" y="{y + 290}" font-family="Arial" font-size="20" '
            f'fill="#23231F">{second:02d}s</text>'
        )
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="1200" viewBox="0 0 1000 1200">'
        '<rect width="100%" height="100%" fill="#F2EFE8"/>'
        + "".join(cells)
        + "</svg>"
    )
    (OUT / "contact-sheet.svg").write_text(svg, encoding="utf-8")
    print(OUT / "poster.png")
    print(OUT / "contact-sheet.svg")


if __name__ == "__main__":
    main()

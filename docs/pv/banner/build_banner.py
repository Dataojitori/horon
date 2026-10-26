"""Regenerate docs/pv/banner/horon-banner.svg (the README header, 1280 x 480).

Input:   horse-emblem.svg in this folder (the horse holding its own reins; drawn by Gemini,
         jaw and neck corrected by hand) and the Instrument Serif font (Horon's wordmark font).
Does:    places the emblem on the left of a light card and sets "HORON / self-improving / harness"
         on the right. The text is converted to outlines (paths) because GitHub shows README SVGs
         through <img>, where web fonts cannot load.
Output:  horon-banner.svg next to this script.

Needs:   pip install fonttools uharfbuzz
Fonts:   web/node_modules/@fontsource/instrument-serif  (run `npm install` in web/ first)
Run:     python docs/pv/banner/build_banner.py
"""
import re
import tempfile
from pathlib import Path

import uharfbuzz as hb
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
FONT_DIR = REPO / "web" / "node_modules" / "@fontsource" / "instrument-serif" / "files"
EMBLEM = HERE / "horse-emblem.svg"
OUT = HERE / "horon-banner.svg"

W, H = 1280, 480
# The emblem uses GitHub's Primer greys plus amber; the card follows it.
INK, MUTED, BORDER, CANVAS = "#24292F", "#57606A", "#D0D7DE", "#FFFFFF"
AMBER_DARK, AMBER_DEEP = "#D97706", "#B45309"   # amber text has to be darker than the reins to read on white

# Emblem placement: its disc is centred at (200, 195), radius 165, in its own 400 x 400 box.
EM_SCALE = 1.18
EM_CX, EM_CY = 302, 256


def fmt(v):
    s = f"{v:.1f}"
    return s[:-2] if s.endswith(".0") else s


def load_font(woff_name):
    """HarfBuzz cannot read WOFF, so unwrap it to a temporary TTF."""
    ttf = Path(tempfile.gettempdir()) / woff_name.replace(".woff", ".ttf")
    if not ttf.exists():
        f = TTFont(FONT_DIR / woff_name)
        f.flavor = None
        f.save(ttf)
    return str(ttf)


def text_path(font_file, text, size, x, y, tracking=0.0):
    """Shape with HarfBuzz (kerning on); return (svg path d, advance width in px)."""
    font = hb.Font(hb.Face(hb.Blob.from_file_path(font_file)))
    buf = hb.Buffer()
    buf.add_str(text)
    buf.guess_segment_properties()
    hb.shape(font, buf, {"kern": True, "liga": False})
    tt = TTFont(font_file)
    gs, order = tt.getGlyphSet(), tt.getGlyphOrder()
    k = size / tt["head"].unitsPerEm
    cur, parts = 0.0, []
    for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
        pen = SVGPathPen(gs, ntos=fmt)
        gs[order[info.codepoint]].draw(
            TransformPen(pen, (k, 0, 0, -k, x + (cur + pos.x_offset) * k, y - pos.y_offset * k))
        )
        parts.append(pen.getCommands())
        cur += pos.x_advance + tracking / k
    return " ".join(p for p in parts if p), cur * k


def emblem():
    """Return (defs, body) of the emblem with its ids prefixed so they cannot clash with ours."""
    src = EMBLEM.read_text(encoding="utf-8")
    src = re.sub(r"<!--.*?-->", "", src, flags=re.S)
    inner = re.search(r"<svg[^>]*>(.*)</svg>", src, flags=re.S).group(1)
    ids = re.findall(r'id="([^"]+)"', inner)
    for i in ids:
        inner = inner.replace(f'id="{i}"', f'id="em-{i}"').replace(f"url(#{i})", f"url(#em-{i})")
    defs = re.search(r"<defs>(.*?)</defs>", inner, flags=re.S).group(1)
    body = re.sub(r"<defs>.*?</defs>", "", inner, flags=re.S)
    return defs.strip(), body.strip()


reg = load_font("instrument-serif-latin-400-normal.woff")
ita = load_font("instrument-serif-latin-400-italic.woff")

TX = 575
eyebrow_d, _ = text_path(reg, "HORON", 28, TX + 4, 143, tracking=12)
line1_d, w1 = text_path(reg, "self-improving", 112, TX, 259)
line2_d, w2 = text_path(ita, "harness", 112, TX, 361)

em_defs, em_body = emblem()
em_tx, em_ty = EM_CX - 200 * EM_SCALE, EM_CY - 195 * EM_SCALE

svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-labelledby="t d">
  <title id="t">Horon: self-improving harness</title>
  <desc id="d">A black horse silhouette holding its own amber reins in its teeth, next to the words: Horon, self-improving harness.</desc>
  <defs>
    {em_defs}
    <pattern id="dots" width="22" height="22" patternUnits="userSpaceOnUse">
      <circle cx="1.5" cy="1.5" r="1.2" fill="{BORDER}" opacity=".55"/>
    </pattern>
    <radialGradient id="halo" cx="50%" cy="50%" r="50%">
      <stop offset="0" stop-color="#FBBF24" stop-opacity=".16"/>
      <stop offset="1" stop-color="#FBBF24" stop-opacity="0"/>
    </radialGradient>
    <linearGradient id="harness" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="{AMBER_DARK}"/>
      <stop offset="1" stop-color="{AMBER_DEEP}"/>
    </linearGradient>
    <clipPath id="card"><rect width="{W}" height="{H}" rx="24"/></clipPath>
  </defs>
  <g clip-path="url(#card)">
    <rect width="{W}" height="{H}" fill="{CANVAS}"/>
    <rect width="{W}" height="{H}" fill="url(#dots)"/>
    <circle cx="{EM_CX}" cy="{EM_CY}" r="250" fill="url(#halo)"/>
    <g transform="translate({fmt(em_tx)} {fmt(em_ty)}) scale({EM_SCALE})">
    {em_body}
    </g>
    <path d="{eyebrow_d}" fill="{MUTED}"/>
    <path d="{line1_d}" fill="{INK}"/>
    <path d="{line2_d}" fill="url(#harness)"/>
  </g>
  <rect x=".5" y=".5" width="{W - 1}" height="{H - 1}" rx="23.5" fill="none" stroke="{BORDER}"/>
</svg>
'''

OUT.write_text(svg, encoding="utf-8", newline="\n")
print(f"wrote {OUT} ({len(svg)} bytes); text right edge x={TX + w1:.0f} of {W}")

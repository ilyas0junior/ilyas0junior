#!/usr/bin/env python3
"""Render the profile README cards (animated SVG) into cards/.

    python scripts/build_cards.py             # animated cards  -> cards/
    STATIC=1 python scripts/build_cards.py    # final frame     -> cards-static/ (+ preview.html)

Needs:  pip install fonttools brotli          (Pillow is optional: it shrinks assets/portrait.*)
Reads:  data/contributions.json               (refreshed daily by .github/workflows/refresh-cards.yml)

Everything in the CONFIG block below is meant to be edited: colours, fonts, copy, tiles, featured repos.

How the cards work (they are shown through <img>, so no scripts, no external files):
  * one inline <style> with @font-face (subset by pyftsubset, base64) and CSS keyframes
  * base styles are the FINAL frame; keyframes only describe `from`, so anything that stops
    animations (prefers-reduced-motion, STATIC=1) lands on the finished card
  * SMIL <animate> is used for decoration only, always inside .deco, which is hidden under
    prefers-reduced-motion (CSS cannot switch SMIL off, so content never depends on it)
"""
from __future__ import annotations

import base64
import datetime as dt
import html
import io
import json
import os
import subprocess
import sys
import tempfile
from collections import OrderedDict
from pathlib import Path

from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parent.parent
STATIC = os.environ.get("STATIC", "").lower() in {"1", "true", "yes", "on"}
DATA_FILE = Path(os.environ.get("DATA_FILE", ROOT / "data" / "contributions.json"))
OUT = Path(os.environ.get("CARDS_DIR", ROOT / ("cards-static" if STATIC else "cards")))
FLAVOR = os.environ.get("FONT_FLAVOR", "woff2")  # "woff" for plain WOFF
FONT_DIR = ROOT / "fonts"

# ───────────────────────────── CONFIG ─────────────────────────────
# Colours: the only palette your old README chose was the dark / tokyonight theme on its stat
# widgets, so that is the base. Swap in your website's values here; brand drives the whole look.
C = dict(
    card="#1a1b26",   # card background
    edge="#2f3549",   # card outline
    cell="#252a3f",   # empty contribution cell
    text="#c0caf5",   # primary text
    soft="#a9b1d6",   # body copy
    muted="#7f89b4",  # labels
    brand="#7aa2f7",  # accent, charts, contribution graph
)

# Fonts: files live in fonts/ (SIL OFL). Change `file` to use another family.
FONTS = {
    "display": dict(file="bricolage-grotesque-latin-800-normal.woff2", family="Bricolage Grotesque", weight=800),
    "body": dict(file="instrument-sans-latin-400-normal.woff2", family="Instrument Sans", weight=400),
    "strong": dict(file="instrument-sans-latin-600-normal.woff2", family="Instrument Sans", weight=600),
}

TAGLINE = "Big-data and AI engineer building data pipelines for trading and football analytics."
PORTRAIT_GLOB = "portrait.*"  # optional: drop assets/portrait.jpg (or .png/.webp) to show a photo in the hero

# Links live in README.md; keep the order in sync with it.
TILES = [
    dict(icon="mail", title="Email me", sub="ilyas.kibbou@e-polytechnique.ma"),
    dict(icon="wave", title="BeatMix", sub="My favorite side project"),
    dict(icon="chat", title="Follow along", sub="@kibbou_ilyas on X"),
]

# `repo` is owner/name or just name. `blurb` wins over the repo's GitHub description.
FEATURED = [
    dict(repo="ilyas0junior/foot-analysis",
         blurb="Interactive dashboard of Moroccan footballers. Transfermarkt data scraped with "
               "Playwright, cleaned in pandas, explored in Streamlit."),
    dict(repo="HSI-Auto-Trader/Auto-trader",
         blurb="Gold AI Trader: an automated chart-pattern scanner that builds a computer-vision dataset."),
    dict(repo="ilyas0junior/Partenariats-Manager-app",
         blurb="Partnership management app. Express and MongoDB API, React, TypeScript and Vite front end, "
               "shipped with Docker."),
]

# Start time (seconds) of each card, so the page plays top to bottom.
STAGGER = dict(hero=0.0, tiles=(0.9, 1.05, 1.2), activity=1.7, repos=(2.5, 2.75, 3.0), graph=3.4)
# ───────────────────────────────────────────────────────────────────


# ───────────────────────────── helpers ─────────────────────────────
def esc(s) -> str:
    return html.escape(str(s), quote=True)


def n(x: float) -> str:
    s = f"{x:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def mix(a: str, b: str, t: float) -> str:
    pa = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    pb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(pa, pb))


def plural(k: int, word: str) -> str:
    return f"{k:,} {word}" + ("" if k == 1 else "s")


def fmt_date(d: dt.date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def ago(pushed: str | None, today: dt.date) -> str | None:
    if not pushed:
        return None
    d = (today - dt.date.fromisoformat(pushed[:10])).days
    if d <= 0:
        return "Updated today"
    if d == 1:
        return "Updated yesterday"
    if d < 14:
        return f"Updated {d} days ago"
    if d < 60:
        return f"Updated {d // 7} weeks ago"
    if d < 365:
        return f"Updated {d // 30} months ago"
    return f"Updated {d // 365} year{'s' if d >= 730 else ''} ago"


LEVEL_COLORS = [C["cell"]] + [mix(C["cell"], C["brand"], t) for t in (0.32, 0.55, 0.78, 1.0)]


# ───────────────────────────── fonts ─────────────────────────────
class Metrics:
    """Advance widths straight from the font files, so wrapping and truncation are measured, not guessed."""

    def __init__(self, key: str):
        path = FONT_DIR / FONTS[key]["file"]
        if not path.exists():
            sys.exit(f"missing font file: {path}")
        f = TTFont(path)
        self.upm, self.cmap, self.hmtx = f["head"].unitsPerEm, f.getBestCmap(), f["hmtx"]

    def width(self, s: str, size: float, ls: float = 0.0) -> float:
        w = sum(self.hmtx[self.cmap[ord(c)]][0] if ord(c) in self.cmap else self.upm * 0.55 for c in s)
        return w / self.upm * size * 1.015 + ls * len(s)  # +1.5% safety for kerning/rounding


M = {k: Metrics(k) for k in FONTS}
_subset_cache: dict[tuple[str, str], str] = {}


def font_face(key: str, chars: str) -> str:
    """pyftsubset the font down to exactly the glyphs this card uses, as a base64 data URI."""
    chars = "".join(sorted(set(chars) | {" ", "\u2026"}))
    if (key, chars) not in _subset_cache:
        with tempfile.TemporaryDirectory() as td:
            txt, out = Path(td, "chars.txt"), Path(td, f"out.{FLAVOR}")
            txt.write_text(chars, encoding="utf-8")
            subprocess.run(
                [sys.executable, "-m", "fontTools.subset", str(FONT_DIR / FONTS[key]["file"]),  # == pyftsubset
                 f"--text-file={txt}", f"--flavor={FLAVOR}", f"--output-file={out}",
                 "--layout-features=kern,liga", "--no-hinting", "--drop-tables+=DSIG"],
                check=True, capture_output=True,
            )
            _subset_cache[(key, chars)] = base64.b64encode(out.read_bytes()).decode()
    f = FONTS[key]
    return (f"@font-face{{font-family:'{f['family']}';font-weight:{f['weight']};font-style:normal;"
            f"src:url(data:font/{FLAVOR};base64,{_subset_cache[(key, chars)]}) format('{FLAVOR}')}}")


def fit(key: str, s: str, size: float, max_w: float, ls: float = 0.0) -> str:
    if M[key].width(s, size, ls) <= max_w:
        return s
    while s and M[key].width(s + "\u2026", size, ls) > max_w:
        s = s[:-1]
    return s.rstrip() + "\u2026"


def wrap(key: str, text: str, size: float, max_w: float, max_lines: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        t = f"{cur} {word}".strip()
        if cur and M[key].width(t, size) > max_w:
            lines.append(cur)
            cur = word
        else:
            cur = t
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines - 1] + [fit(key, " ".join(lines[max_lines - 1:]), size, max_w)]
    return lines


# ───────────────────────────── svg scaffolding ─────────────────────────────
BASE_CSS = (
    "text{font-family:'Instrument Sans',system-ui,-apple-system,'Segoe UI',sans-serif;"
    "font-kerning:normal;text-rendering:geometricPrecision}"
    ".display{font-family:'Bricolage Grotesque','Instrument Sans',system-ui,sans-serif;font-weight:800}"
    ".body{font-weight:400}.strong{font-weight:600}"
)

# Base styles above are the final frame; each keyframe only names its `from` state.
ANIM_CSS = (
    "@keyframes rise{from{opacity:0;transform:translateY(12px)}}"
    "@keyframes fade{from{opacity:0}}"
    "@keyframes pop{from{opacity:0;transform:scale(.6)}}"
    "@keyframes growY{from{transform:scaleY(0)}}"
    "@keyframes growX{from{transform:scaleX(0)}}"
    "@keyframes draw{from{stroke-dashoffset:1}}"
    ".rise,.fade,.pop,.growY,.growYtop,.growX,.draw{animation-duration:.7s;"
    "animation-timing-function:cubic-bezier(.2,.7,.2,1);animation-delay:var(--d,0s);animation-fill-mode:backwards}"
    ".rise{animation-name:rise}.fade{animation-name:fade}"
    ".pop{animation-name:pop;transform-box:fill-box;transform-origin:50% 50%}"
    ".growY{animation-name:growY;transform-box:fill-box;transform-origin:50% 100%}"
    ".growYtop{animation-name:growY;transform-box:fill-box;transform-origin:50% 0}"
    ".growX{animation-name:growX;transform-box:fill-box;transform-origin:0 50%}"
    ".draw{animation-name:draw;stroke-dasharray:1}"
    "@media (prefers-reduced-motion:reduce){*{animation:none!important}.deco{display:none}}"
)


def anim(kind: str, delay: float, dur: float | None = None) -> str:
    """class/style attributes for an entrance animation; empty in STATIC mode."""
    if STATIC:
        return ""
    style = f"--d:{delay:.2f}s" + (f";animation-duration:{dur}s" if dur else "")
    return f' class="{kind}" style="{style}"'


class Card:
    def __init__(self, name: str, w: int, h: int, title: str, desc: str):
        self.name, self.w, self.h, self.title, self.desc = name, w, h, title, desc
        self.body: list[str] = []
        self.chars = {k: set() for k in FONTS}

    def add(self, markup: str) -> None:
        self.body.append(markup)

    def T(self, x, y, s, font="body", size=16, fill=None, anchor="start", ls=0.0, kind=None, delay=0.0) -> str:
        self.chars[font].update(s)
        cls, style = font, ""
        if kind and not STATIC:
            cls, style = f"{font} {kind}", f' style="--d:{delay:.2f}s"'
        a = f' text-anchor="{anchor}"' if anchor != "start" else ""
        spacing = f' letter-spacing="{ls}"' if ls else ""
        return (f'<text class="{cls}" x="{n(x)}" y="{n(y)}" font-size="{n(size)}" '
                f'fill="{fill or C["text"]}"{a}{spacing}{style}>{esc(s)}</text>')

    def runs(self, x, y, parts, kind=None, delay=0.0) -> str:
        """One <text> made of styled runs: parts = [(text, font, size, fill, dx), ...]. No width maths needed."""
        cls, style = "body", ""
        if kind and not STATIC:
            cls, style = f"body {kind}", f' style="--d:{delay:.2f}s"'
        spans = []
        for s, font, size, fill, dx in parts:
            self.chars[font].update(s)
            d = f' dx="{dx}"' if dx else ""
            spans.append(f'<tspan class="{font}" font-size="{n(size)}" fill="{fill}"{d}>{esc(s)}</tspan>')
        return f'<text class="{cls}" x="{n(x)}" y="{n(y)}"{style}>{"".join(spans)}</text>'

    def svg(self) -> str:
        faces = "".join(font_face(k, "".join(v)) for k, v in self.chars.items() if v)
        css = faces + BASE_CSS + ("" if STATIC else ANIM_CSS)
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
                f'viewBox="0 0 {self.w} {self.h}" role="img" aria-labelledby="t d">'
                f'<title id="t">{esc(self.title)}</title><desc id="d">{esc(self.desc)}</desc>'
                f"<style>{css}</style>{''.join(self.body)}</svg>")


def card_bg(w: int, h: int, r: int) -> str:
    return (f'<rect x=".5" y=".5" width="{w - 1}" height="{h - 1}" rx="{r}" '
            f'fill="{C["card"]}" stroke="{C["edge"]}"/>')


ICONS = {  # 24px grid, stroked
    "mail": ["M3.5 6.5h17v11h-17z", "M4 7.5l8 6 8-6"],
    "wave": ["M4 10v4", "M8 6v12", "M12 9v6", "M16 4v16", "M20 11v2"],
    "chat": ["M4.5 5.5h15v10h-7l-4.5 3.5v-3.5h-3.5z"],
    "arrow": ["M7 17L17 7", "M8.5 7H17v8.5"],
    "star": ["M12 3.5l2.6 5.5 6 .8-4.4 4.2 1.1 6-5.3-2.9-5.3 2.9 1.1-6-4.4-4.2 6-.8z"],
    "fork": ["M6 7.2v1.6c0 1.2 1 2.2 2.2 2.2h7.6c1.2 0 2.2-1 2.2-2.2V7.2", "M12 11v5.8"],
}


def icon_paths(name: str, stroke: str, width: float, draw_delay: float | None = None) -> str:
    out = []
    for d in ICONS[name]:
        extra = f' pathLength="1"{anim("draw", draw_delay, 0.9)}' if draw_delay is not None and not STATIC else ""
        out.append(f'<path d="{d}"{extra}/>')
    return (f'<g fill="none" stroke="{stroke}" stroke-width="{width}" stroke-linecap="round" '
            f'stroke-linejoin="round">{"".join(out)}</g>')


# ───────────────────────────── data ─────────────────────────────
class Data:
    def __init__(self, path: Path):
        u = json.loads(path.read_text(encoding="utf-8"))
        u = u.get("data", {}).get("user", u)  # also accept a raw GraphQL response
        self.login, self.name = u["login"], u.get("name") or u["login"]
        cal = u["contributionsCollection"]["contributionCalendar"]
        self.weeks = [[(dt.date.fromisoformat(d["date"]), d["contributionCount"], d["weekday"])
                       for d in w["contributionDays"]] for w in cal["weeks"]]
        self.days = sorted(d for w in self.weeks for d in w)
        self.total = cal["totalContributions"]
        self.today = min(self.days[-1][0], dt.datetime.now(dt.timezone.utc).date())  # ignore any future days
        self.repos = [r for key in ("pinnedItems", "repositories") for r in (u.get(key) or {}).get("nodes", []) if r]

        counts = {d: c for d, c, _ in self.days}
        d = self.today if counts[self.today] else self.today - dt.timedelta(days=1)
        self.streak = 0
        while counts.get(d, 0) > 0:
            self.streak += 1
            d -= dt.timedelta(days=1)
        self.longest = run = 0
        for _, c, _ in self.days:
            run = run + 1 if c else 0
            self.longest = max(self.longest, run)
        self.active = sum(1 for _, c, _ in self.days if c)
        self.busiest = max(self.days, key=lambda x: (x[1], x[0]))  # latest wins ties

        months: OrderedDict[tuple[int, int], int] = OrderedDict()
        for d, c, _ in self.days:
            months[(d.year, d.month)] = months.get((d.year, d.month), 0) + c
        self.months = [(dt.date(y, m, 1), t) for (y, m), t in months.items()]

        nz = sorted(c for _, c, _ in self.days if c)
        q = [nz[min(len(nz) - 1, int(len(nz) * p))] for p in (0.25, 0.5, 0.75)] if nz else [1, 2, 3]
        self.level = lambda c: 0 if c == 0 else 1 + sum(c > x for x in q)

    def find_repo(self, spec: str):
        want = spec.lower()
        for r in self.repos:
            if r["nameWithOwner"].lower() == want:
                return r
        for r in self.repos:
            if r["name"].lower() == want.split("/")[-1]:
                return r
        return None


def load_portrait() -> str | None:
    files = sorted((ROOT / "assets").glob(PORTRAIT_GLOB))
    if not files:
        return None
    raw = files[0].read_bytes()
    try:
        from PIL import Image, ImageOps
        im = ImageOps.fit(Image.open(io.BytesIO(raw)).convert("RGB"), (240, 240), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=82, optimize=True)
        raw = buf.getvalue()
    except ImportError:
        pass
    mime = "image/jpeg" if raw[:2] == b"\xff\xd8" else "image/png" if raw[:4] == b"\x89PNG" else "image/webp"
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


# ───────────────────────────── cards ─────────────────────────────
def hero(D: Data, portrait: str | None, t0: float) -> Card:
    W, H, X = 860, 264, 44
    c = Card("hero", W, H, D.name, TAGLINE)
    c.add(card_bg(W, H, 22))

    size, ls = 64, -1.5
    while M["display"].width(D.name, size, ls) > 470:
        size -= 2
    name_w = M["display"].width(D.name, size, ls)
    lines = wrap("body", TAGLINE, 20, 470, 3)
    cap = size * 0.72
    block = cap + 57 + 28 * (len(lines) - 1) + 5
    by = (H - block) / 2 + cap  # name baseline

    c.add(c.T(X, by, D.name, "display", size, C["text"], ls=ls, kind="rise", delay=t0 + 0.15))
    c.add(f'<rect{anim("growX", t0 + 0.5, 0.9)} x="{X}" y="{n(by + 18)}" width="{n(name_w)}" height="3" rx="1.5" fill="{C["brand"]}"/>')
    for i, line in enumerate(lines):
        c.add(c.T(X, by + 57 + 28 * i, line, "body", 20, C["soft"], kind="rise", delay=t0 + 0.8 + 0.12 * i))

    # the last 17 weeks of real activity, drawn as the same cells the graph card uses
    cols, pitch, cs = D.weeks[-17:], 17, 13
    gw = len(cols) * pitch - 4
    gx, gy = W - X - gw, (H - (7 * pitch - 4)) / 2
    pcx, pcy, R = gx + gw / 2, H / 2, 50
    placed = []
    for ci, wk in enumerate(cols):
        for date, cnt, wd in wk:
            x, y = gx + ci * pitch, gy + wd * pitch
            if portrait and (x + cs / 2 - pcx) ** 2 + (y + cs / 2 - pcy) ** 2 < (R + 16) ** 2:
                continue
            placed.append((x, y, cnt))
            c.add(f'<rect{anim("pop", t0 + 0.35 + (ci + wd) * 0.035, 0.5)} x="{n(x)}" y="{n(y)}" width="{cs}" '
                  f'height="{cs}" rx="3" fill="{LEVEL_COLORS[D.level(cnt)]}"/>')
    if portrait:
        c.add(f'<g{anim("pop", t0 + 0.6, 0.8)}><circle cx="{n(pcx)}" cy="{n(pcy)}" r="{R + 3}" fill="{C["card"]}" '
              f'stroke="{C["brand"]}" stroke-width="2"/><clipPath id="pc"><circle cx="{n(pcx)}" cy="{n(pcy)}" r="{R}"/>'
              f'</clipPath><image href="{portrait}" x="{n(pcx - R)}" y="{n(pcy - R)}" width="{2 * R}" height="{2 * R}" '
              f'preserveAspectRatio="xMidYMid slice" clip-path="url(#pc)"/></g>')
    if not STATIC:  # decoration: three of your most recent active days twinkle twice, then rest
        active = [p for p in placed if p[2]][-3:]
        for k, (x, y, _) in enumerate(active):
            c.add(f'<rect class="deco" x="{n(x)}" y="{n(y)}" width="{cs}" height="{cs}" rx="3" fill="{C["brand"]}" opacity="0">'
                  f'<animate attributeName="opacity" values="0;.95;0" dur="1.8s" begin="{t0 + 2.4 + k * 0.5:.1f}s" '
                  f'repeatCount="2"/></rect>')
    return c


def tile(spec: dict, i: int, t0: float) -> Card:
    # The README puts the three tiles next to each other with no whitespace between the tags (whitespace
    # makes 3 x 32.5% wrap on narrow phones), so the gap between tiles lives here as transparent margin.
    GAP, W, H = 4, 280, 132
    c = Card(f"tile-{i + 1}", W + 2 * GAP, H, spec["title"], spec["sub"])
    sub_size = 15.0  # largest size that fits on one line (never truncate an email address if 13px will do)
    while sub_size > 13 and M["body"].width(spec["sub"], sub_size) > W - 44:
        sub_size -= 0.5
    sub = fit("body", spec["sub"], sub_size, W - 44)
    c.add(f'<g transform="translate({GAP} 0)"><g{anim("rise", t0, 0.7)}>' + card_bg(W, H, 14)
          + f'<g transform="translate(22 20) scale(1.15)">{icon_paths(spec["icon"], C["brand"], 1.9, t0 + 0.35)}</g>'
          + f'<g transform="translate({W - 22 - 18} 22) scale(.75)">{icon_paths("arrow", C["muted"], 2.2)}</g>'
          + c.T(22, 88, spec["title"], "display", 26, C["text"], ls=-0.4)
          + c.T(22, 112, sub, "body", sub_size, C["muted"]) + "</g></g>")
    return c


def activity(D: Data, t0: float) -> Card:
    W, H = 860, 292
    b = D.busiest
    c = Card("activity", W, H, "Contributions per month",
             f"Bar chart of contributions per month over the last year. Current streak {plural(D.streak, 'day')}, "
             f"longest {plural(D.longest, 'day')}. {plural(D.active, 'active day')}. "
             f"Busiest day {fmt_date(b[0])} with {plural(b[1], 'contribution')}.")
    c.add(card_bg(W, H, 16))

    stats = [(str(D.streak), plural(D.streak, "day").split(" ", 1)[1], f"Current streak, longest {D.longest}"),
             (f"{D.active:,}", "days", "Active in the last year"),
             (str(b[1]), "contributions" if b[1] != 1 else "contribution", f"Busiest day, {fmt_date(b[0])}" if b[1] else "Busiest day")]
    for i, (val, unit, label) in enumerate(stats):
        y, d = 80 + i * 72, t0 + 0.1 + i * 0.14
        c.add(f'<g{anim("rise", d)}>'
              + c.runs(36, y, [(val, "display", 44, C["text"], 0), (unit, "strong", 17, C["soft"], 8)])
              + c.T(36, y + 24, fit("body", label, 15, 220), "body", 15, C["muted"]) + "</g>")
    c.add(f'<rect x="268" y="32" width="1" height="{H - 64}" fill="{C["edge"]}"/>')

    x0, x1, base, maxh = 300, W - 36, 232, 148
    c.add(c.T(x0, 44, "Contributions per month", "strong", 16, C["soft"], kind="fade", delay=t0 + 0.1))
    k = len(D.months)
    pitch = (x1 - x0) / k
    bw = min(28, pitch * 0.64)
    top = max(t for _, t in D.months) or 1
    hi = max(range(k), key=lambda i: D.months[i][1])
    for i, (month, total) in enumerate(D.months):
        cx = x0 + pitch * (i + 0.5)
        h = max(3, total / top * maxh) if total else 2
        d = t0 + 0.35 + i * 0.06
        fill = C["brand"] if i == hi else mix(C["card"], C["brand"], 0.5) if total else C["edge"]
        c.add(f'<rect{anim("growY", d, 0.8)} x="{n(cx - bw / 2)}" y="{n(base - h)}" width="{n(bw)}" height="{n(h)}" '
              f'rx="4" fill="{fill}"/>')
        if total:
            c.add(c.T(cx, base - h - 8, f"{total:,}", "strong", 14, C["text"] if i == hi else C["soft"],
                      anchor="middle", kind="fade", delay=d + 0.5))
        c.add(c.T(cx, base + 22, f"{month:%b}", "body", 14, C["muted"], anchor="middle"))
        if i == 0 or month.month == 1:
            c.add(c.T(cx, base + 38, f"\u2019{month:%y}", "body", 13, C["muted"], anchor="middle"))
    c.add(f'<rect{anim("growX", t0 + 0.3, 1.0)} x="{x0}" y="{base}" width="{x1 - x0}" height="1" fill="{C["edge"]}"/>')
    return c


def repo_card(spec: dict, r: dict | None, D: Data, i: int, t0: float) -> Card:
    W, H, LEFT = 860, 140, 34
    r = r or {}
    full = r.get("nameWithOwner") or spec["repo"]
    owner, name = full.split("/") if "/" in full else (D.login, full)
    blurb = spec.get("blurb") or r.get("description") or ""
    lang = r.get("primaryLanguage") or {}
    spine = lang.get("color") or C["brand"]
    c = Card(f"repo-{i + 1}", W, H, full, blurb)
    c.add(f'<g{anim("rise", t0, 0.7)}>' + card_bg(W, H, 16))
    c.add(f'<clipPath id="rc"><rect x=".5" y=".5" width="{W - 1}" height="{H - 1}" rx="16"/></clipPath>'
          f'<g clip-path="url(#rc)"><rect{anim("growYtop", t0 + 0.1, 0.8)} x="0" y="0" width="6" height="{H}" fill="{spine}"/></g>')

    pre = fit("strong", f"{owner}/", 18, 200) if owner.lower() != D.login.lower() else ""
    room = W - LEFT - 90 - (M["strong"].width(pre, 18) if pre else 0)
    parts = ([(pre, "strong", 18, C["muted"], 0)] if pre else []) + [(fit("display", name, 26, room, -0.3), "display", 26, C["text"], 2 if pre else 0)]
    c.add(c.runs(LEFT, 46, parts))
    c.add(f'<g transform="translate({W - 34 - 18} 28) scale(.75)">{icon_paths("arrow", C["muted"], 2.2)}</g>')
    for j, line in enumerate(wrap("body", blurb, 16, 700, 2) if blurb else []):
        c.add(c.T(LEFT, 76 + 22 * j, line, "body", 16, C["soft"]))

    x, y = LEFT, 124
    if lang.get("name"):
        c.add(f'<circle cx="{x + 5}" cy="{y - 5}" r="5" fill="{spine}"/>')
        c.add(c.T(x + 18, y, lang["name"], "strong", 15, C["soft"]))
        x += 18 + M["strong"].width(lang["name"], 15) + 22
    for ic, val in (("star", r.get("stargazerCount")), ("fork", r.get("forkCount"))):
        if val is None:
            continue
        c.add(f'<g transform="translate({n(x)} {y - 14}) scale(.7)">{icon_paths(ic, C["muted"], 2)}</g>')
        c.add(c.T(x + 22, y, str(val), "strong", 15, C["soft"]))
        x += 22 + M["strong"].width(str(val), 15) + 20
    when = ago(r.get("pushedAt"), D.today)
    if when:
        c.add(c.T(x, y, when, "body", 15, C["muted"]))
    c.add("</g>")
    return c


def graph(D: Data, t0: float) -> Card:
    W, LEFT, GX, RIGHT = 860, 28, 60, 28
    nW = len(D.weeks)
    pitch = (W - GX - RIGHT) / nW
    cs = pitch - 3
    top = 86
    H = round(top + 7 * pitch + 22)
    c = Card("graph", W, H, f"{D.total:,} contributions in the last year",
             f"Contribution graph for the last year, {plural(D.total, 'contribution')} in total.")
    c.add(card_bg(W, H, 16))
    c.add(c.T(LEFT, 46, f"{D.total:,} contributions in the last year", "display", 22, C["text"], ls=-0.3,
              kind="rise", delay=t0))

    # legend, right-aligned
    lx = W - RIGHT
    more = M["body"].width("More", 13)
    c.add(c.T(lx, 45, "More", "body", 13, C["muted"], anchor="end"))
    lx -= more + 8
    for lvl in range(4, -1, -1):
        c.add(f'<rect x="{n(lx - 11)}" y="35" width="11" height="11" rx="3" fill="{LEVEL_COLORS[lvl]}"/>')
        lx -= 15
    c.add(c.T(lx - 4, 45, "Less", "body", 13, C["muted"], anchor="end"))

    for row, label in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        c.add(c.T(LEFT, top + row * pitch + cs - 1.5, label, "body", 13, C["muted"]))
    last_m, last_x = None, -99
    ring = None
    for wi, wk in enumerate(D.weeks):
        x = GX + wi * pitch
        m = wk[0][0].month
        if m != last_m and x - last_x >= 34:
            c.add(c.T(x, top - 10, f"{wk[0][0]:%b}", "body", 13, C["muted"], kind="fade", delay=t0 + 0.2 + wi * 0.022))
            last_m, last_x = m, x
        cells = []
        for date, cnt, wd in wk:
            y = top + wd * pitch
            cells.append(f'<rect x="{n(x)}" y="{n(y)}" width="{n(cs)}" height="{n(cs)}" rx="3" '
                         f'fill="{LEVEL_COLORS[D.level(cnt)]}"/>')
            if date == D.today:
                ring = (x, y)
        c.add(f'<g{anim("fade", t0 + 0.2 + wi * 0.022, 0.5)}>{"".join(cells)}</g>')

    if ring:  # today's cell: a ring that pulses three times once the sweep has finished (decoration)
        x, y = ring
        if STATIC:
            c.add(f'<rect class="deco" x="{n(x - 2)}" y="{n(y - 2)}" width="{n(cs + 4)}" height="{n(cs + 4)}" rx="4" '
                  f'fill="none" stroke="{C["brand"]}" stroke-width="1.5" stroke-opacity=".9"/>')
        else:
            begin, dur = t0 + 0.2 + nW * 0.022 + 0.6, "1.8s"
            ani = lambda attr, v: f'<animate attributeName="{attr}" values="{v}" dur="{dur}" begin="{begin:.1f}s" repeatCount="3"/>'
            c.add(f'<rect class="deco" x="{n(x - 1)}" y="{n(y - 1)}" width="{n(cs + 2)}" height="{n(cs + 2)}" rx="4" '
                  f'fill="none" stroke="{C["brand"]}" stroke-width="1.5" stroke-opacity="0">'
                  + ani("stroke-opacity", ".9;0") + ani("x", f"{n(x - 1)};{n(x - 7)}") + ani("y", f"{n(y - 1)};{n(y - 7)}")
                  + ani("width", f"{n(cs + 2)};{n(cs + 14)}") + ani("height", f"{n(cs + 2)};{n(cs + 14)}") + "</rect>")
    return c


# ───────────────────────────── main ─────────────────────────────
def main() -> None:
    D = Data(DATA_FILE)
    portrait = load_portrait()
    cards = [hero(D, portrait, STAGGER["hero"])]
    cards += [tile(s, i, STAGGER["tiles"][min(i, len(STAGGER["tiles"]) - 1)]) for i, s in enumerate(TILES)]
    cards.append(activity(D, STAGGER["activity"]))
    for i, spec in enumerate(FEATURED):
        t = STAGGER["repos"][min(i, len(STAGGER["repos"]) - 1)] + max(0, i - 2) * 0.25
        cards.append(repo_card(spec, D.find_repo(spec["repo"]), D, i, t))
    cards.append(graph(D, STAGGER["graph"]))

    OUT.mkdir(parents=True, exist_ok=True)
    for old in [*OUT.glob("hero.svg"), *OUT.glob("tile-*.svg"), *OUT.glob("repo-*.svg"),
                *OUT.glob("activity.svg"), *OUT.glob("graph.svg")]:
        old.unlink()
    for card in cards:
        (OUT / f"{card.name}.svg").write_text(card.svg(), encoding="utf-8")
    print(f"{'STATIC' if STATIC else 'animated'} cards -> {OUT}")
    for card in cards:
        print(f"  {card.name + '.svg':<14}{(OUT / (card.name + '.svg')).stat().st_size / 1024:6.1f} KB")

    if STATIC:  # preview.html shows README.md exactly as GitHub lays it out, for screenshots
        readme = (ROOT / "README.md").read_text(encoding="utf-8").replace('src="cards/', 'src="')
        (OUT / "preview.html").write_text(
            "<!doctype html><meta charset=utf-8><title>Profile preview</title><style>"
            "body{margin:0;background:#0d1117;color:#e6edf3;font:16px/1.5 system-ui,sans-serif}"
            "main{width:830px;margin:0 auto;padding:32px 0}p{margin:0 0 16px}img{max-width:100%}"
            f"</style><main>{readme}</main>", encoding="utf-8")
        print(f"  open {OUT / 'preview.html'} and screenshot it")


if __name__ == "__main__":
    main()

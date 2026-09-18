"""
Decode minified browser stack traces using sourcemaps from app build dirs.

Map lookup order:
  1. data/sourcemaps/**/<file>.js.map   (explicit drop-in dir, e.g. copied maps)
  2. <apps_root>/<app>/frontend/dist/assets/<file>.js.map        (Vite builds)
  3. <apps_root>/<app>/frontend/build/static/js/<file>.js.map    (CRA builds)

apps_root defaults to SOFIA_APPS_ROOT env or the repo's sibling apps dir.
Only the "mappings" VLQ decode needed for frame lookup is implemented.
"""
import bisect
import json
import logging
import os
import re
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger("sofia.sourcemaps")

_DEFAULT_APPS_ROOT = Path(__file__).resolve().parents[4]  # .../apps
APPS_ROOT = Path(os.getenv("SOFIA_APPS_ROOT", str(_DEFAULT_APPS_ROOT)))
DATA_DIR = Path(os.getenv("SOFIA_DATA_DIR", str(Path(__file__).resolve().parents[2] / "data")))
SOURCEMAP_DIR = DATA_DIR / "sourcemaps"

_B64 = {c: i for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/")}
_FRAME_RE = re.compile(r"(https?://\S+?\.(?:js|mjs)):(\d+):(\d+)")

# path -> (mtime, parsed)
_map_cache: dict = {}


def _decode_vlq(text: str, pos: int) -> Tuple[int, int]:
    result = shift = 0
    while True:
        digit = _B64[text[pos]]
        pos += 1
        result |= (digit & 31) << shift
        if not digit & 32:
            break
        shift += 5
    value = result >> 1
    return (-value if result & 1 else value), pos


def _decode_mappings(mappings: str) -> List[List[dict]]:
    """Decode a sourcemap v3 mappings string into per-line segment lists.

    src/line/scol/name fields accumulate across the whole mappings string;
    gen col accumulates per line. Each returned segment dict has keys
    col, src, line, scol, and optionally name (all resolved absolutes).
    """
    lines: List[List[dict]] = []
    src_idx = src_line = src_col = name_idx = 0
    for line_str in mappings.split(";"):
        segs: List[dict] = []
        gen_col = 0
        pos = 0
        n = len(line_str)
        while pos < n:
            fields: List[int] = []
            while pos < n and line_str[pos] != ",":
                value, pos = _decode_vlq(line_str, pos)
                fields.append(value)
            if pos < n and line_str[pos] == ",":
                pos += 1
            if not fields:
                continue
            gen_col += fields[0]
            seg = {"col": gen_col}
            if len(fields) >= 4:
                src_idx += fields[1]
                src_line += fields[2]
                src_col += fields[3]
                seg["src"], seg["line"], seg["scol"] = src_idx, src_line, src_col
                if len(fields) >= 5:
                    name_idx += fields[4]
                    seg["name"] = name_idx
            segs.append(seg)
        lines.append(segs)
    return lines


def _load_map(path: Path) -> Optional[dict]:
    try:
        mtime = path.stat().st_mtime
        cached = _map_cache.get(str(path))
        if cached and cached[0] == mtime:
            return cached[1]
        obj = json.loads(path.read_text(encoding="utf-8"))
        parsed = {
            "sources": obj.get("sources", []),
            "names": obj.get("names", []),
            "sourceRoot": obj.get("sourceRoot", ""),
            "lines": _decode_mappings(obj.get("mappings", "")),
        }
        _map_cache[str(path)] = (mtime, parsed)
        return parsed
    except Exception as exc:
        logger.debug(f"[sourcemaps] could not load {path}: {exc}")
        return None


def _find_map(basename: str) -> Optional[Path]:
    if SOURCEMAP_DIR.exists():
        for hit in sorted(SOURCEMAP_DIR.rglob(f"{basename}.map")):
            return hit
    for pattern in (
        f"*/frontend/dist/assets/{basename}.map",
        f"*/frontend/build/static/js/{basename}.map",
    ):
        for hit in sorted(APPS_ROOT.glob(pattern)):
            return hit
    return None


def _resolve(parsed: dict, gen_line: int, gen_col: int) -> Optional[Tuple[str, int, int, Optional[str]]]:
    lines = parsed["lines"]
    if gen_line < 1 or gen_line > len(lines):
        return None
    segs = lines[gen_line - 1]
    cols = [s["col"] for s in segs]
    idx = bisect.bisect_right(cols, gen_col) - 1
    while idx >= 0 and "src" not in segs[idx]:
        idx -= 1
    if idx < 0:
        return None
    seg = segs[idx]
    sources = parsed["sources"]
    if seg["src"] >= len(sources):
        return None
    source = parsed["sourceRoot"] + sources[seg["src"]]
    name = None
    if "name" in seg and seg["name"] < len(parsed["names"]):
        name = parsed["names"][seg["name"]]
    return source, seg["line"] + 1, seg["scol"] + 1, name


def decode_browser_traceback(traceback: str) -> Optional[str]:
    """Rewrite minified frames to original source positions.

    Returns the decoded stack, or None if no frame could be resolved.
    Each resolved `url:line:col` becomes `source:line:col` (plus `[name]`
    when the map carries a function name for that segment).
    """
    resolved_any = False

    def repl(match: re.Match) -> str:
        nonlocal resolved_any
        url, gen_line, gen_col = match.group(1), int(match.group(2)), int(match.group(3))
        path = _find_map(url.rsplit("/", 1)[-1])
        parsed = _load_map(path) if path is not None else None
        hit = _resolve(parsed, gen_line, gen_col) if parsed is not None else None
        if hit is None:
            return match.group(0)
        resolved_any = True
        source, sline, scol, name = hit
        return f"{source}:{sline}:{scol}" + (f" [{name}]" if name else "")

    decoded = "\n".join(_FRAME_RE.sub(repl, line) for line in traceback.splitlines())
    return decoded if resolved_any else None

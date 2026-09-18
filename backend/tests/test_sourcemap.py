"""Sourcemap decoder: VLQ mappings decode and browser frame rewriting."""
import json
from pathlib import Path

from app.services import sourcemap_service


def _vlq(value: int) -> str:
    """Encode one signed int as a base64-VLQ group (test-side encoder)."""
    chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    vlq = ((-value) << 1 | 1) if value < 0 else (value << 1)
    out = []
    while True:
        digit = vlq & 31
        vlq >>= 5
        out.append(chars[digit | (32 if vlq else 0)])
        if not vlq:
            return "".join(out)


def _segment(*fields: int) -> str:
    return "".join(_vlq(f) for f in fields)


def _write_map(directory: Path, basename: str, mappings: str,
               sources=None, names=None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{basename}.map"
    path.write_text(json.dumps({
        "version": 3,
        "sources": sources or ["src/App.tsx"],
        "names": names or ["handleClick"],
        "sourceRoot": "",
        "mappings": mappings,
    }), encoding="utf-8")
    return path


def test_decode_frame_via_sourcemap_dir(tmp_path, monkeypatch):
    # gen line 1, col 0 -> sources[0] line 9 (0-based) col 4, name handleClick
    _write_map(tmp_path / "sourcemaps", "index-abc.js", _segment(0, 0, 9, 4, 0))
    monkeypatch.setattr(sourcemap_service, "SOURCEMAP_DIR", tmp_path / "sourcemaps")
    monkeypatch.setattr(sourcemap_service, "APPS_ROOT", tmp_path / "no-apps")

    tb = (
        "TypeError: boom\n"
        "    at handleClick (https://app/assets/index-abc.js:1:0)\n"
        "    at https://app/assets/index-abc.js:1:5123"
    )
    decoded = sourcemap_service.decode_browser_traceback(tb)

    assert decoded is not None
    assert "src/App.tsx:10:5 [handleClick]" in decoded
    assert "index-abc.js" not in decoded.splitlines()[1]


def test_decode_frame_via_apps_dist(tmp_path, monkeypatch):
    # Two segments on gen line 1: col 0 -> line 9, col 100 -> line 20
    mappings = _segment(0, 0, 9, 4, 0) + "," + _segment(100, 0, 11, 2, 0)
    dist = tmp_path / "apps" / "myapp" / "frontend" / "dist" / "assets"
    _write_map(dist, "vendor-xyz.js", mappings)
    monkeypatch.setattr(sourcemap_service, "SOURCEMAP_DIR", tmp_path / "empty")
    monkeypatch.setattr(sourcemap_service, "APPS_ROOT", tmp_path / "apps")

    tb = "at go (https://h/assets/vendor-xyz.js:1:150)"
    decoded = sourcemap_service.decode_browser_traceback(tb)

    # col 150 -> the col-100 segment; srcLine/srcCol accumulate globally:
    # line 9+11+1=21, col 4+2+1=7
    assert decoded is not None
    assert "src/App.tsx:21:7 [handleClick]" in decoded


def test_unresolvable_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(sourcemap_service, "SOURCEMAP_DIR", tmp_path / "none")
    monkeypatch.setattr(sourcemap_service, "APPS_ROOT", tmp_path / "none")
    tb = "at x (https://h/assets/missing.js:1:1)"
    assert sourcemap_service.decode_browser_traceback(tb) is None


def test_non_frame_lines_untouched(tmp_path, monkeypatch):
    _write_map(tmp_path / "sourcemaps", "a.js", _segment(0, 0, 0, 0, 0))
    monkeypatch.setattr(sourcemap_service, "SOURCEMAP_DIR", tmp_path / "sourcemaps")
    monkeypatch.setattr(sourcemap_service, "APPS_ROOT", tmp_path / "none")
    tb = "plain text with no frames\n    at y (https://h/a.js:1:0)"
    decoded = sourcemap_service.decode_browser_traceback(tb)
    assert decoded is not None
    assert decoded.splitlines()[0] == "plain text with no frames"

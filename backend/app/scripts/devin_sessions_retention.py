"""Retencion de sesiones del Devin CLI (sessions.db).

El CLI guarda todos los message nodes de todas las sesiones. Al 2026-11-08:
  - local:  10.3 GB + 1.4 GB de WAL (346 sesiones, 384k nodos)
  - magnate: 17.9 GB + 0.6 GB de WAL
Eso no es un fallo, pero llena disco y alarga el arranque de cada sesion
("session DB inventory at boot").

Este script NO borra nada por defecto: lista las sesiones mas antiguas que
`--days` y su tamano estimado. Con `--apply` borra cada una con el comando
oficial `devin rm <id> --force` (no toca SQLite a mano). `--vacuum` es
opcional y solo debe usarse sin sesiones activas.

Uso:
  python devin_sessions_retention.py                    # dry-run, 30 dias
  python devin_sessions_retention.py --days 45 --apply
  python devin_sessions_retention.py --apply --vacuum   # sin sesiones activas

En magnate:
  python3 devin_sessions_retention.py --db /root/.local/share/devin/cli/sessions.db \
      --devin /root/.local/bin/devin --days 45
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path


def default_db() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", "")) / "devin" / "cli" / "sessions.db"
    return Path.home() / ".local" / "share" / "devin" / "cli" / "sessions.db"


def default_devin() -> str:
    if os.name == "nt":
        cand = Path(os.environ.get("LOCALAPPDATA", "")) / "devin" / "cli" / "bin" / "devin.exe"
        return str(cand) if cand.exists() else "devin.exe"
    cand = Path.home() / ".local" / "bin" / "devin"
    return str(cand) if cand.exists() else "devin"


def fetch_candidates(db: Path, days: int) -> list[dict]:
    cutoff = (dt.datetime.now() - dt.timedelta(days=days)).timestamp()
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    con.execute("PRAGMA query_only=ON")
    try:
        rows = con.execute(
            """
            select s.id,
                   coalesce(s.last_activity_at, s.created_at) as last_ts,
                   s.created_at,
                   coalesce(s.working_directory, ''),
                   coalesce(s.title, ''),
                   (select count(*) from message_nodes m
                     where m.session_id = s.id) as nodes
              from sessions s
             where coalesce(s.last_activity_at, s.created_at) < ?
             order by last_ts asc
            """,
            (cutoff,),
        ).fetchall()
    finally:
        con.close()
    out = []
    for sid, last_ts, created, cwd, title, nodes in rows:
        out.append({
            "id": sid,
            "last": dt.datetime.fromtimestamp(last_ts).strftime("%Y-%m-%d %H:%M"),
            "created": dt.datetime.fromtimestamp(created).strftime("%Y-%m-%d"),
            "cwd": cwd,
            "title": (title or "")[:50],
            "nodes": int(nodes or 0),
        })
    return out


def active_session_ids() -> set[str]:
    """Sesiones con procesos devin vivos no se deben borrar."""
    alive: set[str] = set()
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"Name='devin.exe'\" | "
                 "Select-Object -ExpandProperty CommandLine"],
                capture_output=True, text=True, timeout=30).stdout
        else:
            out = subprocess.run(["pgrep", "-af", "devin"], capture_output=True,
                                 text=True, timeout=30).stdout
    except Exception:
        return alive
    for line in out.splitlines():
        for token in line.split():
            token = token.strip("\"'")
            if token and not token.startswith("-") and "/" not in token \
                    and "\\" not in token and len(token) > 3:
                alive.add(token)
    return alive


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(default_db()))
    ap.add_argument("--devin", default=default_devin())
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--max", type=int, default=200,
                    help="tope de sesiones a borrar en una pasada (default 200)")
    ap.add_argument("--apply", action="store_true",
                    help="borra de verdad con `devin rm` (default: dry-run)")
    ap.add_argument("--vacuum", action="store_true",
                    help="VACUUM despues de borrar (requiere que no haya sesiones activas)")
    ap.add_argument("--vacuum-only", action="store_true",
                    help="solo VACUUM (no borra sesiones); aborta si hay devin activo")
    ap.add_argument("--maintenance", action="store_true",
                    help="checkpoint + VACUUM solo si la DB esta libre; pensado para una tarea horaria")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"ERROR: no existe {db}")
        return 2
    size = db.stat().st_size
    wal = db.with_name(db.name + "-wal")
    wal_size = wal.stat().st_size if wal.exists() else 0
    print(f"DB: {db}  ({size / 1e9:.2f} GB, WAL {wal_size / 1e9:.2f} GB)")
    print(f"Corte: actividad anterior a {args.days} dias")

    if args.vacuum_only:
        return run_vacuum(db)
    if args.maintenance:
        return run_maintenance(db)

    candidates = fetch_candidates(db, args.days)
    if not candidates:
        print("Nada que borrar.")
        return 0
    nodes = sum(c["nodes"] for c in candidates)
    print(f"Candidatas: {len(candidates)} sesiones, {nodes} nodos")
    for c in candidates[:10]:
        print(f"  {c['id']:<24} {c['last']}  nodos={c['nodes']:<6} {c['cwd']}  {c['title']}")
    if len(candidates) > 10:
        print(f"  ... y {len(candidates) - 10} mas")

    if not args.apply:
        print("\nDRY RUN: no se borro nada. Usa --apply para borrar.")
        return 0

    if not shutil.which(args.devin) and not Path(args.devin).exists():
        print(f"ERROR: no encuentro el CLI en {args.devin}")
        return 2

    protected = active_session_ids()
    todo = [c for c in candidates if c["id"] not in protected][: args.max]
    skipped = [c for c in candidates if c["id"] in protected]
    if skipped:
        print(f"Omitidas por estar en uso: {', '.join(c['id'] for c in skipped)}")
    print(f"Borrando {len(todo)} sesiones con `devin rm --force`...")

    ok = failed = 0
    for i, c in enumerate(todo, 1):
        try:
            proc = subprocess.run([args.devin, "rm", c["id"], "--force"],
                                  capture_output=True, text=True, timeout=120)
            if proc.returncode == 0:
                ok += 1
            else:
                failed += 1
                print(f"  FALLO {c['id']}: {(proc.stderr or proc.stdout).strip()[:200]}")
        except Exception as exc:
            failed += 1
            print(f"  FALLO {c['id']}: {exc}")
        if i % 20 == 0:
            print(f"  {i}/{len(todo)}...")
    print(f"Borradas: {ok}  Fallidas: {failed}")

    if args.vacuum:
        run_vacuum(db)
    return 0


def run_maintenance(db: Path) -> int:
    """Checkpoint + VACUUM solo si la DB no esta en uso.

    Leccion 2026-11-09: un VACUUM con lectores abiertos en modo WAL escribe
    todas las paginas al WAL (8,3 GB) y no encoge el archivo principal. El
    orden correcto es checkpoint(TRUNCATE) y, solo si no quedo busy, VACUUM.
    Pensado para una tarea horaria: si esta ocupada, no hace nada y reintenta.
    """
    before_db = db.stat().st_size / 1e9
    wal = db.with_name(db.name + "-wal")
    before_wal = wal.stat().st_size / 1e9 if wal.exists() else 0.0
    print(f"Antes: DB {before_db:.2f} GB, WAL {before_wal:.2f} GB")

    con = sqlite3.connect(str(db), timeout=10)
    try:
        con.execute("PRAGMA busy_timeout=10000")
        busy, log_frames, ckpt_frames = con.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    except sqlite3.Error as exc:
        print(f"checkpoint fallo: {exc}")
        return 0
    finally:
        con.close()
    if busy:
        print("DB en uso (checkpoint busy); no se compacta ahora. Reintento luego.")
        return 0
    print(f"checkpoint ok (frames={log_frames}/{ckpt_frames}); VACUUM...")
    rc = run_vacuum(db)
    wal = db.with_name(db.name + "-wal")
    after_wal = wal.stat().st_size / 1e9 if wal.exists() else 0.0
    print(f"Despues: DB {db.stat().st_size / 1e9:.2f} GB, WAL {after_wal:.2f} GB")
    return rc


def run_vacuum(db: Path) -> int:
    """VACUUM con red de seguridad: no corre si hay procesos devin activos."""
    if active_session_ids():
        print("VACUUM omitido: hay procesos devin activos. Cierra las sesiones "
              "y vuelve a ejecutar con --vacuum-only.")
        return 0
    before = db.stat().st_size / 1e9
    print(f"Ejecutando VACUUM sobre {before:.2f} GB (puede tardar varios minutos)...")
    con = sqlite3.connect(str(db))
    try:
        con.execute("PRAGMA busy_timeout=60000")
        con.execute("VACUUM")
    except sqlite3.Error as exc:
        print(f"VACUUM fallo: {exc}")
        return 1
    finally:
        con.close()
    print(f"VACUUM listo. DB ahora {db.stat().st_size / 1e9:.2f} GB")
    return 0


if __name__ == "__main__":
    sys.exit(main())

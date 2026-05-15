#!/usr/bin/env python3
"""
funplaneviewer-uploads: tiny Flask sidecar for shared, server-side
storage of manual image links and backup snapshots.

Listens on 127.0.0.1:5174 by default (override with $PORT).
nginx fronts it under /api/uploads/ on the GUI host.

Storage:
  $DATA_DIR/images.csv  - plane-alert-db schema:
      $ICAO,$Registration,#ImageLink,#ImageLink2,#ImageLink3,#ImageLink4
  $DATA_DIR/backup.json - JSON snapshot, same shape as the
      browser's `importedBackups` { mil, gov, civ } object.

No auth: meant to sit behind nginx on a LAN/Tailscale-only host.
"""

import csv
import io
import json
import os
import threading
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file

DATA_DIR = Path(os.environ.get("FUNPLANEVIEWER_DATA_DIR", "/opt/funplaneviewer/data"))
IMAGES_CSV = DATA_DIR / "images.csv"
BACKUP_JSON = DATA_DIR / "backup.json"
PORT = int(os.environ.get("PORT", "5174"))
HOST = os.environ.get("HOST", "127.0.0.1")

CSV_HEADER = ["$ICAO", "$Registration", "#ImageLink", "#ImageLink2", "#ImageLink3", "#ImageLink4"]

# Global lock — single-process, low traffic; cheap correctness over throughput.
_lock = threading.Lock()

app = Flask(__name__)


@app.after_request
def _allow_cors(response):
    """Permissive CORS — service is LAN/Tailscale-only behind nginx, and
    the GUI may be served from a different origin during development."""
    response.headers.setdefault("Access-Control-Allow-Origin", "*")
    response.headers.setdefault("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
    response.headers.setdefault("Access-Control-Allow-Headers", "Content-Type")
    return response


@app.route("/api/uploads/<path:_>", methods=["OPTIONS"])
def _cors_preflight(_):
    return ("", 204)


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _normalize_hex(value) -> str:
    return str(value or "").strip().upper()


def _read_images_rows():
    """Return a dict { hex: { 'registration': str, 'links': [str, ...] } }."""
    if not IMAGES_CSV.exists():
        return {}
    out = {}
    with IMAGES_CSV.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            hex_val = _normalize_hex(row.get("$ICAO") or row.get("ICAO"))
            if not hex_val:
                continue
            links = [
                (row.get(col) or "").strip()
                for col in ("#ImageLink", "#ImageLink2", "#ImageLink3", "#ImageLink4")
            ]
            links = [l for l in links if l]
            if not links:
                continue
            out[hex_val] = {
                "registration": (row.get("$Registration") or "").strip(),
                "links": links,
            }
    return out


def _write_images_rows(rows) -> None:
    """Atomically write the images.csv file."""
    _ensure_data_dir()
    tmp = IMAGES_CSV.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(CSV_HEADER)
        for hex_val in sorted(rows.keys()):
            entry = rows[hex_val]
            links = list(entry.get("links") or [])
            links += [""] * (4 - len(links))
            writer.writerow([hex_val, entry.get("registration", ""), *links[:4]])
    tmp.replace(IMAGES_CSV)


@app.get("/api/uploads/health")
def health():
    return jsonify(ok=True)


@app.get("/api/uploads/images.csv")
def get_images_csv():
    _ensure_data_dir()
    if not IMAGES_CSV.exists():
        # Return an empty-but-valid CSV so the client merge logic still works.
        buf = io.StringIO()
        csv.writer(buf).writerow(CSV_HEADER)
        return (buf.getvalue(), 200, {"Content-Type": "text/csv; charset=utf-8"})
    return send_file(
        IMAGES_CSV,
        mimetype="text/csv",
        as_attachment=False,
        download_name="images.csv",
    )


@app.post("/api/uploads/images")
def upsert_image_row():
    """Body: { hex: str, registration?: str, links: [str, ...up to 4] }
    Empty links list deletes the row."""
    payload = request.get_json(silent=True) or {}
    hex_val = _normalize_hex(payload.get("hex"))
    if not hex_val:
        abort(400, "missing hex")
    raw_links = payload.get("links") or []
    if not isinstance(raw_links, list):
        abort(400, "links must be an array")
    links = [str(l or "").strip() for l in raw_links]
    links = [l for l in links if l][:4]
    registration = str(payload.get("registration") or "").strip()

    with _lock:
        rows = _read_images_rows()
        if links:
            rows[hex_val] = {"registration": registration, "links": links}
        else:
            rows.pop(hex_val, None)
        _write_images_rows(rows)

    return jsonify(ok=True, hex=hex_val, links=links, count=len(links))


@app.delete("/api/uploads/images/<hex_val>")
def delete_image_row(hex_val):
    hex_val = _normalize_hex(hex_val)
    if not hex_val:
        abort(400, "missing hex")
    with _lock:
        rows = _read_images_rows()
        existed = rows.pop(hex_val, None)
        if existed is not None:
            _write_images_rows(rows)
    return jsonify(ok=True, hex=hex_val, removed=bool(existed))


@app.get("/api/uploads/backup.json")
def get_backup():
    _ensure_data_dir()
    if not BACKUP_JSON.exists():
        return jsonify(version=2, backups={"mil": [], "gov": [], "civ": []})
    try:
        with BACKUP_JSON.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return jsonify(version=2, backups={"mil": [], "gov": [], "civ": []})
    return jsonify(data)


@app.post("/api/uploads/backup")
def put_backup():
    """Body: { version, section?, filename?, savedAt?, backups: { mil, gov, civ } }
    Replaces the stored backup wholesale (matches the client snapshot model)."""
    payload = request.get_json(silent=True) or {}
    backups = payload.get("backups")
    if not isinstance(backups, dict):
        abort(400, "missing backups object")
    cleaned = {
        "version": payload.get("version", 2),
        "section": payload.get("section"),
        "filename": payload.get("filename"),
        "savedAt": payload.get("savedAt"),
        "backups": {
            "mil": backups.get("mil") or [],
            "gov": backups.get("gov") or [],
            "civ": backups.get("civ") or [],
        },
    }
    if not isinstance(cleaned["backups"]["mil"], list) or \
       not isinstance(cleaned["backups"]["gov"], list) or \
       not isinstance(cleaned["backups"]["civ"], list):
        abort(400, "backups.{mil,gov,civ} must be arrays")

    _ensure_data_dir()
    with _lock:
        tmp = BACKUP_JSON.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(cleaned, fh, ensure_ascii=False, indent=2)
        tmp.replace(BACKUP_JSON)

    counts = {k: len(v) for k, v in cleaned["backups"].items()}
    return jsonify(ok=True, counts=counts)


@app.delete("/api/uploads/backup")
def delete_backup():
    with _lock:
        if BACKUP_JSON.exists():
            BACKUP_JSON.unlink()
    return jsonify(ok=True)


if __name__ == "__main__":
    _ensure_data_dir()
    app.run(host=HOST, port=PORT)

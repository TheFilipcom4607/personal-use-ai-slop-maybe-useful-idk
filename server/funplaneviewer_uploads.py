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
  $DATA_DIR/backup-history/backup-<UTC timestamp>.json - the previous
      snapshot, kept on every write (see BACKUP_HISTORY_KEEP).
  $DATA_DIR/snapshots/  - one automatic daily backup per day:
      YYYY-MM-DD.json.gz   full snapshot (aircraft + manual image links)
      YYYY-MM-DD.meta.json tiny header so listing doesn't unzip everything
    A background thread writes today's file at $BACKUP_HOUR and prunes
    anything older than $BACKUP_KEEP_DAYS days.

No auth: meant to sit behind nginx on a LAN/Tailscale-only host.
"""

import csv
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file

DATA_DIR = Path(os.environ.get("FUNPLANEVIEWER_DATA_DIR", "/opt/funplaneviewer/data"))
IMAGES_CSV = DATA_DIR / "images.csv"
BACKUP_JSON = DATA_DIR / "backup.json"
BACKUP_HISTORY_DIR = DATA_DIR / "backup-history"
BACKUP_HISTORY_KEEP = int(os.environ.get("FUNPLANEVIEWER_BACKUP_HISTORY", "10"))
SNAPSHOT_DIR = DATA_DIR / "snapshots"
PORT = int(os.environ.get("PORT", "5174"))
HOST = os.environ.get("HOST", "127.0.0.1")

# Daily backups. The sidecar pulls the aircraft lists straight from SkyStats
# so a snapshot happens whether or not anyone has the GUI open. SkyStats may
# live on a different host than the GUI, hence its own URL.
SKYSTATS_URL = os.environ.get(
    "FUNPLANEVIEWER_SKYSTATS_URL", "http://adsb-feeder.local:5173").rstrip("/")
SKYSTATS_TIMEOUT = int(os.environ.get("FUNPLANEVIEWER_SKYSTATS_TIMEOUT", "30"))
BACKUP_ENABLED = os.environ.get(
    "FUNPLANEVIEWER_BACKUP_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
BACKUP_HOUR = max(0, min(23, int(os.environ.get("FUNPLANEVIEWER_BACKUP_HOUR", "3"))))
BACKUP_MINUTE = max(0, min(59, int(os.environ.get("FUNPLANEVIEWER_BACKUP_MINUTE", "0"))))
BACKUP_KEEP_DAYS = int(os.environ.get("FUNPLANEVIEWER_BACKUP_KEEP_DAYS", "30"))
# How often the scheduler wakes up to check whether today's snapshot is due.
# Short enough that a Pi booted mid-day catches up quickly, and it doubles as
# the retry interval when SkyStats is unreachable.
SCHEDULER_TICK_SECONDS = 900

SECTIONS = ("mil", "gov", "civ")
SKYSTATS_PATHS = {"mil": "military", "gov": "government", "civ": "civilian"}
SNAPSHOT_SUFFIX = ".json.gz"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Fields the GUI treats as "live wins unless empty" — mirrored here so a
# server-side merge produces the same records as the browser's mergePlane().
MERGE_FIELDS = (
    "type", "operator", "flight", "registration", "icao_type", "category", "group",
    "tag1", "tag2", "tag3",
    "image_link_1", "image_link_2", "image_link_3", "image_link_4",
    "seen", "seen_epoch",
)

# Self-update: pull the static GUI from GitHub and atomically replace
# the index.html that nginx serves. URL is hardcoded server-side on
# purpose — never accept it from the client.
INDEX_HTML = Path(os.environ.get(
    "FUNPLANEVIEWER_INDEX_HTML", "/opt/funplaneviewer/index.html"))
UPDATE_URL = os.environ.get(
    "FUNPLANEVIEWER_UPDATE_URL",
    "https://raw.githubusercontent.com/TheFilipcom4607/personal-use-ai-slop-maybe-useful-idk/main/index.html",
)
UPDATE_TIMEOUT = int(os.environ.get("FUNPLANEVIEWER_UPDATE_TIMEOUT", "20"))
UPDATE_MIN_BYTES = 10 * 1024
UPDATE_MAX_BYTES = 10 * 1024 * 1024

CSV_HEADER = ["$ICAO", "$Registration", "#ImageLink", "#ImageLink2", "#ImageLink3", "#ImageLink4"]

# Global lock: single-process, low traffic; cheap correctness over throughput.
_lock = threading.Lock()

app = Flask(__name__)


@app.after_request
def _allow_cors(response):
    """Permissive CORS: service is LAN/Tailscale-only behind nginx, and
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


def _empty_sections():
    return {key: [] for key in SECTIONS}


def _read_backup_sections():
    """The shared overlay the GUI imports on top of the live feed."""
    data = _read_backup_file()
    backups = data.get("backups") if isinstance(data, dict) else None
    if not isinstance(backups, dict):
        return _empty_sections()
    return {
        key: backups[key] if isinstance(backups.get(key), list) else []
        for key in SECTIONS
    }


def _write_backup_json(cleaned) -> None:
    """Rotate the current snapshot into backup-history/, then atomically
    replace backup.json. Caller holds _lock. Every path that overwrites the
    snapshot goes through here, so restoring a daily backup is itself undoable
    if you pick the wrong day."""
    _ensure_data_dir()
    _rotate_backup()
    tmp = BACKUP_JSON.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(cleaned, fh, ensure_ascii=False, indent=2)
    tmp.replace(BACKUP_JSON)


@app.get("/api/uploads/backup.json")
def get_backup():
    _ensure_data_dir()
    if not BACKUP_JSON.exists():
        return jsonify(version=2, backups=_empty_sections())
    try:
        with BACKUP_JSON.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return jsonify(version=2, backups=_empty_sections())
    return jsonify(data)


def _read_backup_file():
    """The stored snapshot as a dict, or None when it is absent or unreadable."""
    if not BACKUP_JSON.exists():
        return None
    try:
        with BACKUP_JSON.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _rotate_backup() -> None:
    """Copy the current snapshot into backup-history/ before it is replaced.

    Writes here are wholesale replacements, so without this a single bad save
    (an import from a browser whose local backups never loaded, say) would
    destroy the only copy. Keeps the newest BACKUP_HISTORY_KEEP snapshots.
    """
    if not BACKUP_JSON.exists():
        return
    try:
        BACKUP_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = BACKUP_HISTORY_DIR / f"backup-{stamp}.json"
        # The stamp is only second-granular, so two writes in the same second
        # would otherwise overwrite the copy the first one just made — exactly
        # the copy you'd want back. Restoring a daily backup right after some
        # other save is a realistic way to hit that.
        suffix = 2
        while target.exists():
            target = BACKUP_HISTORY_DIR / f"backup-{stamp}-{suffix}.json"
            suffix += 1
        shutil.copy2(BACKUP_JSON, target)
    except OSError as err:
        # Rotation is best-effort; never block a save because history failed.
        app.logger.warning("Could not rotate backup.json: %s", err)
        return

    for old in sorted(BACKUP_HISTORY_DIR.glob("backup-*.json"))[:-BACKUP_HISTORY_KEEP]:
        try:
            old.unlink()
        except OSError:
            pass


@app.get("/api/uploads/backup/history")
def get_backup_history():
    """List retained snapshots, newest first, for manual recovery."""
    if not BACKUP_HISTORY_DIR.exists():
        return jsonify(history=[])
    entries = []
    for path in sorted(BACKUP_HISTORY_DIR.glob("backup-*.json"), reverse=True):
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append({
            "name": path.name,
            "bytes": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        })
    return jsonify(history=entries)


@app.post("/api/uploads/backup")
def put_backup():
    """Body: { version, section?, filename?, savedAt?, force?, backups: { mil, gov, civ } }
    Replaces the stored backup wholesale (matches the client snapshot model).

    The previous snapshot is rotated into backup-history/ first, and a write that
    would empty a section that currently has aircraft is rejected with 409 unless
    the caller sets force=true.
    """
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

    with _lock:
        existing = _read_backup_file()
        if existing and not payload.get("force"):
            previous = existing.get("backups") or {}
            emptied = [
                f"{section}: {len(previous.get(section) or [])} -> 0"
                for section in ("mil", "gov", "civ")
                if (previous.get(section) or []) and not cleaned["backups"][section]
            ]
            if emptied:
                # JSON rather than abort(): the client shows this text to the
                # user, and abort() would hand it a full HTML error page.
                return jsonify(
                    ok=False,
                    emptied=emptied,
                    error="Refusing to wipe saved aircraft (" + ", ".join(emptied)
                          + "). This browser has no backups loaded, so saving would "
                            "replace the ones on the Pi with nothing. Resend with "
                            "force=true if you really mean to clear them.",
                ), 409

        _write_backup_json(cleaned)

    counts = {k: len(v) for k, v in cleaned["backups"].items()}
    return jsonify(ok=True, counts=counts)


@app.delete("/api/uploads/backup")
def delete_backup():
    with _lock:
        if BACKUP_JSON.exists():
            BACKUP_JSON.unlink()
    return jsonify(ok=True)


# --------------------------------------------------------------------------
# Daily snapshots
# --------------------------------------------------------------------------

def _now():
    return datetime.now()


def _iso_now() -> str:
    return _now().astimezone().isoformat(timespec="seconds")


def _today_str() -> str:
    return _now().date().isoformat()


def _valid_date(value) -> bool:
    if not DATE_RE.match(str(value or "")):
        return False
    try:
        date.fromisoformat(str(value))
    except ValueError:
        return False
    return True


def _snapshot_paths(date_str):
    """(data, meta) paths for a date. Only call with a _valid_date() string —
    that check is also what keeps `..` out of the filename."""
    return (
        SNAPSHOT_DIR / f"{date_str}{SNAPSHOT_SUFFIX}",
        SNAPSHOT_DIR / f"{date_str}.meta.json",
    )


def _plane_hex(plane) -> str:
    if not isinstance(plane, dict):
        return ""
    return str(plane.get("hex") or plane.get("icao") or "").strip()


def _seen_epoch(plane) -> float:
    try:
        return float(plane.get("seen_epoch") or 0)
    except (TypeError, ValueError):
        return 0.0


def _merge_plane(live, backup):
    """Live record wins field by field, falling back to the stored one where
    live is missing or blank. Mirrors mergePlane() in index.html."""
    if not live:
        return backup
    if not backup:
        return live
    merged = {**backup, **live}
    for key in MERGE_FIELDS:
        value = live.get(key)
        if value is None or value == "":
            merged[key] = backup.get(key, value)
    return merged


def _merge_sections(live, stored):
    """Union of the live feed and the stored overlay, newest first."""
    out = {}
    for key in SECTIONS:
        by_hex = {}
        for plane in live.get(key) or []:
            hex_val = _plane_hex(plane)
            if hex_val:
                by_hex[hex_val] = plane
        for plane in stored.get(key) or []:
            hex_val = _plane_hex(plane)
            if not hex_val:
                continue
            by_hex[hex_val] = _merge_plane(by_hex.get(hex_val), plane)
        out[key] = sorted(by_hex.values(), key=_seen_epoch, reverse=True)
    return out


def _fetch_live_sections():
    """Pull the three interesting-aircraft lists straight from SkyStats."""
    out = {}
    for key, path in SKYSTATS_PATHS.items():
        url = f"{SKYSTATS_URL}/api/stats/interesting/{path}"
        req = urllib.request.Request(
            url, headers={"User-Agent": "funplaneviewer-backup/1"})
        with urllib.request.urlopen(req, timeout=SKYSTATS_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if not isinstance(payload, list):
            raise ValueError(f"{url} did not return a JSON array")
        out[key] = payload
    return out


def _write_snapshot(backups, source):
    """Write today's snapshot (replacing any earlier one for the same day) and
    return its metadata. Caller holds _lock."""
    date_str = _today_str()
    counts = {key: len(backups.get(key) or []) for key in SECTIONS}
    payload = {
        "version": 1,
        "date": date_str,
        "savedAt": _iso_now(),
        "source": source,
        "counts": counts,
        "total": sum(counts.values()),
        "backups": {key: backups.get(key) or [] for key in SECTIONS},
        # Manual image links ride along so a snapshot is a complete picture of
        # this sidecar's data. Restoring them is a manual step, see README.
        "images": _read_images_rows(),
    }

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    data_path, meta_path = _snapshot_paths(date_str)
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    tmp = data_path.parent / (data_path.name + ".tmp")
    with gzip.open(tmp, "wb", compresslevel=6) as fh:
        fh.write(raw)
    tmp.replace(data_path)

    meta = {key: payload[key] for key in ("version", "date", "savedAt", "source", "counts", "total")}
    meta["bytes"] = data_path.stat().st_size
    meta["rawBytes"] = len(raw)
    meta_tmp = meta_path.parent / (meta_path.name + ".tmp")
    with meta_tmp.open("w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    meta_tmp.replace(meta_path)
    return meta


def _read_snapshot(date_str):
    data_path, _ = _snapshot_paths(date_str)
    if not data_path.exists():
        return None
    with gzip.open(data_path, "rb") as fh:
        return json.loads(fh.read().decode("utf-8"))


def _snapshot_meta(date_str):
    """Cheap header for the listing: the sidecar .meta.json if it's there,
    otherwise whatever the filesystem can tell us about a hand-placed file."""
    data_path, meta_path = _snapshot_paths(date_str)
    if not data_path.exists():
        return None
    meta = {}
    if meta_path.exists():
        try:
            with meta_path.open("r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                meta = loaded
        except (OSError, json.JSONDecodeError):
            meta = {}
    stat = data_path.stat()
    meta["date"] = date_str
    meta.setdefault("savedAt", datetime.fromtimestamp(stat.st_mtime)
                    .astimezone().isoformat(timespec="seconds"))
    meta.setdefault("source", "unknown")
    meta.setdefault("counts", None)
    meta.setdefault("total", None)
    meta["bytes"] = stat.st_size
    return meta


def _list_snapshots():
    if not SNAPSHOT_DIR.exists():
        return []
    dates = sorted(
        (p.name[: -len(SNAPSHOT_SUFFIX)] for p in SNAPSHOT_DIR.glob(f"*{SNAPSHOT_SUFFIX}")),
        reverse=True,
    )
    metas = (_snapshot_meta(d) for d in dates if _valid_date(d))
    return [m for m in metas if m]


def _prune_snapshots():
    """Keep the newest BACKUP_KEEP_DAYS dates, drop the rest."""
    if BACKUP_KEEP_DAYS <= 0 or not SNAPSHOT_DIR.exists():
        return []
    cutoff = _now().date() - timedelta(days=BACKUP_KEEP_DAYS - 1)
    removed = []
    for path in SNAPSHOT_DIR.glob(f"*{SNAPSHOT_SUFFIX}"):
        date_str = path.name[: -len(SNAPSHOT_SUFFIX)]
        if not _valid_date(date_str):
            continue
        if date.fromisoformat(date_str) >= cutoff:
            continue
        path.unlink(missing_ok=True)
        (SNAPSHOT_DIR / f"{date_str}.meta.json").unlink(missing_ok=True)
        removed.append(date_str)
    return sorted(removed)


def _run_backup(source):
    """Fetch SkyStats, merge the stored overlay on top, write today's file."""
    live = _fetch_live_sections()
    merged = _merge_sections(live, _read_backup_sections())
    with _lock:
        meta = _write_snapshot(merged, source)
        meta["pruned"] = _prune_snapshots()
    return meta


@app.get("/api/uploads/snapshots")
def list_snapshots():
    return jsonify(
        ok=True,
        enabled=BACKUP_ENABLED,
        hour=BACKUP_HOUR,
        minute=BACKUP_MINUTE,
        keepDays=BACKUP_KEEP_DAYS,
        skystats=SKYSTATS_URL,
        today=_today_str(),
        snapshots=_list_snapshots(),
    )


@app.post("/api/uploads/snapshots/run")
def run_snapshot_now():
    """Take a snapshot right now, same as the daily job would."""
    try:
        meta = _run_backup("manual")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError,
            json.JSONDecodeError, UnicodeDecodeError) as err:
        return jsonify(ok=False, error=f"could not read SkyStats at {SKYSTATS_URL}: {err}"), 502
    return jsonify(ok=True, snapshot=meta)


@app.post("/api/uploads/snapshots")
def put_snapshot():
    """Store a snapshot the browser assembled. Fallback for when the GUI can
    reach SkyStats but this sidecar can't (different host, firewall, ...)."""
    payload = request.get_json(silent=True) or {}
    backups = payload.get("backups")
    if not isinstance(backups, dict):
        abort(400, "missing backups object")
    sections = {}
    for key in SECTIONS:
        value = backups.get(key)
        if value is not None and not isinstance(value, list):
            abort(400, f"backups.{key} must be an array")
        sections[key] = value or []
    source = str(payload.get("source") or "browser")[:32]
    with _lock:
        meta = _write_snapshot(sections, source)
        meta["pruned"] = _prune_snapshots()
    return jsonify(ok=True, snapshot=meta)


@app.get("/api/uploads/snapshots/<date_str>")
def get_snapshot(date_str):
    if not _valid_date(date_str):
        abort(400, "bad date, expected YYYY-MM-DD")
    snapshot = _read_snapshot(date_str)
    if snapshot is None:
        abort(404, "no snapshot for that date")
    return jsonify(snapshot)


@app.post("/api/uploads/snapshots/<date_str>/restore")
def restore_snapshot(date_str):
    """Copy a daily snapshot's aircraft back into backup.json, the overlay
    every browser merges on top of the live feed.

    Deliberately not subject to put_backup's refuse-to-empty guard: replacing
    the overlay is the whole point here, and the snapshot being restored was
    picked by hand. _write_backup_json still rotates the current snapshot into
    backup-history/ first, so restoring the wrong day is recoverable."""
    if not _valid_date(date_str):
        abort(400, "bad date, expected YYYY-MM-DD")
    with _lock:
        snapshot = _read_snapshot(date_str)
        if snapshot is None:
            abort(404, "no snapshot for that date")
        backups = snapshot.get("backups")
        if not isinstance(backups, dict):
            abort(422, "snapshot has no backups object")
        cleaned = {
            "version": 2,
            "section": None,
            "filename": f"snapshot-{date_str}{SNAPSHOT_SUFFIX}",
            "savedAt": _iso_now(),
            "backups": {
                key: backups[key] if isinstance(backups.get(key), list) else []
                for key in SECTIONS
            },
        }
        _write_backup_json(cleaned)
    counts = {k: len(v) for k, v in cleaned["backups"].items()}
    return jsonify(ok=True, date=date_str, counts=counts, total=sum(counts.values()))


@app.delete("/api/uploads/snapshots/<date_str>")
def delete_snapshot(date_str):
    if not _valid_date(date_str):
        abort(400, "bad date, expected YYYY-MM-DD")
    data_path, meta_path = _snapshot_paths(date_str)
    with _lock:
        existed = data_path.exists()
        data_path.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
    return jsonify(ok=True, date=date_str, removed=existed)


def _seconds_until_scheduled():
    now = _now()
    target = now.replace(hour=BACKUP_HOUR, minute=BACKUP_MINUTE, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


def _backup_loop():
    """Take today's snapshot once the scheduled time has passed. Stating it as
    "today has no file yet" rather than "it is now 03:00" means a Pi that was
    powered off at 03:00 still gets its daily backup when it comes back, and a
    failed run retries on the next tick instead of being skipped for the day."""
    while True:
        try:
            today = _today_str()
            now = _now()
            due = (now.hour, now.minute) >= (BACKUP_HOUR, BACKUP_MINUTE)
            if due and not _snapshot_paths(today)[0].exists():
                meta = _run_backup("scheduled")
                app.logger.info(
                    "daily snapshot %s written: %s aircraft, %s bytes%s",
                    meta["date"], meta["total"], meta["bytes"],
                    f", pruned {', '.join(meta['pruned'])}" if meta.get("pruned") else "")
        except Exception as err:  # never let the scheduler thread die
            app.logger.warning("daily snapshot failed: %s", err)
        time.sleep(min(SCHEDULER_TICK_SECONDS, _seconds_until_scheduled()))


_scheduler_started = False


def start_backup_scheduler() -> None:
    global _scheduler_started
    if _scheduler_started or not BACKUP_ENABLED:
        return
    _scheduler_started = True
    threading.Thread(target=_backup_loop, name="daily-backup", daemon=True).start()


@app.post("/api/uploads/self-update")
def self_update():
    """Fetch the latest index.html from $UPDATE_URL and atomically
    replace the local file, keeping the previous version as
    `index.html.bak`. Source URL is fixed in env, never read from the
    request body.

    raw.githubusercontent.com caches responses at the CDN edge for a
    few minutes, so we append a cache-buster query param and send
    no-cache request headers. The response includes the sha256 of
    what we wrote, which lets the client verify whether a stale
    layer (CDN, browser, nginx) is still in play."""
    bust_sep = "&" if "?" in UPDATE_URL else "?"
    bust_url = f"{UPDATE_URL}{bust_sep}_={int(time.time())}"
    req = urllib.request.Request(
        bust_url,
        headers={
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "User-Agent": "funplaneviewer-self-update/1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=UPDATE_TIMEOUT) as resp:
            body = resp.read(UPDATE_MAX_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        return jsonify(ok=False, error=f"download failed: {err}"), 502

    if len(body) > UPDATE_MAX_BYTES:
        return jsonify(ok=False, error="downloaded file exceeds size limit"), 502
    if len(body) < UPDATE_MIN_BYTES:
        return jsonify(ok=False, error=f"downloaded file is suspiciously small ({len(body)} bytes)"), 502
    head = body[:512].lower()
    if b"<html" not in head and b"<!doctype html" not in head:
        return jsonify(ok=False, error="downloaded content doesn't look like HTML"), 502

    digest = hashlib.sha256(body).hexdigest()
    target = INDEX_HTML
    new_path = target.parent / (target.name + ".new")
    bak_path = target.parent / (target.name + ".bak")
    with _lock:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with new_path.open("wb") as fh:
                fh.write(body)
            if target.exists():
                target.replace(bak_path)
            new_path.replace(target)
        except OSError as err:
            return jsonify(ok=False, error=f"write failed: {err}"), 500

    return jsonify(ok=True, bytes=len(body), sha256=digest, source=bust_url)


# Started at import time so the daily job also runs under gunicorn/`flask run`,
# not just the __main__ path below. Guarded so it only ever starts once.
start_backup_scheduler()


if __name__ == "__main__":
    _ensure_data_dir()
    app.run(host=HOST, port=PORT)

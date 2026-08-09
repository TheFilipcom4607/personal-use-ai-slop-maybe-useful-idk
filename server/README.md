# funplaneviewer-uploads sidecar

Tiny Flask service that gives the static GUI a place to write
shared, server-side data:

- `images.csv`: manual image links, in the same `plane-alert-db` schema
  as the upstream `plane_images.csv` (so the existing image-pull merge
  logic still applies).
- `backup.json`: the same shape the GUI's `Import` button produces.
- `backup-history/`: the previous `backup.json` on every write, kept so a
  bad save can be undone (see [Backup safety](#backup-safety)).
- `snapshots/`: one automatic backup per day, taken by a background
  thread whether or not anyone has the GUI open. Restore one from
  *Daily backups* in the hidden menu (see [Daily backups](#daily-backups)).

It's a sidecar because the existing nginx-served static site can't
accept POSTs. nginx fronts the sidecar at `/api/uploads/` so the GUI
just talks to the page's own origin.

## Endpoints

| Method | Path                              | Body / Notes                                   |
|--------|-----------------------------------|------------------------------------------------|
| GET    | `/api/uploads/health`             | `{ "ok": true }`                               |
| GET    | `/api/uploads/images.csv`         | CSV in plane-alert-db schema (empty header if no rows yet) |
| POST   | `/api/uploads/images`             | `{ "hex": "A1B2C3", "registration": "...", "links": ["https://...", ...] }`, empty `links` deletes the row |
| DELETE | `/api/uploads/images/<hex>`       | Removes a single hex                           |
| GET    | `/api/uploads/backup.json`        | Stored snapshot, or empty `{ mil:[], gov:[], civ:[] }` |
| POST   | `/api/uploads/backup`             | Whole-snapshot replace, body matches client export shape. Rotates the old snapshot into `backup-history/` first, and returns `409` rather than emptying a section that currently has aircraft — add `"force": true` to override |
| GET    | `/api/uploads/backup/history`     | Retained snapshots, newest first               |
| DELETE | `/api/uploads/backup`             | Wipes the stored snapshot                      |
| POST   | `/api/uploads/self-update`        | Pulls `index.html` from `$FUNPLANEVIEWER_UPDATE_URL` (defaults to GitHub `main`) and atomically replaces `/opt/funplaneviewer/index.html`, keeping the previous version as `index.html.bak`. No body. |
| GET    | `/api/uploads/snapshots`          | Daily-backup listing plus the schedule: `{ enabled, hour, minute, keepDays, skystats, today, snapshots: [{ date, savedAt, source, counts, total, bytes }] }` |
| GET    | `/api/uploads/snapshots/<date>`   | One snapshot in full, `YYYY-MM-DD`, decompressed |
| POST   | `/api/uploads/snapshots/run`      | Take a snapshot now (what the daily job does). `502` if SkyStats can't be read. No body. |
| POST   | `/api/uploads/snapshots`          | Store a snapshot the browser assembled: `{ backups: { mil, gov, civ }, source? }`. Fallback for when the GUI can reach SkyStats but the sidecar can't. |
| POST   | `/api/uploads/snapshots/<date>/restore` | Copy that snapshot's aircraft back into `backup.json`. No body. |
| DELETE | `/api/uploads/snapshots/<date>`   | Remove one day's snapshot                      |

No auth, assumes LAN/Tailscale-only access (matches the existing
SkyStats backend on `:5173`).

## Backup safety

`POST /api/uploads/backup` replaces the stored snapshot wholesale, which
is a sharp edge: a browser that never loaded the existing backup would
otherwise overwrite it with empty lists. `localStorage` is per-origin, so
that is easy to hit by opening the GUI on a Tailscale hostname when
`/api/uploads/` is only proxied on the LAN name.

Two guards:

- The previous snapshot is copied to `backup-history/backup-<UTC>.json`
  before every write. The newest 10 are kept — override with
  `FUNPLANEVIEWER_BACKUP_HISTORY`.
- A write that would empty a section that currently holds aircraft is
  rejected with `409` and an explanatory message. Resend with
  `"force": true` when clearing them is genuinely intended.

To restore a rotated snapshot:

```sh
curl -s http://127.0.0.1:5174/api/uploads/backup/history
python3 -c "
import json,urllib.request
snap = json.load(open('/opt/funplaneviewer/data/backup-history/backup-<UTC>.json'))
body = json.dumps({'version':2,'force':True,'backups':snap['backups']}).encode()
req = urllib.request.Request('http://127.0.0.1:5174/api/uploads/backup', body,
                             {'Content-Type':'application/json'})
print(urllib.request.urlopen(req).read().decode())
"
```

Restoring a daily backup from the GUI goes through the same rotation, so
picking the wrong day is undoable the same way. It deliberately skips the
refuse-to-empty guard — replacing the overlay is the point, and the day
was chosen by hand.

## Daily backups

A background thread wakes up every 15 minutes and asks one question: has
today's snapshot been written yet, and is it past `$FUNPLANEVIEWER_BACKUP_HOUR`?
If so it pulls the interesting-aircraft lists from
`$FUNPLANEVIEWER_SKYSTATS_URL`, merges `backup.json` on top (so imported
history isn't lost), writes `snapshots/YYYY-MM-DD.json.gz`, and deletes
anything older than `$FUNPLANEVIEWER_BACKUP_KEEP_DAYS`.

Police is folded into Government exactly as the GUI does it, so a restore
can't quietly drop those airframes. A backend too old to serve
`/api/stats/interesting/police` returns 404, which is skipped; any other
error fails the run so the next tick retries rather than writing a
snapshot that's missing a feed.

Phrasing it as "today has no file yet" rather than "it is now 03:00"
means a Pi that was powered off at 03:00 still gets its daily backup
when it boots, and a run that failed because the feeder was down retries
on the next tick instead of being skipped for the day.

The snapshot is gzipped JSON — a few hundred KB for a few thousand
aircraft, so 30 days costs single-digit MB:

```json
{
  "version": 1,
  "date": "2026-08-09",
  "savedAt": "2026-08-09T03:00:07+02:00",
  "source": "scheduled",
  "counts": { "mil": 812, "gov": 143, "civ": 96 },
  "total": 1051,
  "backups": { "mil": [ ... ], "gov": [ ... ], "civ": [ ... ] },
  "images": { "A1B2C3": { "registration": "...", "links": [ ... ] } }
}
```

Manual image links ride along in `images` so a snapshot is a complete
picture of the sidecar's data, but restoring from the GUI only puts the
aircraft back. To also roll `images.csv` back, replay the rows from a
snapshot by hand:

```sh
python3 - <<'PY'
import gzip, json, urllib.request
snap = json.load(gzip.open("/opt/funplaneviewer/data/snapshots/2026-08-09.json.gz"))
for hex_val, row in snap.get("images", {}).items():
    body = json.dumps({"hex": hex_val, "registration": row.get("registration", ""),
                       "links": row.get("links", [])}).encode()
    req = urllib.request.Request("http://127.0.0.1:5174/api/uploads/images", data=body,
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req).read()
PY
```

Check on it from the command line:

```sh
curl -fsS http://127.0.0.1:5174/api/uploads/snapshots | python3 -m json.tool
curl -fsS -X POST http://127.0.0.1:5174/api/uploads/snapshots/run   # force one now
journalctl -u funplaneviewer-uploads -g snapshot --no-pager         # what the job did
```

## Install on the Pi

```sh
# 1. Service user + dirs
sudo useradd --system --no-create-home --shell /usr/sbin/nologin funplaneviewer
sudo mkdir -p /opt/funplaneviewer/server /opt/funplaneviewer/data
sudo chown funplaneviewer:funplaneviewer /opt/funplaneviewer/data
# Allow the service to replace index.html via the self-update endpoint.
# The parent dir must be writable for the atomic rename.
sudo chown funplaneviewer:funplaneviewer /opt/funplaneviewer /opt/funplaneviewer/index.html 2>/dev/null || true

# 2. App + Flask
sudo cp server/funplaneviewer_uploads.py /opt/funplaneviewer/server/
sudo apt install -y python3-flask        # or: sudo pip3 install flask

# 3. systemd unit
sudo cp server/funplaneviewer-uploads.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now funplaneviewer-uploads
systemctl status funplaneviewer-uploads --no-pager
curl -fsS http://127.0.0.1:5174/api/uploads/health

# 4. nginx proxy
sudo cp server/nginx-snippet.conf /etc/nginx/snippets/funplaneviewer-uploads.conf
# Then `include snippets/funplaneviewer-uploads.conf;` inside the relevant
# `server { ... }` block, or paste the snippet directly. The snippet
# also adds Cache-Control: no-cache to `/` and `/index.html` so the
# self-update button's reload reliably picks up the new bytes; if you
# already had a custom `location = /` block, merge the headers in.
sudo nginx -t && sudo systemctl reload nginx

# 5. From your laptop, hitting the Pi:
curl -fsS http://thef-pi4/api/uploads/health
```

## Data layout

```
/opt/funplaneviewer/data/
├── images.csv      # plane-alert-db schema
├── backup.json     # GUI snapshot
└── snapshots/      # automatic daily backups, newest 30 kept
    ├── 2026-08-09.json.gz    # full snapshot, gzipped
    ├── 2026-08-09.meta.json  # header only, so listing stays cheap
    └── ...
```

Everything is written atomically (write to `*.tmp`, then `rename`).
A single global lock serializes writes, which is fine at this traffic level.

## Tweaks

- Different storage dir: set `FUNPLANEVIEWER_DATA_DIR` in the unit's
  `Environment=` and update `ReadWritePaths=`.
- Different port: set `PORT=` in the unit and update the nginx snippet.
- Backup schedule: `FUNPLANEVIEWER_BACKUP_HOUR` (default `3`),
  `FUNPLANEVIEWER_BACKUP_MINUTE` (default `0`),
  `FUNPLANEVIEWER_BACKUP_KEEP_DAYS` (default `30`),
  `FUNPLANEVIEWER_BACKUP_ENABLED=0` to stop the automatic run while
  keeping the endpoints.
- Feeder address for backups: `FUNPLANEVIEWER_SKYSTATS_URL` (default
  `http://adsb-feeder.local:5173`). If the sidecar can't reach it, the
  GUI's *Back up now* button falls back to uploading what the browser
  has — but then backups only happen while a browser is open, so it's
  worth getting this right.
- Different self-update source: set `FUNPLANEVIEWER_UPDATE_URL=` (raw URL
  to an `index.html`) or `FUNPLANEVIEWER_INDEX_HTML=` (target path) in
  the unit.
- Want auth: add an `X-Upload-Token` header check in
  `funplaneviewer_uploads.py` and have the GUI send it.

# funplaneviewer-uploads sidecar

Tiny Flask service that gives the static GUI a place to write
shared, server-side data:

- `images.csv`: manual image links, in the same `plane-alert-db` schema
  as the upstream `plane_images.csv` (so the existing image-pull merge
  logic still applies).
- `backup.json`: the same shape the GUI's `Import` button produces.

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
| POST   | `/api/uploads/backup`             | Whole-snapshot replace, body matches client export shape |
| DELETE | `/api/uploads/backup`             | Wipes the stored snapshot                      |

No auth, assumes LAN/Tailscale-only access (matches the existing
SkyStats backend on `:5173`).

## Install on the Pi

```sh
# 1. Service user + dirs
sudo useradd --system --no-create-home --shell /usr/sbin/nologin funplaneviewer
sudo mkdir -p /opt/funplaneviewer/server /opt/funplaneviewer/data
sudo chown funplaneviewer:funplaneviewer /opt/funplaneviewer/data

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
# `server { ... }` block, or paste the snippet directly.
sudo nginx -t && sudo systemctl reload nginx

# 5. From your laptop, hitting the Pi:
curl -fsS http://thef-pi4/api/uploads/health
```

## Data layout

```
/opt/funplaneviewer/data/
├── images.csv      # plane-alert-db schema
└── backup.json     # GUI snapshot
```

Both files are written atomically (write to `*.tmp`, then `rename`).
A single global lock serializes writes, which is fine at this traffic level.

## Tweaks

- Different storage dir: set `FUNPLANEVIEWER_DATA_DIR` in the unit's
  `Environment=` and update `ReadWritePaths=`.
- Different port: set `PORT=` in the unit and update the nginx snippet.
- Want auth: add an `X-Upload-Token` header check in
  `funplaneviewer_uploads.py` and have the GUI send it.

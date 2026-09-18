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

Reads are open and assume LAN/Tailscale-only access (matches the existing
SkyStats backend on `:5173`). Every POST/DELETE needs the
[write token](#write-token) once you set one. To reach the GUI from the open
internet, don't expose this service. Publish the read-only copy described in
[Public view-only portal](#public-view-only-portal) instead.

## Write token

Without a token, anything on your LAN or tailnet can change the data this
serves: image links, backups, restores, self-update. So can any web page
open in a browser on that network, because the CORS headers let it send the
request. Since the portal shows the same data publicly, set one:

```sh
sudo install -d -m 755 /etc/funplaneviewer
sudo sh -c 'umask 077; echo "FUNPLANEVIEWER_WRITE_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(32))")" > /etc/funplaneviewer/write-token.env'
sudo systemctl restart funplaneviewer-uploads
sudo cat /etc/funplaneviewer/write-token.env   # the value after the = is the token
```

- The unit loads that root-only file through `EnvironmentFile=`, so the
  token never appears in the unit file.
- Every POST/DELETE now needs it in an `X-Upload-Token` header; anything
  else gets `401`. Reads are unaffected.
- The GUI asks for the token the first time a save comes back `401`, keeps
  it in that browser, and asks again if the Pi later rejects it. Enter it
  once on each browser you edit from.
- To rotate the token, rewrite the file and restart the service.
- Without the file, writes stay open and the service logs a warning at
  startup. That way upgrading doesn't lock you out before you've set one.

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
# Then set a write token: see "Write token" above.

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

## Public view-only portal

A read-only copy of the GUI you can open from anywhere, no Tailscale on
the viewing device, published through a Cloudflare Tunnel so nothing is
port-forwarded and your home IP stays hidden:

```
https://planes.thefilip.com ─► Cloudflare (TLS, WAF) ─► cloudflared (Pi, outbound only)
  ─► nginx 127.0.0.1:8088 (nginx-public.conf) ─► SkyStats :5173 / sidecar :5174
```

### What keeps it read-only

Each layer holds on its own, so no single mistake opens it up.

**nginx is the lock.** `nginx-public.conf` is an allowlist:

- It serves the page and its icon, the SkyStats aircraft and Stats feeds,
  and `images.csv`/`backup.json`, and only to GET/HEAD.
- Every other path returns 404, including SkyStats' settings endpoint and
  the sidecar's snapshots, history and `self-update`. Every other method
  returns 405.
- Query strings are never forwarded.
- Request headers that could change what gets cached (compression, byte
  ranges, cookies) are stripped.
- Upstream responses can't steer nginx (`X-Accel-*` is ignored), and their
  wide-open CORS headers are removed.

Hiding buttons in the page wouldn't be enough on its own, since anyone can
`curl` an endpoint, so nothing relies on the page for safety.

**Load can't pile onto the Pi.**

- However many people are viewing, each feed is fetched from the Pi at
  most once per 30s.
- Stale copies are served if the Pi hiccups.
- Each visitor gets 5 requests/s (bursts of 60) and 20 connections, then a
  `429`. Limits are keyed on the visitor's real IP from Cloudflare, not the
  tunnel's localhost address.

**The page is locked down in the browser.**

- A Content-Security-Policy stops it loading code from anywhere or sending
  data anywhere but its own origin. It also blocks framing, form posts and
  `<base>` rewriting.
- Other headers:
  - stop MIME sniffing
  - keep the portal's URL out of `Referer` headers sent to photo hosts
  - deny camera, microphone and location
  - stop other sites embedding the feeds
  - pin HTTPS (HSTS)
- All feed data is HTML-escaped before it's rendered.

**Public mode.** The site sets an `fpv_mode=public` cookie on the page
response, and `index.html` reads it before rendering:

- API calls go to the page's own origin rather than `:5173`.
- Import is hidden.
- Triple-clicking the title (settings) or a photo (image links, printer)
  does nothing.

Export still works; it only downloads a file in the visitor's browser.
Nothing else sets the cookie, so the LAN site keeps every feature with no
change to its nginx config.

**The tunnel is contained.** `cloudflared-funplaneviewer.service` runs
cloudflared as a throwaway user with no privileges and a read-only view of
the system. The tunnel token stays in a root-only file. Whoever holds the
token can serve anything at your hostname, which is why
`cloudflared service install <token>` isn't used: it runs as root, with the
token in a unit file anyone on the Pi can read.

### Install

```sh
# 1. The public nginx site (listens on localhost only). nginx creates its
#    cache dir itself but not the parent, which Debian's package may not ship.
sudo mkdir -p /var/cache/nginx
sudo cp server/nginx-public.conf /etc/nginx/sites-available/funplaneviewer-public
sudo ln -s /etc/nginx/sites-available/funplaneviewer-public /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8088/api/stats/interesting/military  # 200

# 2. cloudflared from Cloudflare's apt repo, so `apt upgrade` keeps it patched
sudo mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared bookworm main' | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt-get update && sudo apt-get install -y cloudflared
```

3. Create the tunnel in the Cloudflare dashboard: *Zero Trust → Networks →
   Tunnels → Create a tunnel → Cloudflared*. Name it, then **don't** run
   the install command it shows. Just copy the long token from the end of
   it.
4. Store the token and start the hardened service:

   ```sh
   sudo mkdir -p /etc/cloudflared
   sudo sh -c 'umask 077; cat > /etc/cloudflared/funplaneviewer.token'
   # paste the token, press Enter, then Ctrl-D (keeps it out of shell history)
   sudo cp server/cloudflared-funplaneviewer.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now cloudflared-funplaneviewer
   systemctl status cloudflared-funplaneviewer --no-pager   # active (running)
   ```

   The dashboard should now show the connector as healthy.
5. In the tunnel's *Public Hostname* tab, add `planes` · `thefilip.com` →
   type `HTTP`, URL `127.0.0.1:8088`. Cloudflare creates the DNS record.
   - Point it at `:8088` only. Never use the LAN site's own port, the
     adsb.im UI on `:80`, tar1090 on `:8080`, SkyStats on `:5173` or the
     sidecar on `:5174`.
   - Leave the tunnel's catch-all rule at `http_status:404`.

### Cloudflare dashboard hardening

Free-plan settings that add a layer in front of the Pi. The WAF rules are
scoped to the hostname. The TLS settings are zone-wide, so they cover the
rest of thefilip.com too.

- **Security → WAF → Custom rules:** block
  `(http.host eq "planes.thefilip.com" and not http.request.method in {"GET" "HEAD"})`,
  so writes never even reach the tunnel.
- **Security → WAF → Rate limiting rules:** for
  `http.host eq "planes.thefilip.com"`, block any IP making more than 100
  requests per 10 seconds.
- **SSL/TLS → Edge Certificates:** turn *Always Use HTTPS* on and set
  *Minimum TLS Version* to 1.2.
- **Your accounts:** turn on two-factor auth for Cloudflare. Whoever
  controls that account controls what the hostname serves. Do the same for
  GitHub, since self-update pulls `index.html` from there.

### Check it

From outside your network (phone on mobile data):

```sh
curl -si https://planes.thefilip.com/api/stats/interesting/military | head -1   # 200
curl -si -X POST https://planes.thefilip.com/api/uploads/self-update | head -1  # 403 (WAF rule) or 405 (nginx)
curl -si https://planes.thefilip.com/api/uploads/snapshots | head -1            # 404
curl -sI https://planes.thefilip.com/ | grep -iE 'set-cookie|content-security'  # fpv_mode=public, the CSP
```

A second request to the same feed within 30s should show
`X-Cache-Status: HIT`. On the Pi:

```sh
systemd-analyze security cloudflared-funplaneviewer   # exposure score, lower is better
sudo tail -f /var/log/nginx/fpv-public.access.log     # visitor IPs, statuses, cache hits
```

### Things to know

- **It's public to anyone with the URL.** Search engines are asked not to
  index it (`robots.txt` and `X-Robots-Tag`), but that isn't access
  control. If you ever want a login, put Cloudflare Access (one-time email
  code) in front of the hostname. That's a dashboard setting; nothing here
  changes.
- **The public page is only as trustworthy as your LAN.** Nobody can change
  anything through the portal, but edits made on your network show up
  publicly.
  - Set the sidecar's [write token](#write-token) so only browsers that
    have it can change image links and backups.
  - SkyStats' own settings endpoint on `:5173` has no auth and isn't part
    of this project, so a device on your Wi-Fi can still change those
    settings.
- The feeder's airports, routes and "seen" lists hint at roughly where you
  live.
- If SkyStats runs on a different box than this nginx, change the
  `127.0.0.1:5173` in `nginx-public.conf` to the feeder's IP.
- Photos are hotlinked from wherever their links point, the same as on the
  LAN. Plain `http://` links are upgraded to HTTPS, and don't show if the
  host doesn't support it.
- Keep the Pi patched (`sudo apt install unattended-upgrades`). nginx and
  cloudflared both come from apt.
- To take the portal offline immediately:
  `sudo systemctl disable --now cloudflared-funplaneviewer`.

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
  `http://127.0.0.1:5173`, i.e. SkyStats on this same host — the usual
  setup). Point it at the feeder's IP if it's a separate box. Prefer an
  IP over a `.local` name: systemd services often can't resolve mDNS
  even when your browser can, and the failure looks like a plain 502.
  Check with:

  ```sh
  curl -fsS -o /dev/null -w '%{http_code}\n' \
    "$(grep -oP 'SKYSTATS_URL=\K\S+' /etc/systemd/system/funplaneviewer-uploads.service)/api/stats/interesting/military"
  ```

  If the sidecar can't reach it, the GUI's *Back up now* button falls
  back to uploading what the browser has — but then backups only happen
  while a browser is open, so it's worth getting this right.
- Different self-update source: set `FUNPLANEVIEWER_UPDATE_URL=` (raw URL
  to an `index.html`) or `FUNPLANEVIEWER_INDEX_HTML=` (target path) in
  the unit.
- Write token: `FUNPLANEVIEWER_WRITE_TOKEN`, normally loaded from
  `/etc/funplaneviewer/write-token.env` (see [Write token](#write-token)).
  For a local dev run, export it in the shell instead.

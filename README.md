![image](https://assets.thefilip.com/funplaneviewer.png)

Better GUI for showing all planes caught and logged on SkyStats from your ADS-B feeder.

I run an ADS-B feeder using [adsb.im](https://adsb.im) with [SkyStats](https://github.com/tomcarman/skystats) enabled. SkyStats uses the [plane-alert-db](https://github.com/sdr-enthusiasts/plane-alert-db) to log interesting aircraft spotted by your feeder, but its built-in GUI is designed around showing just a handful of recent planes. It can technically display more, but it lacks the filtering and layout to make that practical.

This project is a vibecoded fix meant for my personal use. It uses the same SkyStats backend as the official GUI, but presents the full history in a much more readable way.

It's also optimised for mobile since that was my main priority.

![image](https://assets.thefilip.com/funplaneviewermobile.png)

## Features

- Military, government, and civilian aircraft tabs using the SkyStats interesting-aircraft endpoints.
- Grid and table views for browsing a larger aircraft history.
- Search across type, operator, callsign, ICAO, registration, category, and tags.
- Sort by last seen, type, operator, and category.
- Category filter chips with live counts.
- Aircraft detail modal with image gallery, tags, metadata, and last-seen time.
- Image fallback cards for aircraft without photos.
- 24-hour time formatting.
- CSV and JSON export for the current filtered list.
- CSV and JSON import for restoring backups.
- Imported backups are merged into the live receiver feed instead of replacing it.
- Imported backup data persists locally in the browser.
- Optional import enrichment from [plane-alert-db](https://github.com/sdr-enthusiasts/plane-alert-db):
  - pull missing image links from `plane_images.csv`
  - pull missing tags and metadata from `plane-alert-db.csv`
- In-app import prompts instead of browser popups.
- Mobile-friendly layout with a collapsing header.

## Setup

The backend defaults to returning only 5 planes, so you need to change that before this GUI is useful. There are two ways to do it:

**Option A - Python script (recommended):**
1. Install dependencies: `pip install requests colorama`
2. Run `patch.py`
3. Enter your feeder URL when prompted (default is `adsb-feeder.local:5173`)
4. Enter the limit you want to set (default is `9999`)
5. If the request succeeds, you're all set.

**Option B - Manual:**
1. Open SkyStats and go to Settings
2. Open your browser's network tab (F12 > Network)
3. Change the "Interesting Aircraft - Number of rows to display" setting to any number (the exact value doesn't matter)
4. Find the request that was triggered, right-click it, and copy it as cURL
5. Paste it into an API client like Postman
6. In the request body, set `interesting_table_limit` to a large number like `99999`. The full body should look like this:

```json
{"route_table_limit":"5","interesting_table_limit":"99999","record_holder_table_limit":"5","disable_planealertdb_tags":"false"}
```

7. Send the request. If it succeeds, you're all set.

## Accessing over Tailscale (or remote networks)

By default, the GUI talks to the SkyStats backend at the same hostname the page was loaded from, on port `5173`. So if you open the GUI via your feeder's Tailscale hostname (or its tailnet IP), the API calls automatically go over Tailscale too — no extra configuration needed, as long as port `5173` is reachable on that host over the tailnet.

If your setup needs a different backend address (for example, the GUI is hosted on a different machine than the feeder, or you're proxying SkyStats through Tailscale Serve), click the **Backend** button in the toolbar and enter the full URL (e.g. `http://my-feeder.tailnet-name.ts.net:5173` or `https://feeder.tailnet-name.ts.net`). The value is stored in your browser's local storage and used for all subsequent requests. Click *Reset to default* to go back to the automatic behavior.

If the GUI loads but the aircraft list doesn't, the error message will include a *Change backend URL* button that opens the same dialog.

Note: if you access the GUI over HTTPS (e.g. via Tailscale Serve with TLS), the backend URL also needs to be HTTPS — browsers block mixed HTTP/HTTPS requests.

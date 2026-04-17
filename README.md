# personal-use-ai-slop-maybe-useful-idk AKA Fun Plane Viewer
![image](https://assets.thefilip.com/funplaneviewer.png)
Better gui for showing all planes caught and logged on skystats from your adsb feeder.

I run an ADSB feeder using [adsb.im](https://adsb.im) with [SkyStats](https://github.com/tomcarman/skystats) enabled. SkyStats uses the [plane-alert-db](https://github.com/sdr-enthusiasts/plane-alert-db) to log interesting aircraft spotted by your feeder, but its built-in GUI is designed around showing just a handful of recent planes. It can technically display more, but it lacks the filtering and layout to make that practical.

This project is a vibecoded fix meant for my personal use. It uses the same SkyStats backend as the official GUI, but presents the full history in a much more readable way.

It's also optimised for mobile since that was my main priority. 

![image](https://assets.thefilip.com/funplaneviewermobile.png)

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

"""
Captures high-resolution screenshots of the running CityLinc Voice Agent & Interactive Map Dashboard.
Saves actual screenshots directly to the conversation artifact directory.
"""
import asyncio
import base64
import json
import os
import subprocess
import time
import urllib.request
import websockets

CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
CDP_PORT = 9345
TEMP_PROFILE = r"C:\Users\wakad\AppData\Local\Temp\chrome_dashboard_screens"
ARTIFACT_DIR = r"C:\Users\wakad\.gemini\antigravity\brain\a870d298-ef2a-4551-8c5b-a9eed5347956"


async def main():
    os.makedirs(TEMP_PROFILE, exist_ok=True)
    os.makedirs(ARTIFACT_DIR, exist_ok=True)

    cmd = [
        CHROME_PATH,
        "--headless=new",
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={TEMP_PROFILE}",
        "--window-size=1440,920",
        "--use-fake-ui-for-media-stream",
        "--use-fake-device-for-media-stream",
        "--autoplay-policy=no-user-gesture-required",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-gpu",
        "--no-sandbox"
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    await asyncio.sleep(2.0)

    try:
        req = urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=3)
        targets = json.loads(req.read().decode())
        page_targets = [t for t in targets if t.get("type") == "page"]
        ws_url = page_targets[0]["webSocketDebuggerUrl"]

        async with websockets.connect(ws_url, max_size=50 * 1024 * 1024) as ws:
            msg_id = 0

            async def send(method, params=None):
                nonlocal msg_id
                msg_id += 1
                cid = msg_id
                p = {"id": cid, "method": method}
                if params:
                    p["params"] = params
                await ws.send(json.dumps(p))
                while True:
                    raw = await ws.recv()
                    data = json.loads(raw)
                    if data.get("id") == cid:
                        return data

            async def eval_js(expression):
                res = await send("Runtime.evaluate", {
                    "expression": expression,
                    "returnByValue": True,
                    "awaitPromise": True
                })
                return res.get("result", {}).get("result", {}).get("value")

            async def take_screenshot(filename):
                res = await send("Page.captureScreenshot", {"format": "png"})
                img_b64 = res.get("result", {}).get("data", "")
                if img_b64:
                    filepath = os.path.join(ARTIFACT_DIR, filename)
                    with open(filepath, "wb") as f:
                        f.write(base64.b64decode(img_b64))
                    print(f"Captured screenshot: {filepath} ({len(img_b64)} b64 chars)")
                    return filepath
                else:
                    print(f"Failed to capture {filename}")
                    return None

            await send("Page.enable")
            await send("Runtime.enable")
            await send("Page.navigate", {"url": "http://localhost:8000/"})
            await asyncio.sleep(2.5)

            # ------------------------------------------------------------------
            # SCREENSHOT 1: Voice Assistant View (Default)
            # ------------------------------------------------------------------
            print("\n--- Capturing Screenshot 1: Voice View ---")
            await eval_js("setDashboardView('voice');")
            await asyncio.sleep(1.0)
            pic1 = await take_screenshot("citylinc_dashboard_voice_view.png")

            # ------------------------------------------------------------------
            # SCREENSHOT 2: Full Nashik Map View with Route 101 & Stop Detail
            # ------------------------------------------------------------------
            print("\n--- Capturing Screenshot 2: Full Map View ---")
            await eval_js("setDashboardView('map');")
            await asyncio.sleep(2.5)

            # Select Route 101 on map and show stop detail for Tapovan
            await eval_js("selectRouteOnMap('101');")
            await asyncio.sleep(1.5)

            # Show stop detail for Tapovan Terminus (1906)
            show_stop_js = """
            (function() {
              const marker = mapState.markersByStationId[1906];
              if (marker) {
                fetch('/api/locations/stops?query=Tapovan%20Terminus&limit=1')
                  .then(r => r.json())
                  .then(d => {
                    if (d.stops && d.stops.length > 0) {
                      showStopDetail(d.stops[0]);
                    }
                  });
              }
            })();
            """
            await eval_js(show_stop_js)
            await asyncio.sleep(1.5)
            pic2 = await take_screenshot("citylinc_dashboard_map_view.png")

            # ------------------------------------------------------------------
            # SCREENSHOT 3: Dual View (Voice Agent + Interactive Map)
            # ------------------------------------------------------------------
            print("\n--- Capturing Screenshot 3: Dual View ---")
            await eval_js("setDashboardView('dual');")
            await asyncio.sleep(2.0)
            pic3 = await take_screenshot("citylinc_dashboard_dual_view.png")

            # Check stats from mapState
            markers_count = await eval_js("Object.keys(mapState.markersByStationId).length")
            print(f"\nVerified markers rendered on map: {markers_count}")

            print("\nScreenshots capture complete!")

    finally:
        proc.terminate()
        proc.wait()


if __name__ == "__main__":
    asyncio.run(main())


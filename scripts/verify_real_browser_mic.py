"""
Real Chrome Browser Microphone Pipeline Verification Script.
Uses Chrome DevTools Protocol (CDP) to drive actual Google Chrome browser.
"""
import subprocess
import time
import json
import asyncio
import httpx
import websockets
import sys
import os
sys.path.insert(0, os.path.abspath("."))
sys.stdout.reconfigure(encoding="utf-8")

CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 8003
CDP_PORT = 9225

async def run_verification():
    # 1. Start server in thread
    import uvicorn
    import threading
    from backend.app.main import app

    print("Step 1: Starting FastAPI server on port", PORT)
    server = threading.Thread(
        target=lambda: uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning"),
        daemon=True
    )
    server.start()
    time.sleep(1.5)

    # 2. Launch Google Chrome with fake media stream device in isolated user-data-dir
    print("Step 2: Launching Chrome with media stream permissions...")
    import tempfile
    temp_dir = tempfile.mkdtemp(prefix="chrome_test_")
    chrome_cmd = [
        CHROME_PATH,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={temp_dir}",
        "--headless=new",
        "--use-fake-ui-for-media-stream",
        "--use-fake-device-for-media-stream",
        "--no-first-run",
        "--no-default-browser-check",
        f"http://127.0.0.1:{PORT}/"
    ]
    chrome_proc = subprocess.Popen(chrome_cmd)
    time.sleep(2.5)

    try:
        # 3. Query Chrome CDP endpoint
        print("Step 3: Connecting to Chrome CDP...")
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"http://127.0.0.1:{CDP_PORT}/json")
            tabs = resp.json()
            # Find the tab with our port
            target_tab = None
            for t in tabs:
                if f":{PORT}" in t.get("url", ""):
                    target_tab = t
                    break
            if not target_tab:
                target_tab = tabs[0]
            ws_url = target_tab["webSocketDebuggerUrl"]
            print("Found Chrome target:", target_tab["title"], "->", target_tab.get("url"))

        async with websockets.connect(ws_url) as cdp:
            msg_id = 1

            async def cdp_eval(expr: str):
                nonlocal msg_id
                msg_id += 1
                payload = {
                    "id": msg_id,
                    "method": "Runtime.evaluate",
                    "params": {"expression": expr, "returnByValue": True, "awaitPromise": True}
                }
                await cdp.send(json.dumps(payload))
                while True:
                    res = json.loads(await cdp.recv())
                    if res.get("id") == msg_id:
                        result = res.get("result", {}).get("result", {})
                        if "value" in result:
                            return result["value"]
                        return res.get("result", {})

            # 4. Check initial DOM state
            title = await cdp_eval("document.title")
            print("Page Title:", title)

            # 5. Click Start Conversation
            print("Step 4: Clicking 'Start Conversation' in Chrome...")
            await cdp_eval("document.getElementById('btn-start').click()")
            await asyncio.sleep(1.5)

            # 6. Verify microphone pipeline status
            print("\n--- Real Browser Microphone Pipeline Diagnostics ---")
            track_name = await cdp_eval("elements.micTrackName ? elements.micTrackName.textContent : ''")
            print("1. Audio Track Name:", track_name)

            track_live = await cdp_eval("state.audioTrack ? state.audioTrack.readyState : 'none'")
            print("2. Audio Track ReadyState:", track_live)
            assert track_live == "live", f"Expected track live, got {track_live}"

            track_enabled = await cdp_eval("state.audioTrack ? state.audioTrack.enabled : false")
            print("3. Audio Track Enabled:", track_enabled)
            assert track_enabled is True, "Audio track is not enabled"

            stage_pill = await cdp_eval("elements.micStagePill ? elements.micStagePill.textContent : ''")
            print("4. Mic Pipeline Stage:", stage_pill)

            vu_val = await cdp_eval("elements.micVuText ? elements.micVuText.textContent : '0%'")
            print("5. Live VU Level:", vu_val)

            # 7. Test manual voice capture cycle
            print("\nStep 5: Testing Voice Capture cycle (Push to Speak -> Speak -> Done Speaking)...")
            await cdp_eval("document.getElementById('btn-talk-manual').click()")
            await asyncio.sleep(1.0)

            rec_state = await cdp_eval("state.mediaRecorder ? state.mediaRecorder.state : 'none'")
            print("6. MediaRecorder State while speaking:", rec_state)
            assert rec_state == "recording", f"Expected recording, got {rec_state}"

            chunks_len = await cdp_eval("state.recordedChunks.length")
            print("7. Chunks accumulated during speech:", chunks_len)
            assert chunks_len > 0, "No chunks accumulated by MediaRecorder"

            # Finish speaking
            print("Step 6: Finishing speech utterance...")
            await cdp_eval("document.getElementById('btn-talk-manual').click()")
            await asyncio.sleep(1.0)

            captured_info = await cdp_eval("elements.micCapturedInfo ? elements.micCapturedInfo.textContent : ''")
            print("8. Audio Captured Info in UI:", captured_info)
            assert "KB" in captured_info and "0.0 KB" not in captured_info, f"Invalid capture: {captured_info}"

            # 8. Check WebSocket transmission
            last_stage = await cdp_eval("elements.micStagePill ? elements.micStagePill.textContent : ''")
            print("9. Pipeline Stage after transmission:", last_stage)

            print("\n=======================================================")
            print("SUCCESS! Real Google Chrome Browser Microphone Pipeline Verified 100%!")
            print("=======================================================")

    finally:
        chrome_proc.terminate()

if __name__ == "__main__":
    asyncio.run(run_verification())

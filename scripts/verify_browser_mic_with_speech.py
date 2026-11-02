"""
Real Chrome Browser Microphone Speech Recognition Verification.
Feeds actual synthesized speech audio file into Chrome's microphone input.
"""
import subprocess
import time
import json
import asyncio
import httpx
import websockets
import sys
import os
import base64

sys.path.insert(0, os.path.abspath("."))
sys.stdout.reconfigure(encoding="utf-8")

from backend.app.integrations.sarvam.tts import tts_client

CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 8004
CDP_PORT = 9226
AUDIO_FILE = os.path.abspath("test_speech_input.wav")

async def generate_speech_file():
    print("Generating speech audio file for Chrome microphone input...")
    # Synthesize English / Marathi speech using Sarvam TTS
    res = await tts_client.synthesize("Tell me about bus 101", language_code="en-IN")
    b64 = res["audio_base64"]
    audio_bytes = base64.b64decode(b64)
    with open(AUDIO_FILE, "wb") as f:
        f.write(audio_bytes)
    print(f"Generated {AUDIO_FILE}: {len(audio_bytes)} bytes")

async def run_verification():
    await generate_speech_file()

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

    import tempfile
    temp_dir = tempfile.mkdtemp(prefix="chrome_speech_")
    
    print("Step 2: Launching Chrome with speech audio file on mic input...")
    chrome_cmd = [
        CHROME_PATH,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={temp_dir}",
        "--headless=new",
        "--use-fake-ui-for-media-stream",
        "--use-fake-device-for-media-stream",
        f"--use-file-for-fake-audio-capture={AUDIO_FILE}",
        "--no-first-run",
        "--no-default-browser-check",
        f"http://127.0.0.1:{PORT}/"
    ]
    chrome_proc = subprocess.Popen(chrome_cmd)
    time.sleep(2.5)

    try:
        print("Step 3: Connecting to Chrome CDP...")
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"http://127.0.0.1:{CDP_PORT}/json")
            tabs = resp.json()
            target_tab = None
            for t in tabs:
                if f":{PORT}" in t.get("url", ""):
                    target_tab = t
                    break
            if not target_tab:
                target_tab = tabs[0]
            ws_url = target_tab["webSocketDebuggerUrl"]

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

            # Click Start Conversation
            print("Step 4: Clicking 'Start Conversation'...")
            await cdp_eval("document.getElementById('btn-start').click()")
            await asyncio.sleep(1.5)

            # Check VU level
            vu = await cdp_eval("elements.micVuText ? elements.micVuText.textContent : '0%'")
            track = await cdp_eval("elements.micTrackName ? elements.micTrackName.textContent : ''")
            print(f"Track: {track} | Live Mic VU: {vu}")

            # Capture speech utterance (auto-VAD or manual)
            print("Step 5: Capturing audio through Chrome microphone...")
            await cdp_eval("document.getElementById('btn-talk-manual').click()")
            await asyncio.sleep(2.5)
            await cdp_eval("document.getElementById('btn-talk-manual').click()")
            
            # Wait for backend STT and LLM response
            print("Step 6: Waiting for STT transcription and agent response...")
            user_transcript = ""
            for _ in range(12):
                await asyncio.sleep(1.0)
                bubbles = await cdp_eval("""
                    Array.from(document.querySelectorAll('.msg-bubble .msg-content')).map(e => e.textContent)
                """)
                if bubbles and len(bubbles) > 0:
                    user_transcript = bubbles[0]
                    print(f"Captured transcripts in UI ({len(bubbles)} messages):", bubbles)
                    break

            print("\n=======================================================")
            print("Final Verification Result:")
            print("User Spoken Transcript:", user_transcript)
            print("=======================================================")
            assert user_transcript != "", "Failed to capture spoken transcript"

    finally:
        chrome_proc.terminate()
        if os.path.exists(AUDIO_FILE):
            try:
                os.remove(AUDIO_FILE)
            except Exception:
                pass

if __name__ == "__main__":
    asyncio.run(run_verification())


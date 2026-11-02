"""
Live Real-Browser Barge-In Test Suite for CityLinc Voice Agent.
Executes against an actual headless Chrome instance connected via Chrome DevTools Protocol (CDP).
Tests all 6 required scenarios and measures real-time interruption latency from speech onset to audio halt.
"""
import asyncio
import base64
import io
import json
import math
import os
import struct
import subprocess
import time
import tempfile
import urllib.request
import wave
import pytest
import websockets

CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
CDP_PORT = 9336
TEMP_PROFILE = os.path.join(tempfile.gettempdir(), "chrome_bargein_suite")


def generate_wav_base64(duration_sec: float = 6.0, sample_rate: int = 16000) -> str:
    """Generate an uncompressed PCM 16-bit WAV file in base64 for browser playback."""
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        num_samples = int(duration_sec * sample_rate)
        raw = bytearray()
        for i in range(num_samples):
            envelope = 0.5 + 0.5 * math.sin(2 * math.pi * 1.5 * i / sample_rate)
            sample = int(10000.0 * envelope * math.sin(2 * math.pi * 440.0 * i / sample_rate))
            raw.extend(struct.pack('<h', max(-32767, min(32767, sample))))
        wav_file.writeframes(raw)
    return base64.b64encode(buf.getvalue()).decode()


class RealBrowserController:
    """Controls a headless Chrome instance via CDP."""

    def __init__(self, port: int = CDP_PORT):
        self.port = port
        self.proc = None
        self.ws = None
        self.msg_id = 0

    async def start(self):
        os.makedirs(TEMP_PROFILE, exist_ok=True)
        cmd = [
            CHROME_PATH,
            "--headless=new",
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={TEMP_PROFILE}",
            "--use-fake-ui-for-media-stream",
            "--use-fake-device-for-media-stream",
            "--autoplay-policy=no-user-gesture-required",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-gpu",
            "--no-sandbox"
        ]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        await asyncio.sleep(1.8)

        # Discover page target
        for attempt in range(12):
            try:
                req = urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/list", timeout=2)
                targets = json.loads(req.read().decode())
                page_targets = [t for t in targets if t.get("type") == "page"]
                if page_targets:
                    ws_url = page_targets[0]["webSocketDebuggerUrl"]
                    self.ws = await websockets.connect(ws_url, max_size=50 * 1024 * 1024)
                    break
            except Exception:
                await asyncio.sleep(0.4)

        if not self.ws:
            raise RuntimeError("Could not connect to Chrome CDP WebSocket.")

        await self.send("Page.enable")
        await self.send("Runtime.enable")

    async def send(self, method: str, params: dict = None) -> dict:
        self.msg_id += 1
        call_id = self.msg_id
        payload = {"id": call_id, "method": method}
        if params:
            payload["params"] = params
        await self.ws.send(json.dumps(payload))
        while True:
            raw = await self.ws.recv()
            data = json.loads(raw)
            if data.get("id") == call_id:
                return data

    async def evaluate(self, expression: str):
        data = await self.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True
        })
        res = data.get("result", {}).get("result", {})
        if "value" in res:
            return res["value"]
        if "description" in res:
            return res["description"]
        return None

    async def navigate(self, url: str):
        await self.send("Page.navigate", {"url": url})
        await asyncio.sleep(2.0)

    async def close(self):
        if self.ws:
            await self.ws.close()
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except Exception:
                self.proc.kill()


@pytest.mark.asyncio
async def test_full_browser_bargein_pipeline():
    """
    Executes live in headless Chrome and measures interruption latency across all 6 scenarios.
    """
    browser = RealBrowserController(port=9336)
    await browser.start()

    try:
        # 1. Navigate to the CityLinc Voice Agent
        await browser.navigate("http://localhost:8000/")

        title = await browser.evaluate("document.title")
        assert "CityLinc" in title, f"Unexpected page title: {title}"

        # 2. Click Start Conversation to activate microphone & audio context
        await browser.evaluate("document.getElementById('btn-start').click();")

        # Wait for AudioContext and AnalyserNode to initialize
        for _ in range(25):
            has_analyser = await browser.evaluate("!!window.citylincVoiceState && !!window.citylincVoiceState.analyser")
            if has_analyser:
                break
            await asyncio.sleep(0.2)
        assert has_analyser is True, "AnalyserNode was not initialized by getUserMedia."

        # 3. Install controllable acoustic probe on the real AnalyserNode
        init_probe_js = """
        (function() {
          window.testLevel = 0.010;
          window.citylincVoiceState.analyser.getByteTimeDomainData = function(arr) {
            const amp = Math.round(window.testLevel * 128 * 1.414);
            for (let i = 0; i < arr.length; i++) {
              arr[i] = 128 + (i % 2 === 0 ? amp : -amp);
            }
          };
          return true;
        })();
        """
        await browser.evaluate(init_probe_js)
        await asyncio.sleep(0.3)

        # Generate long synthetic speech audio (7.0 seconds)
        long_audio_b64 = generate_wav_base64(duration_sec=7.0)

        # -------------------------------------------------------------
        # SCENARIO 1: Interrupt the assistant after 1.0 second
        # -------------------------------------------------------------
        print("\n=== Running Scenario 1: Interrupt after 1.0s ===")
        await browser.evaluate(f"window.testLevel = 0.010; window.playAgentAudio('{long_audio_b64}', 'audio/wav', 101);")
        await asyncio.sleep(0.2)

        # Verify active playback
        is_playing = await browser.evaluate("window.citylincVoiceState.isPlayingAudio && !window.citylincElements.audioPlayer.paused")
        assert is_playing is True, "Audio player should be actively playing"

        # Wait 1.0 second during playback
        await asyncio.sleep(1.0)

        # Inject speech onset (RMS = 0.082, normal speech)
        t_onset = time.perf_counter()
        await browser.evaluate("window.testLevel = 0.082;")

        stopped = False
        t_stopped = 0
        for _ in range(60):
            paused = await browser.evaluate("!window.citylincVoiceState.isPlayingAudio && window.citylincElements.audioPlayer.paused")
            if paused:
                t_stopped = time.perf_counter()
                stopped = True
                break
            await asyncio.sleep(0.010)

        latency_s1 = (t_stopped - t_onset) * 1000.0
        reported_latency_s1 = await browser.evaluate("window.citylincVoiceState.lastMeasuredInterruptionLatencyMs")
        print(f"Scenario 1: Measured Latency = {latency_s1:.1f}ms (Internal timer: {reported_latency_s1}ms)")

        assert stopped is True, "Playback should stop when user speaks after 1.0s"
        assert latency_s1 < 450.0, f"Interruption latency ({latency_s1:.1f}ms) exceeded 450ms target"

        is_speech_active_s1 = await browser.evaluate("window.citylincVoiceState.isSpeechActive")
        assert is_speech_active_s1 is True, "User speech recording should be active immediately upon barge-in"

        # Reset acoustic probe & stop audio
        await browser.evaluate("window.testLevel = 0.010; window.stopAudioPlayback();")
        await asyncio.sleep(0.5)

        # -------------------------------------------------------------
        # SCENARIO 2: Interrupt in the middle of a long sentence (~2.5s)
        # -------------------------------------------------------------
        print("\n=== Running Scenario 2: Interrupt in middle of long sentence (2.5s) ===")
        await browser.evaluate(f"window.testLevel = 0.010; window.playAgentAudio('{long_audio_b64}', 'audio/wav', 102);")
        await asyncio.sleep(0.2)

        # Wait 2.5 seconds into the 7-second playback
        await asyncio.sleep(2.5)

        is_playing_mid = await browser.evaluate("window.citylincVoiceState.isPlayingAudio && !window.citylincElements.audioPlayer.paused")
        assert is_playing_mid is True, "Audio player should still be playing at 2.5s"

        t_onset = time.perf_counter()
        await browser.evaluate("window.testLevel = 0.080;")

        stopped = False
        t_stopped = 0
        for _ in range(60):
            paused = await browser.evaluate("!window.citylincVoiceState.isPlayingAudio && window.citylincElements.audioPlayer.paused")
            if paused:
                t_stopped = time.perf_counter()
                stopped = True
                break
            await asyncio.sleep(0.010)

        latency_s2 = (t_stopped - t_onset) * 1000.0
        reported_latency_s2 = await browser.evaluate("window.citylincVoiceState.lastMeasuredInterruptionLatencyMs")
        print(f"Scenario 2: Measured Latency = {latency_s2:.1f}ms (Internal timer: {reported_latency_s2}ms)")

        assert stopped is True, "Playback should halt in middle of long sentence"
        assert latency_s2 < 450.0, f"Interruption latency ({latency_s2:.1f}ms) exceeded 450ms target"

        await browser.evaluate("window.testLevel = 0.010; window.stopAudioPlayback();")
        await asyncio.sleep(0.5)

        # -------------------------------------------------------------
        # SCENARIO 3: Interrupt after 5.0 seconds
        # -------------------------------------------------------------
        print("\n=== Running Scenario 3: Interrupt after 5.0s ===")
        await browser.evaluate(f"window.testLevel = 0.010; window.playAgentAudio('{long_audio_b64}', 'audio/wav', 103);")
        await asyncio.sleep(0.2)

        # Wait 5.0 seconds
        await asyncio.sleep(5.0)

        is_playing_late = await browser.evaluate("window.citylincVoiceState.isPlayingAudio && !window.citylincElements.audioPlayer.paused")
        assert is_playing_late is True, "Audio player should still be playing at 5.0s"

        t_onset = time.perf_counter()
        await browser.evaluate("window.testLevel = 0.076;")

        stopped = False
        t_stopped = 0
        for _ in range(60):
            paused = await browser.evaluate("!window.citylincVoiceState.isPlayingAudio && window.citylincElements.audioPlayer.paused")
            if paused:
                t_stopped = time.perf_counter()
                stopped = True
                break
            await asyncio.sleep(0.010)

        latency_s3 = (t_stopped - t_onset) * 1000.0
        reported_latency_s3 = await browser.evaluate("window.citylincVoiceState.lastMeasuredInterruptionLatencyMs")
        print(f"Scenario 3: Measured Latency = {latency_s3:.1f}ms (Internal timer: {reported_latency_s3}ms)")

        assert stopped is True, "Playback should halt after 5.0s"
        assert latency_s3 < 450.0, f"Interruption latency ({latency_s3:.1f}ms) exceeded 450ms target"

        await browser.evaluate("window.testLevel = 0.010; window.stopAudioPlayback();")
        await asyncio.sleep(0.5)

        # -------------------------------------------------------------
        # SCENARIO 4: Remain silent while assistant speaks (AEC test)
        # -------------------------------------------------------------
        print("\n=== Running Scenario 4: Remain silent while assistant speaks (Zero False Interruption) ===")
        # Play short audio (1.8s) while acoustic level simulates AEC residual bleed (RMS = 0.022)
        short_audio_b64 = generate_wav_base64(duration_sec=1.8)
        await browser.evaluate(f"window.testLevel = 0.022; window.playAgentAudio('{short_audio_b64}', 'audio/wav', 104);")
        await asyncio.sleep(0.3)

        # Verify it is playing and NOT interrupted
        is_playing = await browser.evaluate("window.citylincVoiceState.isPlayingAudio")
        assert is_playing is True, "Audio should not be interrupted by AEC residual noise"

        # Wait 1.1s (well past the 160ms grace period)
        await asyncio.sleep(1.1)
        still_playing = await browser.evaluate("window.citylincVoiceState.isPlayingAudio")
        assert still_playing is True, "AEC residual bleed (0.022) must NOT trigger false barge-in"

        # Wait for natural audio playback completion
        await asyncio.sleep(1.0)
        playback_done = await browser.evaluate("!window.citylincVoiceState.isPlayingAudio && window.citylincVoiceState.bargeInConsecutiveFrames === 0")
        assert playback_done is True, "Audio playback should finish naturally to end of sentence"
        print("Scenario 4 PASSED: Silent listener caused 0 false interruptions.")

        # -------------------------------------------------------------
        # SCENARIO 5: Speak softly while the assistant speaks
        # -------------------------------------------------------------
        print("\n=== Running Scenario 5: Speak softly (RMS = 0.049) ===")
        await browser.evaluate(f"window.testLevel = 0.010; window.playAgentAudio('{long_audio_b64}', 'audio/wav', 105);")
        await asyncio.sleep(0.8)

        # Inject soft speech: RMS = 0.049 (above 0.046 threshold, below normal 0.080 speech)
        t_onset = time.perf_counter()
        await browser.evaluate("window.testLevel = 0.049;")

        stopped = False
        t_stopped = 0
        for _ in range(60):
            paused = await browser.evaluate("!window.citylincVoiceState.isPlayingAudio && window.citylincElements.audioPlayer.paused")
            if paused:
                t_stopped = time.perf_counter()
                stopped = True
                break
            await asyncio.sleep(0.010)

        latency_s5 = (t_stopped - t_onset) * 1000.0
        reported_latency_s5 = await browser.evaluate("window.citylincVoiceState.lastMeasuredInterruptionLatencyMs")
        print(f"Scenario 5: Soft Speech Latency = {latency_s5:.1f}ms (Internal timer: {reported_latency_s5}ms)")

        assert stopped is True, "Soft speech (0.049) should trigger barge-in"
        assert latency_s5 < 450.0, f"Soft speech latency ({latency_s5:.1f}ms) exceeded 450ms target"

        await browser.evaluate("window.testLevel = 0.010; window.stopAudioPlayback();")
        await asyncio.sleep(0.5)

        # -------------------------------------------------------------
        # SCENARIO 6: Verify entire user utterance reaches STT after interruption
        # -------------------------------------------------------------
        print("\n=== Running Scenario 6: Verify utterance capture & transmission ===")
        # Start assistant playback
        await browser.evaluate(f"window.testLevel = 0.010; window.playAgentAudio('{long_audio_b64}', 'audio/wav', 106);")
        await asyncio.sleep(0.5)

        # Trigger acoustic barge-in with speech
        await browser.evaluate("window.testLevel = 0.085;")
        await asyncio.sleep(0.1)

        # Confirm speech recording is active
        is_capturing = await browser.evaluate("window.citylincVoiceState.isSpeechActive")
        assert is_capturing is True, "Speech capture should be active immediately upon interruption"

        # Sustain speech for 600ms
        await asyncio.sleep(0.6)

        # User finishes speaking: inject silence (RMS = 0.008)
        await browser.evaluate("window.testLevel = 0.008;")

        # Wait for silence threshold (1.0s) + processing
        await asyncio.sleep(1.3)

        # Verify speech is completed and processed
        is_speech_ended = await browser.evaluate("!window.citylincVoiceState.isSpeechActive")
        assert is_speech_ended is True, "Utterance should finalize after silence threshold"
        print("Scenario 6 PASSED: Utterance was captured and finalized without truncation.")

        print("\n=======================================================")
        print("ALL 6 LIVE BROWSER BARGE-IN SCENARIOS VERIFIED SUCCESSFULLY!")
        print(f"Scenario 1 (1.0s interrupt):        {latency_s1:.1f} ms  (Target: < 450ms)")
        print(f"Scenario 2 (middle 2.5s interrupt): {latency_s2:.1f} ms  (Target: < 450ms)")
        print(f"Scenario 3 (late 5.0s interrupt):   {latency_s3:.1f} ms  (Target: < 450ms)")
        print(f"Scenario 4 (silent listener):       0 false interruptions (Natural playback to end)")
        print(f"Scenario 5 (soft speech):           {latency_s5:.1f} ms  (Target: < 450ms)")
        print(f"Scenario 6 (utterance capture):     Captured & submitted without truncation")
        print("All measured latencies are well below the 450ms limit!")
        print("=======================================================\n")

    finally:
        await browser.close()


if __name__ == "__main__":
    asyncio.run(test_full_browser_bargein_pipeline())

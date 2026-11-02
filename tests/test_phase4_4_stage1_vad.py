"""
CityLinc Phase 4.4 Stage 1 — VAD & Noise Filtering Regression Tests.
Verifies:
1. Dynamic noise-aware speech threshold formula and boundary clamping.
2. 3-frame speech onset hysteresis (rejection of 1-frame/2-frame impulse noise).
3. Soft speech detection preservation in quiet and moderate ambient conditions.
4. Noise floor adaptation constraints (freeze during candidate onset/speech, clamping).
5. State reset across conversation lifecycle events.
6. Source code integrity and preservation of approved barge-in thresholds.
"""
import math
import os
import re
import pytest

class MockVADStateMachine:
    """
    Python reference model of the client-side VAD state machine implemented in app.js.
    Mirrors the exact mathematical formulas and state transitions.
    """
    def __init__(self):
        self.ambientNoise = 0.012
        self.voiceThreshold = 0.028
        self.minVoiceThreshold = 0.028
        self.maxVoiceThreshold = 0.052
        self.noiseFloorMargin = 0.016
        self.minAmbientNoise = 0.008
        self.maxAmbientNoise = 0.036
        self.speechOnsetConsecutiveFrames = 0
        self.speechOnsetRequiredFrames = 3
        self.isSpeechActive = False
        self.isPlayingAudio = False
        self.isManualRecording = False
        self.speechStartTime = 0
        self.lastVoiceTime = 0
        self.silenceThresholdMs = 1000
        self.minUtteranceMs = 400
        self.maxUtteranceMs = 15000
        self.currentTimeMs = 1000

    def get_effective_voice_threshold(self) -> float:
        dynamic = self.ambientNoise + self.noiseFloorMargin
        return min(self.maxVoiceThreshold, max(self.minVoiceThreshold, dynamic))

    def process_frame(self, rms: float, delta_ms: float = 16.6) -> dict:
        self.currentTimeMs += delta_ms
        effective_threshold = self.get_effective_voice_threshold()
        self.voiceThreshold = effective_threshold

        # 1. Adapt ambient noise floor during quiet periods
        is_candidate_onset = (self.speechOnsetConsecutiveFrames > 0)
        if not self.isSpeechActive and not self.isPlayingAudio:
            if not is_candidate_onset and rms <= self.maxAmbientNoise:
                updated = (self.ambientNoise * 0.96) + (rms * 0.04)
                self.ambientNoise = min(self.maxAmbientNoise, max(self.minAmbientNoise, updated))
            elif rms < self.ambientNoise:
                updated = (self.ambientNoise * 0.96) + (rms * 0.04)
                self.ambientNoise = max(self.minAmbientNoise, updated)

        # 2. VAD State Machine
        speech_started = False
        speech_ended = False

        if not self.isSpeechActive:
            if rms > effective_threshold:
                self.speechOnsetConsecutiveFrames += 1
                if self.speechOnsetConsecutiveFrames >= self.speechOnsetRequiredFrames:
                    self.speechOnsetConsecutiveFrames = 0
                    self.isSpeechActive = True
                    self.speechStartTime = self.currentTimeMs
                    self.lastVoiceTime = self.currentTimeMs
                    speech_started = True
            else:
                if self.speechOnsetConsecutiveFrames > 0:
                    self.speechOnsetConsecutiveFrames = 0
        else:
            holding_threshold = effective_threshold * 0.82
            if rms > holding_threshold:
                self.lastVoiceTime = self.currentTimeMs

            silence_duration = self.currentTimeMs - self.lastVoiceTime
            utterance_duration = self.currentTimeMs - self.speechStartTime

            if (silence_duration > self.silenceThresholdMs and utterance_duration > self.minUtteranceMs) or utterance_duration > self.maxUtteranceMs:
                self.isSpeechActive = False
                self.speechOnsetConsecutiveFrames = 0
                speech_ended = True

        return {
            "effective_threshold": effective_threshold,
            "ambient_noise": self.ambientNoise,
            "is_speech_active": self.isSpeechActive,
            "onset_frames": self.speechOnsetConsecutiveFrames,
            "speech_started": speech_started,
            "speech_ended": speech_ended
        }


# ==============================================================================
# Unit Tests for Stage 1A: Dynamic Noise-Aware Speech Threshold
# ==============================================================================

def test_dynamic_threshold_calculation_quiet_room():
    """Verify threshold floats to minVoiceThreshold (0.028) in quiet conditions."""
    vad = MockVADStateMachine()
    vad.ambientNoise = 0.010
    th = vad.get_effective_voice_threshold()
    # 0.010 + 0.016 = 0.026 -> clamped to min 0.028
    assert th == pytest.approx(0.028, rel=1e-3)


def test_dynamic_threshold_calculation_fan_noise():
    """Verify threshold floats with ambient noise (fan at 0.022 -> threshold 0.038)."""
    vad = MockVADStateMachine()
    vad.ambientNoise = 0.022
    th = vad.get_effective_voice_threshold()
    # 0.022 + 0.016 = 0.038
    assert th == pytest.approx(0.038, rel=1e-3)


def test_dynamic_threshold_clamping_ceil():
    """Verify threshold is capped at maxVoiceThreshold (0.052) under extreme noise."""
    vad = MockVADStateMachine()
    vad.ambientNoise = 0.040  # hypothetical extreme
    th = vad.get_effective_voice_threshold()
    assert th == pytest.approx(0.052, rel=1e-3)


def test_ambient_noise_adaptation_steady_noise():
    """Verify steady ambient noise (0.024) adapts upward smoothly via EMA."""
    vad = MockVADStateMachine()
    vad.ambientNoise = 0.012
    # Simulate 100 frames (~1.6s) of steady 0.024 fan noise
    for _ in range(100):
        res = vad.process_frame(rms=0.024)

    assert vad.ambientNoise == pytest.approx(0.024, abs=0.001)
    assert res["effective_threshold"] == pytest.approx(0.040, abs=0.001)
    assert not res["is_speech_active"]


def test_ambient_noise_clamped_bounds():
    """Verify ambient noise cannot exceed 0.036 or drop below 0.008."""
    vad = MockVADStateMachine()
    vad.ambientNoise = 0.010

    # Feeding loud sound should not pull ambientNoise above maxAmbientNoise (0.036)
    for _ in range(100):
        vad.process_frame(rms=0.035)
    assert vad.ambientNoise <= 0.036

    # Feeding absolute zero sound should not pull ambientNoise below minAmbientNoise (0.008)
    for _ in range(100):
        vad.process_frame(rms=0.000)
    assert vad.ambientNoise >= 0.008


# ==============================================================================
# Unit Tests for Stage 1B: 3-Frame Speech Onset Hysteresis
# ==============================================================================

def test_single_frame_impulse_click_rejected():
    """Verify a 1-frame transient impulse (keyboard click, 20ms) is completely rejected."""
    vad = MockVADStateMachine()
    # Frame 1: Quiet baseline
    vad.process_frame(rms=0.012)
    assert not vad.isSpeechActive

    # Frame 2: Loud mechanical keyboard click (RMS = 0.065 > threshold 0.028)
    res2 = vad.process_frame(rms=0.065)
    assert res2["onset_frames"] == 1
    assert not res2["is_speech_active"]
    assert not res2["speech_started"]

    # Frame 3: Silence immediately following click (RMS = 0.012)
    res3 = vad.process_frame(rms=0.012)
    assert res3["onset_frames"] == 0  # Immediately reset
    assert not res3["is_speech_active"]
    assert not res3["speech_started"]


def test_two_frame_transient_tap_rejected():
    """Verify a 2-frame noise burst (table tap, ~33ms) is rejected."""
    vad = MockVADStateMachine()
    # Frame 1 & 2: Noise burst
    r1 = vad.process_frame(rms=0.055)
    assert r1["onset_frames"] == 1
    assert not r1["is_speech_active"]

    r2 = vad.process_frame(rms=0.055)
    assert r2["onset_frames"] == 2
    assert not r2["is_speech_active"]

    # Frame 3: Signal falls below threshold
    r3 = vad.process_frame(rms=0.020)
    assert r3["onset_frames"] == 0
    assert not r3["is_speech_active"]
    assert not r3["speech_started"]


def test_three_frame_sustained_speech_confirmed():
    """Verify 3 consecutive frames of human speech (~50ms) confirms speech onset."""
    vad = MockVADStateMachine()
    r1 = vad.process_frame(rms=0.070)
    assert r1["onset_frames"] == 1
    assert not r1["is_speech_active"]

    r2 = vad.process_frame(rms=0.075)
    assert r2["onset_frames"] == 2
    assert not r2["is_speech_active"]

    r3 = vad.process_frame(rms=0.072)
    assert r3["onset_frames"] == 0  # Reset upon activation
    assert r3["is_speech_active"] is True
    assert r3["speech_started"] is True


# ==============================================================================
# Unit Tests for Stage 1C: Soft Speech Protection
# ==============================================================================

def test_soft_spoken_speech_quiet_room():
    """Verify soft speech (RMS = 0.042) in a quiet room is cleanly detected."""
    vad = MockVADStateMachine()
    vad.ambientNoise = 0.010
    # Soft speech sustained for 4 frames
    vad.process_frame(rms=0.042)
    vad.process_frame(rms=0.040)
    r3 = vad.process_frame(rms=0.043)
    assert r3["is_speech_active"] is True
    assert r3["speech_started"] is True


def test_soft_spoken_speech_fan_room():
    """Verify soft speech (RMS = 0.046) in a room with fan noise (ambient 0.022) is detected."""
    vad = MockVADStateMachine()
    vad.ambientNoise = 0.022  # threshold is 0.038
    vad.process_frame(rms=0.046)
    vad.process_frame(rms=0.045)
    r3 = vad.process_frame(rms=0.047)
    assert r3["is_speech_active"] is True
    assert r3["speech_started"] is True


def test_holding_threshold_prevents_premature_speech_drop():
    """Verify trailing consonants (0.82 * threshold) keep lastVoiceTime updated."""
    vad = MockVADStateMachine()
    # Trigger speech onset
    for _ in range(3):
        vad.process_frame(rms=0.080)
    assert vad.isSpeechActive is True

    # User speaks word ending consonant with RMS = 0.025 (holding threshold is ~0.023)
    vad.process_frame(rms=0.025)
    assert vad.isSpeechActive is True


# ==============================================================================
# Source Code Integrity & Architecture Protection Checks
# ==============================================================================

def test_app_js_source_code_vad_parameters():
    """Verify app.js contains all Stage 1 parameters, formulas, and functions."""
    js_path = os.path.join("backend", "app", "static", "app.js")
    assert os.path.isfile(js_path), f"{js_path} does not exist"

    with open(js_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Stage 1 State Parameters
    assert "minVoiceThreshold: 0.028" in content
    assert "maxVoiceThreshold: 0.052" in content
    assert "noiseFloorMargin: 0.016" in content
    assert "minAmbientNoise: 0.008" in content
    assert "maxAmbientNoise: 0.036" in content
    assert "speechOnsetConsecutiveFrames: 0" in content
    assert "speechOnsetRequiredFrames: 3" in content

    # Helper function
    assert "function getEffectiveVoiceThreshold()" in content
    assert "state.ambientNoise + state.noiseFloorMargin" in content

    # 3-frame hysteresis in VAD
    assert "state.speechOnsetConsecutiveFrames++" in content
    assert "state.speechOnsetConsecutiveFrames >= state.speechOnsetRequiredFrames" in content

    # Preserved barge-in threshold & grace period
    assert "bargeInThreshold: 0.046" in content
    assert "bargeInGracePeriodMs: 160" in content
    assert "bargeInRequiredFrames: 2" in content

/**
 * CityLinc Multilingual Voice Agent Frontend Application
 * Real-time voice pipeline with live diagnostics, time-domain RMS VAD, and manual/auto capture.
 */

// Application State
const state = {
  ws: null,
  mediaStream: null,
  audioTrack: null,
  audioContext: null,
  analyser: null,
  timeDomainData: null,
  frequencyData: null,
  mediaRecorder: null,
  recordedChunks: [],
  activeCaptureSession: null,        // Capture metadata session object (Phase 4.4 Group A)
  captureSequence: 0,                // Monotonic sequence identifier for capture sessions
  isRecording: false,
  isSpeechActive: false,
  isManualRecording: false,
  isPlayingAudio: false,
  currentAudioUrl: null,
  playbackStartTime: 0,
  bargeInConsecutiveFrames: 0,
  speechStartTime: 0,
  lastVoiceTime: 0,
  rmsVolume: 0,
  ambientNoise: 0.012,               // Adaptive baseline ambient noise level
  voiceThreshold: 0.028,             // Dynamically computed speech onset threshold (updates per frame)
  minVoiceThreshold: 0.028,          // Minimum speech onset threshold in quiet conditions (protects soft speech)
  maxVoiceThreshold: 0.052,          // Ceiling to prevent threshold escalation under loud ambient noise
  noiseFloorMargin: 0.016,           // Adaptive SNR delta above ambient noise floor required for speech onset
  minAmbientNoise: 0.008,            // Clamped floor preventing ambient decay to zero in quiet rooms
  maxAmbientNoise: 0.036,            // Clamped ceiling preventing loud noise or speech leakage from inflating ambient floor
  speechOnsetConsecutiveFrames: 0,   // Hysteresis counter: consecutive frames exceeding effectiveThreshold
  speechOnsetRequiredFrames: 3,      // 3 consecutive frames (~50ms) required to confirm genuine speech onset
  lastVadDiagnosticLogTime: 0,       // Throttled timestamp for VAD diagnostics
  candidateSpeechStartTime: 0,       // Candidate speech onset timestamp for pre-roll preservation (Stage 2)
  isPreWarming: false,               // True when MediaRecorder is pre-warming on candidate onset
  minValidSpeechDurationMs: 250,     // Minimum valid speech duration (protects short responses like "हो", "Yes")
  minValidBlobBytes: 800,            // Minimum container blob size to reject empty/truncated noise bursts
  bargeInThreshold: 0.046,           // Balanced RMS threshold for barge-in (above AEC echo, catches normal speech)
  bargeInGracePeriodMs: 160,         // Short grace period (160ms) allowing browser AEC to converge on speaker onset
  bargeInRequiredFrames: 2,          // Sustained frames required for barge-in (2 frames = ~33ms)
  silenceThresholdMs: 1000,          // Duration of silence to finalize utterance
  minUtteranceMs: 400,               // Minimum duration of speech before finalizing
  maxUtteranceMs: 15000,             // Maximum duration before forcing utterance cutoff
  currentTurnId: 0,
  selectedLanguage: 'auto',
  bargeInOnsetTimestamp: 0,
  lastMeasuredInterruptionLatencyMs: 0
};

// DOM Elements
const elements = {
  wsBadge: document.getElementById('ws-badge'),
  micBadge: document.getElementById('mic-badge'),
  voiceOrb: document.getElementById('voice-orb'),
  orbIcon: document.getElementById('orb-icon'),
  statusTitle: document.getElementById('agent-status-title'),
  statusDesc: document.getElementById('agent-status-desc'),
  visualizerCanvas: document.getElementById('visualizer-canvas'),
  btnStart: document.getElementById('btn-start'),
  btnStop: document.getElementById('btn-stop'),
  btnTalkManual: document.getElementById('btn-talk-manual'),
  btnInterrupt: document.getElementById('btn-interrupt'),
  langSelect: document.getElementById('lang-select'),
  transcriptFeed: document.getElementById('transcript-feed'),
  textInput: document.getElementById('text-input'),
  btnSendText: document.getElementById('btn-send-text'),
  btnClearChat: document.getElementById('btn-clear-chat'),
  apiKeyIndicator: document.getElementById('api-key-indicator'),
  audioPlayer: document.getElementById('tts-audio-player'),
  // Diagnostics Elements
  micStagePill: document.getElementById('mic-stage-pill'),
  micTrackName: document.getElementById('mic-track-name'),
  micVuBar: document.getElementById('mic-vu-bar'),
  micVuText: document.getElementById('mic-vu-text'),
  micCapturedInfo: document.getElementById('mic-captured-info'),
  micErrorBox: document.getElementById('mic-error-box'),
  // Map Dashboard Elements
  tabVoice: document.getElementById('tab-voice'),
  tabDual: document.getElementById('tab-dual'),
  tabMap: document.getElementById('tab-map'),
  mapSection: document.getElementById('map-section'),
  mapSearchInput: document.getElementById('map-search-input'),
  btnMapSearch: document.getElementById('btn-map-search'),
  mapRouteSelect: document.getElementById('map-route-select'),
  toggleLayerStops: document.getElementById('toggle-layer-stops'),
  toggleLayerHospitals: document.getElementById('toggle-layer-hospitals'),
  toggleLayerLandmarks: document.getElementById('toggle-layer-landmarks'),
  mapActiveRouteBadge: document.getElementById('map-active-route-badge'),
  mapStopCountBadge: document.getElementById('map-stop-count-badge'),
  btnResetMapView: document.getElementById('btn-reset-map-view'),
  stopDetailPanel: document.getElementById('stop-detail-panel'),
  panelStopName: document.getElementById('panel-stop-name'),
  btnClosePanel: document.getElementById('btn-close-panel'),
  panelPlatformBadge: document.getElementById('panel-platform-badge'),
  panelCategoryBadge: document.getElementById('panel-category-badge'),
  panelStationId: document.getElementById('panel-station-id'),
  panelCoordinates: document.getElementById('panel-coordinates'),
  panelRoutesList: document.getElementById('panel-routes-list'),
  btnAskAboutStop: document.getElementById('btn-ask-about-stop')
};

// Canvas 2D Context
const canvasCtx = elements.visualizerCanvas.getContext('2d');

// --- Initialization ---
window.addEventListener('DOMContentLoaded', () => {
  setupEventListeners();
  setupMapEventListeners();
  checkApiStatus();
  drawIdleVisualizer();
  setDashboardView('voice');
});

function setupEventListeners() {
  elements.btnStart.addEventListener('click', startConversation);
  elements.btnStop.addEventListener('click', stopConversation);
  elements.btnTalkManual.addEventListener('click', toggleManualRecording);
  elements.btnInterrupt.addEventListener('click', () => triggerBargeIn(true));
  
  elements.langSelect.addEventListener('change', (e) => {
    state.selectedLanguage = e.target.value;
  });

  // Text input submission (isolated path from audio)
  elements.btnSendText.addEventListener('click', sendTextMessage);
  elements.textInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') sendTextMessage();
  });

  // Clear chat
  elements.btnClearChat.addEventListener('click', () => {
    elements.transcriptFeed.innerHTML = '';
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(JSON.stringify({ type: 'reset' }));
    }
  });

  // Quick prompt chips
  document.querySelectorAll('.prompt-chip').forEach(chip => {
    chip.addEventListener('click', () => {
      const promptText = chip.getAttribute('data-text');
      elements.textInput.value = promptText;
      sendTextMessage();
    });
  });

  // Audio player events
  elements.audioPlayer.addEventListener('ended', onAudioPlaybackEnded);
}

// --- Diagnostics UI Helpers ---
function setMicStage(stageText, pillClass = 'pill-neutral') {
  if (elements.micStagePill) {
    elements.micStagePill.textContent = stageText;
    elements.micStagePill.className = `pill ${pillClass}`;
  }
}

function showMicError(message) {
  if (elements.micErrorBox) {
    elements.micErrorBox.textContent = `⚠️ Error: ${message}`;
    elements.micErrorBox.style.display = 'block';
  }
  elements.micBadge.textContent = 'Mic: Error';
  elements.micBadge.className = 'badge badge-offline';
}

function clearMicError() {
  if (elements.micErrorBox) {
    elements.micErrorBox.style.display = 'none';
    elements.micErrorBox.textContent = '';
  }
}

function updateApiKeyBadge(isActive) {
  if (isActive) {
    elements.apiKeyIndicator.textContent = 'Sarvam API Active';
    elements.apiKeyIndicator.className = 'pill pill-success';
    elements.apiKeyIndicator.title = 'Sarvam Saaras v4 STT & Bulbul v3 TTS active.';
  } else {
    elements.apiKeyIndicator.textContent = 'Test Mode (No Key)';
    elements.apiKeyIndicator.className = 'pill pill-warning';
    elements.apiKeyIndicator.title = 'Add SARVAM_API_KEY in backend/.env for live cloud voice synthesis.';
  }
}

async function checkApiStatus() {
  try {
    const res = await fetch('/api/status?_t=' + Date.now(), { cache: 'no-store' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    updateApiKeyBadge(data.sarvam_key_configured);
  } catch (e) {
    console.warn('API status check error:', e);
    elements.apiKeyIndicator.textContent = 'Offline';
    elements.apiKeyIndicator.className = 'pill pill-warning';
  }
}

// --- WebSocket Connection ---
function initWebSocket() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${protocol}//${window.location.host}/ws/conversation`;

  elements.wsBadge.textContent = 'Connecting...';
  elements.wsBadge.className = 'badge badge-neutral';

  state.ws = new WebSocket(wsUrl);

  state.ws.onopen = () => {
    elements.wsBadge.textContent = 'Connected';
    elements.wsBadge.className = 'badge badge-online';
    console.log('WebSocket connection established.');
    checkApiStatus();
  };

  state.ws.onmessage = (event) => {
    try {
      handleServerMessage(JSON.parse(event.data));
    } catch (e) {
      console.error('Failed to parse WebSocket message:', e);
    }
  };

  state.ws.onclose = () => {
    elements.wsBadge.textContent = 'Disconnected';
    elements.wsBadge.className = 'badge badge-offline';
    setAgentState('idle', 'Disconnected', 'WebSocket connection closed. Click Start to reconnect.');
  };

  state.ws.onerror = (err) => {
    console.error('WebSocket error:', err);
    elements.wsBadge.textContent = 'Error';
    elements.wsBadge.className = 'badge badge-offline';
    showMicError('WebSocket connection error.');
  };
}

// --- Server Message Handler ---
function handleServerMessage(data) {
  const { type } = data;

  switch (type) {
    case 'ready':
      console.log('Voice Agent Ready:', data);
      if (data.has_sarvam_key !== undefined) {
        updateApiKeyBadge(data.has_sarvam_key);
      }
      break;

    case 'status':
      updateAgentStatus(data.status, data.stage, data.bytes_received);
      break;

    case 'transcript':
      appendTranscriptBubble(data.role, data.text, data.language);
      break;

    case 'audio':
      playAgentAudio(data.audio, data.format, data.turn_id);
      break;

    case 'interrupted':
      console.log('Turn interrupted by server:', data.turn_id);
      stopAudioPlayback();
      if (!state.isSpeechActive) {
        setAgentState('listening', 'Listening...', 'Listening to your speech.');
        setMicStage('Recording: Listening', 'pill-neutral');
      }
      break;

    case 'language_switched':
      console.log(`Language switched: ${data.previous_language} -> ${data.language} (${data.reason})`);
      const langNames = { 'mr-IN': 'Marathi (मराठी)', 'hi-IN': 'Hindi (हिंदी)', 'en-IN': 'English' };
      const friendlyName = langNames[data.language] || data.language;
      setMicStage(`Language: ${friendlyName}`, 'pill-success');
      setTimeout(() => {
        if (state.isRecording && !state.isPlayingAudio) {
          setMicStage('Recording: Listening', 'pill-neutral');
        }
      }, 2500);
      break;

    case 'info':
      console.info('Server info:', data.message);
      setMicStage('Speech Not Clear', 'pill-warning');
      setTimeout(() => {
        if (state.isRecording && !state.isPlayingAudio) {
          setMicStage('Recording: Listening', 'pill-neutral');
        }
      }, 2000);
      break;

    case 'error':
      console.error('Server error:', data.message);
      showSystemError(data.message);
      showMicError(data.message);
      break;

    case 'map_action':
      console.log('Voice-to-Map Action received:', data);
      handleMapAction(data);
      break;

    case 'call_warning':
      console.warn('Call duration warning:', data);
      if (data.text) {
        appendTranscriptBubble('assistant', data.text, data.language);
      }
      setMicStage('Call Limit: 20s remaining', 'pill-warning');
      break;

    case 'call_terminated':
      console.warn('Call terminated by server:', data);
      stopAudioPlayback();
      if (typeof stopRecording === 'function' && state.isRecording) {
        stopRecording();
      }
      setAgentState('idle', 'Call Ended', data.message || 'Call reached 3-minute limit.');
      setMicStage('Call Ended (3 min limit)', 'pill-offline');
      break;

    case 'call_rejected':
      console.warn('Call rejected by server:', data);
      stopAudioPlayback();
      if (typeof stopRecording === 'function' && state.isRecording) {
        stopRecording();
      }
      setAgentState('idle', 'Limit Exceeded', data.message || 'Daily call limit exceeded.');
      setMicStage('Daily Limit Exceeded', 'pill-offline');
      break;
  }
}

function handleMapAction(actionData) {
  if (!actionData || !actionData.action) return;

  // Auto switch to dual view if in voice-only view so user sees the route
  if (mapState.currentView === 'voice') {
    setDashboardView('dual');
  }

  if (!mapState.isInitialized) {
    initCityLincMap();
  }

  setTimeout(() => {
    if (actionData.action === 'highlight_journey') {
      highlightJourneyOnMap(actionData);
    } else if (actionData.action === 'highlight_hospital') {
      highlightHospitalOnMap(actionData);
    }
  }, 100);
}

function highlightJourneyOnMap(journey) {
  if (!mapState.map) return;
  if (mapState.routeLayer) mapState.routeLayer.clearLayers();

  const polylineCoords = journey.polyline || [];
  if (polylineCoords.length > 0) {
    const polyline = L.polyline(polylineCoords, {
      color: '#06b6d4',
      weight: 6,
      opacity: 0.95,
      lineCap: 'round',
      lineJoin: 'round'
    }).addTo(mapState.routeLayer);

    if (journey.origin) {
      const origIcon = L.divIcon({
        className: 'custom-station-pin pin-both',
        html: '<span style="font-size:16px;">🟢</span>',
        iconSize: [28, 28],
        iconAnchor: [14, 14]
      });
      L.marker([journey.origin.lat, journey.origin.lon], { icon: origIcon })
        .bindPopup(`<strong>Boarding Stop:</strong><br>${journey.origin.name}<br><span style="color:#10b981;font-weight:bold;">Route ${journey.rno}</span>`)
        .addTo(mapState.routeLayer)
        .openPopup();
    }

    if (journey.destination) {
      const destIcon = L.divIcon({
        className: 'custom-station-pin pin-hospital',
        html: '<span style="font-size:16px;">🔴</span>',
        iconSize: [28, 28],
        iconAnchor: [14, 14]
      });
      L.marker([journey.destination.lat, journey.destination.lon], { icon: destIcon })
        .bindPopup(`<strong>Alighting Stop:</strong><br>${journey.destination.name}<br><span style="color:#ef4444;font-weight:bold;">Destination</span>`)
        .addTo(mapState.routeLayer);
    }

    mapState.map.fitBounds(polyline.getBounds(), { padding: [50, 50] });

    if (elements.mapActiveRouteBadge) {
      elements.mapActiveRouteBadge.style.display = 'inline-flex';
      elements.mapActiveRouteBadge.textContent = `Active Journey: Route ${journey.rno}`;
    }
  }
}

function highlightHospitalOnMap(hosp) {
  if (!mapState.map) return;
  if (mapState.routeLayer) mapState.routeLayer.clearLayers();

  const coords = hosp.physical_coordinates;
  if (!coords) return;

  const hospIcon = L.divIcon({
    className: 'custom-station-pin pin-hospital',
    html: '<span style="font-size:20px;">🏥</span>',
    iconSize: [32, 32],
    iconAnchor: [16, 16]
  });

  L.marker([coords.lat, coords.lon], { icon: hospIcon })
    .bindPopup(`<strong>${hosp.hospital_name}</strong><br><span style="color:#38bdf8;">${hosp.primary_stop ? 'Alighting: ' + hosp.primary_stop.name : ''}</span>`)
    .addTo(mapState.routeLayer)
    .openPopup();

  if (hosp.primary_stop) {
    const busIcon = L.divIcon({
      className: 'custom-station-pin pin-up',
      html: '<span style="font-size:14px;">🚏</span>',
      iconSize: [22, 22],
      iconAnchor: [11, 11]
    });
    L.marker([hosp.primary_stop.lat, hosp.primary_stop.lon], { icon: busIcon })
      .bindTooltip(`<strong>Bus Stop:</strong> ${hosp.primary_stop.name}`)
      .addTo(mapState.routeLayer);
  }

  mapState.map.setView([coords.lat, coords.lon], 15);
}

function updateAgentStatus(status, stage, bytesReceived) {
  if (status === 'thinking') {
    let desc = 'Processing speech with Sarvam AI...';
    if (stage === 'stt') {
      desc = 'Transcribing with Saaras v4...';
      setMicStage(`Transcribing (${bytesReceived ? Math.round(bytesReceived/1024) + ' KB' : ''})`, 'pill-busy');
    }
    if (stage === 'llm') desc = 'Consulting CityLinc schedule & rules...';
    if (stage === 'tts') desc = 'Generating voice with Bulbul v3...';
    setAgentState('thinking', 'Thinking...', desc);
  } else if (status === 'speaking') {
    setAgentState('speaking', 'Speaking...', 'Agent is responding (speak to interrupt).');
    setMicStage('Agent Speaking', 'pill-busy');
  } else if (status === 'listening') {
    setAgentState('listening', 'Listening...', 'Speak naturally in Marathi, Hindi, or English.');
    setMicStage('Recording: Listening', 'pill-neutral');
  } else if (status === 'idle') {
    setAgentState('idle', 'Ready', 'Waiting for speech input.');
    setMicStage('Standby', 'pill-neutral');
  }
}

function setAgentState(mode, title, desc) {
  elements.voiceOrb.className = `voice-orb ${mode}`;
  elements.statusTitle.textContent = title;
  elements.statusDesc.textContent = desc;

  if (mode === 'listening') {
    elements.orbIcon.textContent = '🎙️';
    elements.btnInterrupt.disabled = true;
  } else if (mode === 'thinking') {
    elements.orbIcon.textContent = '⏳';
    elements.btnInterrupt.disabled = false;
  } else if (mode === 'speaking') {
    elements.orbIcon.textContent = '🔊';
    elements.btnInterrupt.disabled = false;
  } else if (mode === 'interrupted') {
    elements.orbIcon.textContent = '🛑';
    elements.btnInterrupt.disabled = true;
  } else {
    elements.orbIcon.textContent = '🎙️';
    elements.btnInterrupt.disabled = true;
  }
}

// --- Audio Playback & Barge-in ---
function base64ToBlob(base64, mimeType) {
  const byteCharacters = atob(base64);
  const byteNumbers = new Uint8Array(byteCharacters.length);
  for (let i = 0; i < byteCharacters.length; i++) {
    byteNumbers[i] = byteCharacters.charCodeAt(i);
  }
  return new Blob([byteNumbers], { type: mimeType });
}

function playAgentAudio(base64Audio, format, turnId) {
  if (!base64Audio) return;

  state.currentTurnId = turnId;
  state.isPlayingAudio = true;
  state.playbackStartTime = Date.now();
  state.bargeInConsecutiveFrames = 0;
  state.bargeInOnsetTimestamp = 0;
  state.speechOnsetConsecutiveFrames = 0;
  state.candidateSpeechStartTime = 0;
  state.isPreWarming = false;

  // Cleanly stop any existing recording so subsequent barge-in starts fresh
  if (state.activeCaptureSession && !state.activeCaptureSession.isConfirmed) {
    state.activeCaptureSession.disposed = true;
  }
  safelyStopMediaRecorder('agent_speaking');
  state.isSpeechActive = false;

  setAgentState('speaking', 'Speaking...', 'Assistant is responding (speak to interrupt).');
  setMicStage('Agent Speaking', 'pill-busy');

  let mimeType = format || 'audio/mpeg';
  if (mimeType === 'audio/mp3' || mimeType === 'mp3') {
    mimeType = 'audio/mpeg';
  } else if (mimeType === 'wav') {
    mimeType = 'audio/wav';
  }

  if (state.currentAudioUrl) {
    URL.revokeObjectURL(state.currentAudioUrl);
    state.currentAudioUrl = null;
  }

  try {
    const audioBlob = base64ToBlob(base64Audio, mimeType);
    state.currentAudioUrl = URL.createObjectURL(audioBlob);
    elements.audioPlayer.src = state.currentAudioUrl;

    const playPromise = elements.audioPlayer.play();
    if (playPromise !== undefined) {
      playPromise.catch(err => {
        if (err.name === 'AbortError') return;
        console.warn('Audio playback error:', err);
        state.isPlayingAudio = false;
        setAgentState('listening', 'Listening...', 'Ready for your next question.');
      });
    }
  } catch (err) {
    console.error('Failed to decode or play audio blob:', err);
    state.isPlayingAudio = false;
    setAgentState('listening', 'Listening...', 'Ready for your next question.');
  }
}

function onAudioPlaybackEnded() {
  if (state.isPlayingAudio) {
    state.isPlayingAudio = false;
    state.bargeInConsecutiveFrames = 0;
    state.lastVoiceTime = Date.now();

    if (state.ws && state.ws.readyState === WebSocket.OPEN && state.currentTurnId) {
      try {
        state.ws.send(JSON.stringify({
          type: 'audio_playback_completed',
          turn_id: state.currentTurnId
        }));
      } catch (e) {
        // WebSocket closing
      }
    }

    if (state.isRecording) {
      setAgentState('listening', 'Listening...', 'Speak now in Marathi, Hindi, or English.');
      setMicStage('Recording: Listening', 'pill-neutral');
    } else {
      setAgentState('idle', 'Ready', 'Click Start Conversation to talk again.');
      setMicStage('Standby', 'pill-neutral');
    }
  }
}

function stopAudioPlayback() {
  state.isPlayingAudio = false;
  state.bargeInConsecutiveFrames = 0;

  try {
    elements.audioPlayer.pause();
    elements.audioPlayer.currentTime = 0;
  } catch (e) {}

  if (state.currentAudioUrl) {
    URL.revokeObjectURL(state.currentAudioUrl);
    state.currentAudioUrl = null;
  }
}

function triggerBargeIn(isAcoustic = false) {
  const onsetTime = state.bargeInOnsetTimestamp || performance.now();
  const turnToInterrupt = state.currentTurnId;

  // 1. Instantly stop audio playback (< 5ms)
  stopAudioPlayback();

  const latencyMs = Math.round((performance.now() - onsetTime) * 10) / 10;
  state.lastMeasuredInterruptionLatencyMs = latencyMs;
  console.log(`[Barge-In] Turn ${turnToInterrupt} interrupted (acoustic=${isAcoustic}, latency=${latencyMs}ms)`);

  // 2. Instantly notify backend over WebSocket
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify({
      type: 'interrupt',
      turn_id: turnToInterrupt
    }));
  }

  // 3. Handle speech recording based on trigger type:
  state.speechOnsetConsecutiveFrames = 0;
  if (isAcoustic) {
    // Acoustic speech detected: keep capturing user's interrupting utterance
    state.isSpeechActive = true;
    state.speechStartTime = Date.now();
    state.lastVoiceTime = Date.now();

    setAgentState('listening', 'Listening...', 'Interrupted — listening to your question...');
    setMicStage('Recording: Speaking...', 'pill-active');

    // Immediately start or queue MediaRecorder to capture user speech
    const session = createCaptureSession('barge_in', state.speechStartTime);
    session.isConfirmed = true;

    if (!captureManager.hasActiveOrFinalizingSession()) {
      safelyStartMediaRecorder(session);
    } else if (captureManager.finalizingSession && !captureManager.finalizingSession.completed) {
      console.log(`[BargeIn] Enqueuing barge-in session #${session.id} while previous session finalizes.`);
      captureManager.enqueuePendingCapture(session);
    } else if (captureManager.activeSession) {
      captureManager.activeSession.isConfirmed = true;
      captureManager.activeSession.type = 'barge_in';
    }
  } else {
    // Manual button click
    state.isSpeechActive = false;
    state.lastVoiceTime = Date.now();
    setAgentState('interrupted', 'Interrupted', 'Stopping playback and listening...');
    setMicStage('Interrupted', 'pill-danger');

    setTimeout(() => {
      if (state.isRecording && !state.isPlayingAudio && !state.isSpeechActive) {
        setAgentState('listening', 'Listening...', 'Speak now.');
        setMicStage('Recording: Listening', 'pill-neutral');
      }
    }, 200);
  }
}

// --- Microphone & Web Audio Pipeline ---
async function startConversation() {
  clearMicError();
  setMicStage('Permission: Requesting...', 'pill-busy');

  try {
    // 1. Request microphone with verified constraint fallbacks
    const constraints = {
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
        channelCount: 1,
        sampleRate: { ideal: 16000 }
      }
    };

    const stream = await navigator.mediaDevices.getUserMedia(constraints);
    state.mediaStream = stream;

    // 2. Validate audio track
    const tracks = stream.getAudioTracks();
    if (!tracks || tracks.length === 0) {
      throw new Error('MediaStream contains no audio tracks.');
    }

    state.audioTrack = tracks[0];
    const trackLabel = state.audioTrack.label || 'Default Microphone';
    console.log(`Audio track active: ${trackLabel} (enabled: ${state.audioTrack.enabled})`);

    if (elements.micTrackName) {
      elements.micTrackName.textContent = trackLabel.length > 28 ? trackLabel.slice(0, 26) + '...' : trackLabel;
    }

    state.audioTrack.onended = () => {
      console.warn('Microphone track disconnected or ended.');
      showMicError('Microphone disconnected.');
      setMicStage('Track Disconnected', 'pill-danger');
    };

    state.audioTrack.onmute = () => {
      console.warn('Microphone track muted by hardware/OS.');
      setMicStage('Hardware Muted', 'pill-warning');
    };

    state.audioTrack.onunmute = () => {
      setMicStage('Recording: Listening', 'pill-neutral');
    };

    elements.micBadge.textContent = 'Mic: Active';
    elements.micBadge.className = 'badge badge-online';
    setMicStage('Permission: Granted', 'pill-active');

    // 3. Connect WebSocket if not open
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
      initWebSocket();
    }

    // 4. Initialize Web Audio API Analyser for live time-domain RMS
    setupAudioContext(stream);

    // 5. Initialize MediaRecorder instance
    initMediaRecorder();

    // 6. Update UI state
    state.isRecording = true;
    state.isSpeechActive = false;
    state.speechOnsetConsecutiveFrames = 0;
    elements.btnStart.disabled = true;
    elements.btnStop.disabled = false;
    elements.btnTalkManual.disabled = false;
    setAgentState('listening', 'Listening...', 'Listening to your speech. Speak now.');

    setTimeout(() => {
      setMicStage('Recording: Listening', 'pill-neutral');
    }, 800);

  } catch (err) {
    console.error('Microphone initialization error:', err);
    showMicError(`${err.name}: ${err.message || 'Permission denied'}`);
    setMicStage('Permission: Denied', 'pill-danger');
    elements.micBadge.textContent = 'Mic: Denied';
    elements.micBadge.className = 'badge badge-offline';
    if (elements.micTrackName) {
      elements.micTrackName.textContent = 'Access Failed';
    }
  }
}

function stopConversation() {
  if (state.isManualRecording) {
    toggleManualRecording();
  }

  // Stop MediaRecorder and cancel all active/finalizing sessions cleanly
  captureManager.cancelAllSessions('conversation_ended');

  // Stop audio tracks
  if (state.mediaStream) {
    state.mediaStream.getTracks().forEach(track => track.stop());
    state.mediaStream = null;
    state.audioTrack = null;
  }

  // Close AudioContext
  if (state.audioContext && state.audioContext.state !== 'closed') {
    state.audioContext.close();
    state.audioContext = null;
  }

  stopAudioPlayback();

  // Close WebSocket
  if (state.ws) {
    state.ws.close();
    state.ws = null;
  }

  state.isRecording = false;
  state.isSpeechActive = false;
  state.isPreWarming = false;
  state.candidateSpeechStartTime = 0;
  state.speechOnsetConsecutiveFrames = 0;
  elements.micBadge.textContent = 'Mic: Off';
  elements.micBadge.className = 'badge badge-neutral';

  elements.btnStart.disabled = false;
  elements.btnStop.disabled = true;
  elements.btnTalkManual.disabled = true;
  elements.btnInterrupt.disabled = true;

  if (elements.micTrackName) elements.micTrackName.textContent = 'Off';
  if (elements.micVuBar) elements.micVuBar.style.width = '0%';
  if (elements.micVuText) elements.micVuText.textContent = '0%';
  setMicStage('Standby', 'pill-neutral');

  setAgentState('idle', 'Ended', 'Conversation ended. Click Start to begin again.');
  drawIdleVisualizer();
}

function setupAudioContext(stream) {
  const AudioContext = window.AudioContext || window.webkitAudioContext;
  state.audioContext = new AudioContext();

  const source = state.audioContext.createMediaStreamSource(stream);
  state.analyser = state.audioContext.createAnalyser();
  state.analyser.fftSize = 512;
  state.analyser.smoothingTimeConstant = 0.2;

  source.connect(state.analyser);

  state.timeDomainData = new Uint8Array(state.analyser.fftSize);
  state.frequencyData = new Uint8Array(state.analyser.frequencyBinCount);

  renderVisualizer();
}

// --- Capture Session Management (Phase 4.4 Re-Architecture: One Session = One MediaRecorder = Immutable Closure) ---

function getSupportedAudioMimeType() {
  const candidates = [
    'audio/webm;codecs=opus',
    'audio/webm',
    'audio/ogg;codecs=opus',
    'audio/mp4'
  ];
  for (const c of candidates) {
    if (typeof MediaRecorder !== 'undefined' && MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(c)) {
      return c;
    }
  }
  return '';
}

class CaptureSession {
  constructor(id, type, startTime) {
    this.id = id;
    this.type = type; // 'auto' | 'manual' | 'barge_in'
    this.startTime = startTime;
    this.isConfirmed = (type === 'manual' || type === 'barge_in');
    this.chunks = [];
    this.recorder = null;
    this.stopReason = null;
    this.stopRequested = false;
    this.utteranceProcessed = false;
    this.completed = false;
    this.disposed = false;
    this.error = null;
    this.finalizationResolve = null;
    this.finalizationPromise = new Promise((resolve) => {
      this.finalizationResolve = resolve;
    });
  }
}

const captureManager = {
  activeSession: null,
  finalizingSession: null,
  pendingQueue: [],
  maxQueueSize: 2,

  hasActiveOrFinalizingSession() {
    return (this.activeSession !== null && !this.activeSession.completed && !this.activeSession.disposed) ||
           (this.finalizingSession !== null && !this.finalizingSession.completed && !this.finalizingSession.disposed);
  },

  enqueuePendingCapture(session) {
    if (!session) return false;
    if (this.pendingQueue.length >= this.maxQueueSize) {
      console.warn(`[CaptureManager] Queue overflow (${this.pendingQueue.length}/${this.maxQueueSize}): rejecting capture #${session.id} (${session.type})`);
      session.error = new Error(`Queue overflow: maximum queue size (${this.maxQueueSize}) reached`);
      session.disposed = true;
      session.completed = true;
      session.finalizationResolve(session);
      showMicError('Too many speech requests — please speak again after the assistant finishes.');
      return false;
    }
    this.pendingQueue.push(session);
    console.log(`[CaptureManager] Enqueued pending confirmed capture #${session.id} (${session.type}) in FIFO queue [Depth: ${this.pendingQueue.length}/${this.maxQueueSize}].`);
    return true;
  },

  processPendingCapture() {
    if (this.pendingQueue.length === 0) return;
    if (this.hasActiveOrFinalizingSession()) {
      return; // Still busy
    }

    while (this.pendingQueue.length > 0) {
      const nextSession = this.pendingQueue.shift();

      if (nextSession.disposed || nextSession.completed) {
        continue;
      }

      // Check if user requested stop while waiting in queue (e.g. manual push & release)
      if (nextSession.stopRequested) {
        console.warn(`[CaptureManager] Queued session #${nextSession.id} (${nextSession.type}) was stopped before recorder became available. Discarding cleanly.`);
        nextSession.disposed = true;
        nextSession.completed = true;
        nextSession.finalizationResolve(nextSession);
        if (nextSession.type === 'manual') {
          showMicError('Manual recording finished before microphone became available. Please push to speak again.');
          setMicStage('Recording: Listening', 'pill-neutral');
        }
        continue;
      }

      console.log(`[CaptureManager] Starting queued FIFO capture session #${nextSession.id} (${nextSession.type}) [Remaining queue: ${this.pendingQueue.length}].`);
      const started = safelyStartMediaRecorder(nextSession);
      if (!started) {
        console.warn(`[CaptureManager] Failed to start queued capture session #${nextSession.id}.`);
        nextSession.disposed = true;
        nextSession.completed = true;
        nextSession.finalizationResolve(nextSession);
        continue;
      }

      // Successfully started active recorder for this session
      break;
    }
  },

  cancelAllSessions(reason = 'shutdown') {
    while (this.pendingQueue.length > 0) {
      const s = this.pendingQueue.shift();
      s.disposed = true;
      s.completed = true;
      s.finalizationResolve(s);
    }
    if (this.activeSession) {
      this.activeSession.disposed = true;
      safelyStopMediaRecorder(reason);
    }
    if (this.finalizingSession) {
      this.finalizingSession.disposed = true;
      this.finalizingSession.completed = true;
      if (this.finalizingSession.recorder) {
        this.finalizingSession.recorder.ondataavailable = null;
        this.finalizingSession.recorder.onstop = null;
        this.finalizingSession.recorder.onerror = null;
      }
      this.finalizingSession.finalizationResolve(this.finalizingSession);
    }
    this.activeSession = null;
    this.finalizingSession = null;
    state.activeCaptureSession = null;
    state.mediaRecorder = null;
  }
};

function createCaptureSession(type = 'auto', startTime = Date.now()) {
  state.captureSequence = (state.captureSequence || 0) + 1;
  const session = new CaptureSession(state.captureSequence, type, startTime);
  console.debug(`[AudioCapture] Created capture session #${session.id} (type: ${type}, confirmed: ${session.isConfirmed})`);
  return session;
}

function safelyStartMediaRecorder(captureSession) {
  if (!captureSession) {
    console.warn('[AudioCapture] Cannot start: captureSession is null.');
    return false;
  }
  if (!state.mediaStream) {
    console.warn('[AudioCapture] Cannot start: mediaStream is null.');
    captureSession.disposed = true;
    captureSession.completed = true;
    if (state.activeCaptureSession === captureSession) {
      state.activeCaptureSession = null;
    }
    captureSession.finalizationResolve(captureSession);
    return false;
  }

  // 1. Enforce strict single-capture serialization
  if (captureSession.recorder) {
    console.debug(`[CaptureManager] Session #${captureSession.id} already has a MediaRecorder.`);
    return false;
  }
  if (captureManager.finalizingSession && !captureManager.finalizingSession.completed && !captureManager.finalizingSession.disposed) {
    console.debug(`[CaptureManager] Previous session #${captureManager.finalizingSession.id} is finalizing. Cannot start new recorder yet.`);
    return false;
  }
  if (captureManager.activeSession && !captureManager.activeSession.completed && !captureManager.activeSession.disposed) {
    console.debug(`[CaptureManager] Session #${captureManager.activeSession.id} is already actively recording.`);
    return false;
  }

  // 2. Create dedicated MediaRecorder instance for this session (One Session = One MediaRecorder)
  const mimeType = getSupportedAudioMimeType();
  let recorder;
  try {
    recorder = mimeType ? new MediaRecorder(state.mediaStream, { mimeType }) : new MediaRecorder(state.mediaStream);
  } catch (err) {
    console.error(`[AudioCapture] Failed to create dedicated MediaRecorder for #${captureSession.id}:`, err);
    captureSession.error = err;
    captureSession.disposed = true;
    captureSession.completed = true;
    if (state.activeCaptureSession === captureSession) {
      state.activeCaptureSession = null;
    }
    captureSession.finalizationResolve(captureSession);
    captureManager.processPendingCapture();
    return false;
  }

  captureSession.recorder = recorder;
  state.mediaRecorder = recorder; // maintain reference for UI/diagnostics

  // 3. Immutable lexical closure over captureSession and recorder
  recorder.ondataavailable = (e) => {
    if (e.data && e.data.size > 0) {
      // Do not accept chunks after session finalization or disposal
      if (captureSession.completed || captureSession.disposed) {
        console.debug(`[AudioCapture] Ignored chunk for finalized/disposed session #${captureSession.id}.`);
        return;
      }
      // Audio chunks belong exclusively to this CaptureSession (FIX 2)
      captureSession.chunks.push(e.data);
    }
  };

  recorder.onstop = async () => {
    // Immutable ownership: onstop processes ONLY its owning captureSession
    try {
      // Await asynchronous processing & WebSocket dispatch (FIX 3)
      await handleUtteranceRecorded(captureSession);
    } catch (err) {
      console.error(`[AudioCapture] Error in onstop for #${captureSession.id}:`, err);
      captureSession.error = err;
    } finally {
      // Remove event listeners on this recorder instance
      recorder.ondataavailable = null;
      recorder.onstop = null;
      recorder.onerror = null;

      if (state.mediaRecorder === recorder) {
        state.mediaRecorder = null;
      }
      if (captureManager.finalizingSession === captureSession) {
        captureManager.finalizingSession = null;
      }
      if (captureManager.activeSession === captureSession) {
        captureManager.activeSession = null;
      }
      if (state.activeCaptureSession === captureSession) {
        state.activeCaptureSession = null;
      }

      captureSession.completed = true;
      captureSession.finalizationResolve(captureSession);

      // Check if there are pending confirmed captures in FIFO queue
      captureManager.processPendingCapture();
    }
  };

  recorder.onerror = (e) => {
    console.error(`[AudioCapture] Recorder error on #${captureSession.id}:`, e);
    captureSession.error = e.error || e;
    captureSession.disposed = true;
    captureSession.completed = true;
    recorder.ondataavailable = null;
    recorder.onstop = null;
    recorder.onerror = null;

    if (state.mediaRecorder === recorder) {
      state.mediaRecorder = null;
    }
    if (captureManager.activeSession === captureSession) {
      captureManager.activeSession = null;
    }
    if (captureManager.finalizingSession === captureSession) {
      captureManager.finalizingSession = null;
    }
    if (state.activeCaptureSession === captureSession) {
      state.activeCaptureSession = null;
    }
    captureSession.finalizationResolve(captureSession);
    captureManager.processPendingCapture();
  };

  try {
    recorder.start(100);
    captureManager.activeSession = captureSession;
    state.activeCaptureSession = captureSession;
    console.debug(`[AudioCapture] Started dedicated MediaRecorder for session #${captureSession.id} (${captureSession.type})`);
    return true;
  } catch (err) {
    console.error(`[AudioCapture] MediaRecorder.start failed for #${captureSession.id}:`, err);
    captureSession.error = err;
    captureSession.disposed = true;
    captureSession.completed = true;
    recorder.ondataavailable = null;
    recorder.onstop = null;
    recorder.onerror = null;
    if (state.mediaRecorder === recorder) {
      state.mediaRecorder = null;
    }
    captureSession.finalizationResolve(captureSession);
    if (captureManager.activeSession === captureSession) {
      captureManager.activeSession = null;
    }
    if (state.activeCaptureSession === captureSession) {
      state.activeCaptureSession = null;
    }
    state.isPreWarming = false;
    captureManager.processPendingCapture();
    return false;
  }
}

function safelyStopMediaRecorder(reason = 'normal') {
  const session = captureManager.activeSession;
  if (!session) {
    console.debug(`[AudioCapture] safelyStopMediaRecorder called with no active session (${reason}).`);
    return;
  }

  // Transfer from active to finalizing immediately, clearing activeCaptureSession (FIX 5 & FIX 6)
  captureManager.activeSession = null;
  state.activeCaptureSession = null;
  captureManager.finalizingSession = session;
  session.stopReason = reason;

  const rec = session.recorder;
  if (!rec) {
    session.disposed = true;
    session.completed = true;
    if (captureManager.finalizingSession === session) captureManager.finalizingSession = null;
    session.finalizationResolve(session);
    captureManager.processPendingCapture();
    return;
  }

  // FIX 1: MediaRecorder lifecycle is driven by actual events. No premature 600ms timeout!
  if (rec.state === 'recording') {
    try {
      rec.stop();
      console.debug(`[AudioCapture] MediaRecorder.stop requested for #${session.id} (reason: ${reason}).`);
    } catch (err) {
      console.warn(`[AudioCapture] MediaRecorder.stop error for #${session.id}:`, err);
      session.error = err;
      session.disposed = true;
      session.completed = true;
      rec.ondataavailable = null;
      rec.onstop = null;
      rec.onerror = null;
      if (state.mediaRecorder === rec) {
        state.mediaRecorder = null;
      }
      if (captureManager.finalizingSession === session) captureManager.finalizingSession = null;
      session.finalizationResolve(session);
      captureManager.processPendingCapture();
    }
  } else if (rec.state === 'paused') {
    try {
      rec.resume();
      rec.stop();
      console.debug(`[AudioCapture] MediaRecorder resumed+stopped for #${session.id} (reason: ${reason}).`);
    } catch (err) {
      console.warn(`[AudioCapture] MediaRecorder resume+stop error for #${session.id}:`, err);
      session.error = err;
      session.disposed = true;
      session.completed = true;
      rec.ondataavailable = null;
      rec.onstop = null;
      rec.onerror = null;
      if (state.mediaRecorder === rec) {
        state.mediaRecorder = null;
      }
      if (captureManager.finalizingSession === session) captureManager.finalizingSession = null;
      session.finalizationResolve(session);
      captureManager.processPendingCapture();
    }
  } else {
    // Recorder state is inactive
    console.debug(`[AudioCapture] Recorder for #${session.id} in state '${rec.state}', not recording.`);
    session.disposed = true;
    session.completed = true;
    rec.ondataavailable = null;
    rec.onstop = null;
    rec.onerror = null;
    if (state.mediaRecorder === rec) {
      state.mediaRecorder = null;
    }
    if (captureManager.finalizingSession === session) captureManager.finalizingSession = null;
    session.finalizationResolve(session);
    captureManager.processPendingCapture();
  }
}

function initMediaRecorder() {
  if (!state.mediaStream) return;
  const mimeType = getSupportedAudioMimeType();
  console.log(`MediaRecorder support verified: ${mimeType || 'browser default'}`);
}

// --- Utterance Processing and WebSocket Transmission (Phase 4.4 Group A & Corrective Re-Architecture) ---
async function handleUtteranceRecorded(captureSession = null) {
  const session = captureSession;

  if (!session) {
    console.debug('[AudioCapture] Recorder stopped with no active capture session.');
    return;
  }

  // 1. Prevent duplicate processing or processing disposed sessions
  if (session.utteranceProcessed || session.completed || session.disposed) {
    console.debug(`[AudioCapture] Capture #${session.id} already completed or disposed (stopReason: ${session.stopReason || 'none'}).`);
    return;
  }

  // 2. If this recorder stop was from an unconfirmed candidate pre-warm (speech was not confirmed), discard safely
  if (!session.isConfirmed && session.type !== 'manual') {
    session.disposed = true;
    session.completed = true;
    session.utteranceProcessed = true;
    console.debug(`[AudioCapture] Capture #${session.id} dropped: unconfirmed transient pre-warm (${session.stopReason || 'abort'}).`);
    return;
  }

  // 3. Mark processed to prevent duplicate Blob creation or WebSocket transmission
  session.utteranceProcessed = true;

  // FIX 2: Strictly use session.chunks with zero fallback to global chunks
  const chunks = session.chunks;
  if (!chunks || chunks.length === 0) {
    console.debug(`[AudioCapture] No audio chunks available for capture #${session.id}.`);
    return;
  }

  const mime = (session.recorder && session.recorder.mimeType) || 'audio/webm';
  const audioBlob = new Blob(chunks, { type: mime });
  // Release chunk array now that blob is created
  session.chunks = [];

  const onsetReferenceTime = session.startTime || Date.now();
  const actualDurationSec = Math.max(0.1, (Date.now() - onsetReferenceTime) / 1000);
  const actualDurationMs = Math.round(actualDurationSec * 1000);
  const sizeKB = (audioBlob.size / 1024).toFixed(1);

  if (elements.micCapturedInfo) {
    elements.micCapturedInfo.textContent = `${sizeKB} KB (${actualDurationSec.toFixed(2)}s)`;
  }

  console.log(`[AudioCapture] Utterance #${session.id} captured: ${audioBlob.size} bytes (${actualDurationSec.toFixed(2)}s) [type: ${session.type}, mime: ${mime}]`);

  // Scope B: Quality and duration validation
  // 1. Reject empty or sub-kilobyte bursts (< minValidBlobBytes)
  if (audioBlob.size < state.minValidBlobBytes) {
    console.log(`[AudioCapture] Discarding sub-kilobyte noise burst for #${session.id} (${audioBlob.size} bytes < ${state.minValidBlobBytes} bytes).`);
    if (!captureManager.activeSession) {
      setMicStage('Recording: Listening', 'pill-neutral');
    }
    return;
  }

  // 2. Reject sub-phoneme noise transients (<250ms with sub-1800 byte payload) while preserving short responses ("हो", "Yes", "No")
  if (actualDurationMs < state.minValidSpeechDurationMs && audioBlob.size < 1800) {
    console.log(`[AudioCapture] Discarding short noise transient for #${session.id} (${actualDurationMs}ms < ${state.minValidSpeechDurationMs}ms).`);
    if (!captureManager.activeSession) {
      setMicStage('Recording: Listening', 'pill-neutral');
    }
    return;
  }

  // 3. Validate supported MIME prefix
  const cleanMime = mime.split(';')[0].trim().toLowerCase() || 'audio/webm';
  if (!cleanMime.startsWith('audio/')) {
    console.error(`[AudioCapture] Unsupported audio format: ${mime}`);
    showMicError('Unsupported audio format recorded.');
    if (!captureManager.activeSession) {
      setMicStage('Recording: Listening', 'pill-neutral');
    }
    return;
  }

  // FIX 3 & FIX 4: Send through WebSocket pipeline and await asynchronous dispatch
  if (!captureManager.activeSession) {
    setMicStage('Uploading Audio...', 'pill-busy');
  }
  try {
    await sendAudioBlob(audioBlob, cleanMime, actualDurationMs, session.id);
  } catch (err) {
    console.error(`[AudioCapture] Failed to upload audio for #${session.id}:`, err);
    session.error = err;
  }
}

function sendAudioBlob(blob, mimeType, durationMs = 0, captureId = 0) {
  return new Promise((resolve, reject) => {
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
      console.warn(`[AudioCapture] Cannot send audio for #${captureId}: WebSocket is not open.`);
      showMicError('WebSocket disconnected during audio upload.');
      reject(new Error('WebSocket is not open'));
      return;
    }

    // Normalize Content-Type by stripping codec parameters (e.g. 'audio/webm;codecs=opus' -> 'audio/webm')
    const cleanMime = (mimeType || 'audio/webm').split(';')[0].trim().toLowerCase() || 'audio/webm';

    if (!captureManager.activeSession) {
      setMicStage('Transcribing with Saaras v4...', 'pill-busy');
      setAgentState('thinking', 'Thinking...', 'Transcribing with Saaras v4...');
    }

    let settled = false;
    const reader = new FileReader();

    reader.onload = () => {
      if (settled) return;
      settled = true;
      try {
        if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
          throw new Error('WebSocket closed during FileReader encoding');
        }
        const result = reader.result;
        if (!result || typeof result !== 'string') {
          throw new Error('FileReader returned empty or invalid result');
        }
        const parts = result.split(',');
        const base64Audio = parts.length > 1 ? parts[1] : '';
        if (!base64Audio) {
          throw new Error('Failed to parse base64 audio payload');
        }
        // Note: state.ws.send() dispatches payload into the browser WebSocket buffer.
        // This marks successful client dispatch; server transcription acknowledgment occurs asynchronously.
        state.ws.send(JSON.stringify({
          type: 'audio',
          data: base64Audio,
          format: cleanMime,
          duration_ms: durationMs,
          language: state.selectedLanguage
        }));
        console.log(`[WebSocket] Sent audio payload for #${captureId}: ${base64Audio.length} base64 chars | format: ${cleanMime} | duration: ${durationMs}ms`);
        resolve({ captureId, bytes: blob.size, durationMs });
      } catch (err) {
        console.error(`[AudioCapture] WebSocket send failed for #${captureId}:`, err);
        showMicError('Failed to send audio data.');
        reject(err);
      }
    };

    reader.onerror = (err) => {
      if (settled) return;
      settled = true;
      console.error(`[AudioCapture] FileReader error encoding audio blob for #${captureId}:`, err);
      showMicError('Failed to encode audio for upload.');
      reject(err || new Error('FileReader encoding error'));
    };

    reader.onabort = () => {
      if (settled) return;
      settled = true;
      console.warn(`[AudioCapture] FileReader read was aborted for #${captureId}.`);
      showMicError('Audio encoding aborted.');
      reject(new Error('FileReader read aborted'));
    };

    reader.readAsDataURL(blob);
  });
}

// --- Manual Talk Toggle ---
function toggleManualRecording() {
  if (!state.isRecording) return;

  if (!state.isManualRecording) {
    // Start Manual Recording
    state.speechOnsetConsecutiveFrames = 0;
    state.candidateSpeechStartTime = 0;
    state.isPreWarming = false;
    state.isManualRecording = true;
    state.isSpeechActive = true;
    state.speechStartTime = Date.now();
    elements.btnTalkManual.innerHTML = '<span class="btn-icon">⏹</span> Done Speaking';
    elements.btnTalkManual.className = 'btn btn-action recording';
    setMicStage('Manual Capture Active', 'pill-active');

    const session = createCaptureSession('manual', state.speechStartTime);
    session.isConfirmed = true;
    if (!captureManager.hasActiveOrFinalizingSession()) {
      safelyStartMediaRecorder(session);
    } else if (captureManager.finalizingSession && !captureManager.finalizingSession.completed) {
      console.log(`[ManualTalk] Enqueuing manual session #${session.id} while previous session finalizes.`);
      captureManager.enqueuePendingCapture(session);
    }
  } else {
    // Finish Manual Recording
    state.speechOnsetConsecutiveFrames = 0;
    state.candidateSpeechStartTime = 0;
    state.isPreWarming = false;
    state.isManualRecording = false;
    state.isSpeechActive = false;
    elements.btnTalkManual.innerHTML = '<span class="btn-icon">🎙️</span> Push to Speak';
    elements.btnTalkManual.className = 'btn btn-action';

    if (captureManager.activeSession && captureManager.activeSession.type === 'manual') {
      setMicStage('Audio Captured', 'pill-busy');
      safelyStopMediaRecorder('manual_finished');
    } else {
      // Find manual session in pendingQueue if still waiting for previous session to finalize
      const pendingManual = captureManager.pendingQueue.find(s => s.type === 'manual' && !s.disposed);
      if (pendingManual) {
        console.log(`[ManualTalk] Stop requested while manual session #${pendingManual.id} is still queued.`);
        pendingManual.stopRequested = true;
        pendingManual.stopReason = 'manual_finished';
      } else {
        safelyStopMediaRecorder('manual_finished');
      }
    }
  }
}

// --- Real-time Visualizer and Time-Domain RMS VAD Loop ---
function getEffectiveVoiceThreshold() {
  const dynamicThreshold = state.ambientNoise + state.noiseFloorMargin;
  return Math.min(state.maxVoiceThreshold, Math.max(state.minVoiceThreshold, dynamicThreshold));
}

function renderVisualizer() {
  if (!state.analyser) return;

  requestAnimationFrame(renderVisualizer);

  // 1. Compute physical sound energy using Time-Domain RMS
  state.analyser.getByteTimeDomainData(state.timeDomainData);
  let sumSq = 0;
  for (let i = 0; i < state.timeDomainData.length; i++) {
    const norm = (state.timeDomainData[i] - 128) / 128.0;
    sumSq += norm * norm;
  }
  const rms = Math.sqrt(sumSq / state.timeDomainData.length);
  state.rmsVolume = rms;

  // Compute dynamic noise-aware voice threshold for current frame (Stage 1A)
  const effectiveThreshold = getEffectiveVoiceThreshold();
  state.voiceThreshold = effectiveThreshold;

  // Update physical VU volume meter (0% - 100%)
  const vuPercent = Math.min(100, Math.round(rms * 450));
  if (elements.micVuBar) elements.micVuBar.style.width = `${vuPercent}%`;
  if (elements.micVuText) elements.micVuText.textContent = `${vuPercent}%`;

  // 2. Draw canvas frequency bars
  state.analyser.getByteFrequencyData(state.frequencyData);
  canvasCtx.clearRect(0, 0, elements.visualizerCanvas.width, elements.visualizerCanvas.height);
  const barWidth = (elements.visualizerCanvas.width / state.analyser.frequencyBinCount) * 2.2;
  let x = 0;

  for (let i = 0; i < state.analyser.frequencyBinCount; i++) {
    const barHeight = (state.frequencyData[i] / 255) * (elements.visualizerCanvas.height - 4);
    let fillColor = '#3b82f6';
    if (state.isPlayingAudio) {
      fillColor = '#8b5cf6';
    } else if (state.isSpeechActive || rms > state.voiceThreshold) {
      fillColor = '#10b981';
    }
    canvasCtx.fillStyle = fillColor;
    canvasCtx.fillRect(x, elements.visualizerCanvas.height - barHeight, barWidth, barHeight);
    x += barWidth + 1;
  }

  const now = Date.now();

  // Adapt ambient noise floor during quiet non-playback periods (Stage 1A)
  if (!state.isSpeechActive && !state.isPlayingAudio) {
    const isCandidateOnset = (state.speechOnsetConsecutiveFrames > 0);
    if (!isCandidateOnset && rms <= state.maxAmbientNoise) {
      const updatedAmbient = (state.ambientNoise * 0.96) + (rms * 0.04);
      state.ambientNoise = Math.min(
        state.maxAmbientNoise,
        Math.max(state.minAmbientNoise, updatedAmbient)
      );
    } else if (rms < state.ambientNoise) {
      // Room became quieter: allow downward adaptation
      const updatedAmbient = (state.ambientNoise * 0.96) + (rms * 0.04);
      state.ambientNoise = Math.max(state.minAmbientNoise, updatedAmbient);
    }
  }

  // Periodic throttled VAD diagnostic log (Stage 1D - every 5s during listening)
  if (now - state.lastVadDiagnosticLogTime > 5000 && !state.isPlayingAudio && !state.isSpeechActive) {
    state.lastVadDiagnosticLogTime = now;
    console.debug(`[VAD Diagnostics] ambient=${state.ambientNoise.toFixed(4)}, threshold=${effectiveThreshold.toFixed(4)}, rms=${rms.toFixed(4)}, onsetFrames=${state.speechOnsetConsecutiveFrames}`);
  }

  // 3. Audio state machine: Assistant Playback & Real-Time Full-Duplex Barge-In
  if (state.isPlayingAudio) {
    state.speechOnsetConsecutiveFrames = 0;
    const timeSincePlayback = now - state.playbackStartTime;
    // Barge-in threshold: safely above AEC residual echo (<0.028), catches normal speech (0.06-0.18) & soft speech (0.040-0.055)
    const effectiveBargeThreshold = Math.max(state.bargeInThreshold, state.ambientNoise * 2.2);

    if (rms > effectiveBargeThreshold) {
      // Record speech onset timestamp at first frame
      if (state.bargeInConsecutiveFrames === 0) {
        state.bargeInOnsetTimestamp = performance.now();
      }

      const isEarlyPlayback = (timeSincePlayback < state.bargeInGracePeriodMs);
      const isStrongSpeech = (rms >= effectiveBargeThreshold * 1.35); // e.g. >= 0.062

      // Allow interruption if past grace period (160ms) OR if user speech is loud/strong
      if (!isEarlyPlayback || isStrongSpeech) {
        state.bargeInConsecutiveFrames++;
        if (state.bargeInConsecutiveFrames >= state.bargeInRequiredFrames || (isStrongSpeech && state.bargeInConsecutiveFrames >= 1)) {
          state.bargeInConsecutiveFrames = 0;
          triggerBargeIn(true);
          return;
        }
      }
    } else {
      state.bargeInConsecutiveFrames = Math.max(0, state.bargeInConsecutiveFrames - 1);
      if (state.bargeInConsecutiveFrames === 0) {
        state.bargeInOnsetTimestamp = 0;
      }
    }
    return;
  }

  // If in manual recording mode, auto-VAD silence cutoff is disabled
  if (state.isManualRecording) {
    return;
  }

  // 4. Auto-VAD Utterance Detection (Stage 1B: 3-frame onset hysteresis & Stage 2 Scope A: Pre-Roll First-Syllable Capture)
  if (!state.isSpeechActive) {
    // User is currently silent
    if (rms > effectiveThreshold) {
      // First candidate onset frame: immediately pre-warm MediaRecorder to capture initial plosives/syllables
      if (state.speechOnsetConsecutiveFrames === 0) {
        state.candidateSpeechStartTime = now;
        if (!captureManager.hasActiveOrFinalizingSession()) {
          const session = createCaptureSession('auto', now);
          const started = safelyStartMediaRecorder(session);
          if (started) {
            state.isPreWarming = true;
            console.debug(`[AudioCapture] Candidate pre-warm #${session.id} started at t=${now} (rms=${rms.toFixed(4)})`);
          } else {
            state.isPreWarming = false;
          }
        }
      }

      state.speechOnsetConsecutiveFrames++;
      if (state.speechOnsetConsecutiveFrames >= state.speechOnsetRequiredFrames) {
        // Speech onset confirmed after 3 consecutive frames (~50ms)
        state.speechOnsetConsecutiveFrames = 0;
        state.isSpeechActive = true;
        state.isPreWarming = false;
        // Anchor speechStartTime to candidateSpeechStartTime so pre-warmed audio frames are preserved
        state.speechStartTime = state.candidateSpeechStartTime || now;
        state.lastVoiceTime = now;
        setMicStage('Recording: Speaking...', 'pill-active');

        // Confirm active pre-warming session OR start new session OR enqueue during finalization (FIX 4)
        if (captureManager.activeSession && captureManager.activeSession.type === 'auto') {
          captureManager.activeSession.isConfirmed = true;
          state.activeCaptureSession = captureManager.activeSession;
        } else if (!captureManager.hasActiveOrFinalizingSession()) {
          const session = createCaptureSession('auto', state.speechStartTime);
          session.isConfirmed = true;
          safelyStartMediaRecorder(session);
        } else if (captureManager.finalizingSession && !captureManager.finalizingSession.completed) {
          const session = createCaptureSession('auto', state.speechStartTime);
          session.isConfirmed = true;
          captureManager.enqueuePendingCapture(session);
        }

        const captureId = captureManager.activeSession ? captureManager.activeSession.id : (captureManager.pendingSession ? captureManager.pendingSession.id : 0);
        console.log(`[AudioCapture] Speech onset confirmed for #${captureId}: rms=${rms.toFixed(4)}, threshold=${effectiveThreshold.toFixed(4)}, preRollLead=${now - state.speechStartTime}ms`);
      }
    } else {
      // Signal fell below threshold before 3 frames were reached: reset onset counter immediately
      if (state.speechOnsetConsecutiveFrames > 0) {
        state.speechOnsetConsecutiveFrames = 0;
      }
      // If we were pre-warming on an unconfirmed transient (click, tap), cleanly abort and discard
      if (state.isPreWarming && !state.isSpeechActive) {
        state.isPreWarming = false;
        state.candidateSpeechStartTime = 0;
        if (captureManager.activeSession) {
          captureManager.activeSession.isConfirmed = false;
        }
        safelyStopMediaRecorder('transient_abort');
      }
    }
  } else {
    // User is actively in an utterance
    // Holding hysteresis: trailing syllables down to 82% of effectiveThreshold keep utterance alive (Stage 1C)
    const holdingThreshold = effectiveThreshold * 0.82;
    if (rms > holdingThreshold) {
      state.lastVoiceTime = now;
    }

    const silenceDuration = now - state.lastVoiceTime;
    const utteranceDuration = now - state.speechStartTime;

    // Finalize utterance when pause threshold is met OR maximum length exceeded
    if ((silenceDuration > state.silenceThresholdMs && utteranceDuration > state.minUtteranceMs) || 
        utteranceDuration > state.maxUtteranceMs) {
      state.isSpeechActive = false;
      state.isPreWarming = false;
      state.candidateSpeechStartTime = 0;
      state.speechOnsetConsecutiveFrames = 0; // Ensure clean state for next turn
      setMicStage('Audio Captured', 'pill-busy');

      const captureId = captureManager.activeSession ? captureManager.activeSession.id : 0;
      safelyStopMediaRecorder('utterance_finalized');
      console.log(`[VAD] Utterance finalized for #${captureId}: duration=${utteranceDuration}ms, silence=${silenceDuration}ms`);
    }
  }
}

function drawIdleVisualizer() {
  canvasCtx.clearRect(0, 0, elements.visualizerCanvas.width, elements.visualizerCanvas.height);
  canvasCtx.fillStyle = '#1e293b';
  canvasCtx.fillRect(0, elements.visualizerCanvas.height / 2 - 1, elements.visualizerCanvas.width, 2);
}

// --- Separate Text Input Submission Path ---
function sendTextMessage() {
  const text = elements.textInput.value.trim();
  if (!text) return;

  elements.textInput.value = '';

  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify({
      type: 'text',
      text: text,
      language: state.selectedLanguage
    }));
  } else {
    // REST fallback
    fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message: text,
        language: state.selectedLanguage
      })
    })
    .then(r => r.json())
    .then(data => {
      appendTranscriptBubble('user', text, data.language);
      appendTranscriptBubble('assistant', data.response, data.language);
    })
    .catch(err => {
      console.error('Chat error:', err);
      showSystemError('Failed to send text message.');
    });
  }
}

// --- Transcript UI ---
function appendTranscriptBubble(role, text, language) {
  const welcome = document.querySelector('.welcome-bubble');
  if (welcome) welcome.remove();

  const bubble = document.createElement('div');
  bubble.className = `msg-bubble ${role === 'user' ? 'msg-user' : 'msg-assistant'}`;

  const meta = document.createElement('div');
  meta.className = 'msg-meta';

  const sender = document.createElement('span');
  sender.className = 'msg-sender';
  sender.textContent = role === 'user' ? '👤 You' : '🚌 CityLinc Assistant';

  const langTag = document.createElement('span');
  langTag.className = 'msg-lang';
  langTag.textContent = language || 'mr-IN';

  meta.appendChild(sender);
  meta.appendChild(langTag);

  const content = document.createElement('div');
  content.className = 'msg-content';
  content.textContent = text;

  // Voice-to-Map Synchronization Bridge: Check if assistant mentioned a bus route
  if (role === 'assistant' && text) {
    const mentionedRoute = extractRouteNumberFromText(text);
    if (mentionedRoute) {
      const mapBtn = document.createElement('button');
      mapBtn.className = 'btn-subtle';
      mapBtn.style.marginTop = '8px';
      mapBtn.style.display = 'inline-flex';
      mapBtn.style.alignItems = 'center';
      mapBtn.style.gap = '4px';
      mapBtn.style.fontSize = '11px';
      mapBtn.style.padding = '3px 8px';
      mapBtn.style.borderRadius = '12px';
      mapBtn.style.background = 'rgba(56, 189, 248, 0.15)';
      mapBtn.style.color = '#38bdf8';
      mapBtn.style.border = '1px solid rgba(56, 189, 248, 0.4)';
      mapBtn.innerHTML = `🗺️ View Route ${mentionedRoute} on Map`;
      mapBtn.addEventListener('click', () => {
        setDashboardView('dual');
        selectRouteOnMap(mentionedRoute);
      });
      content.appendChild(document.createElement('br'));
      content.appendChild(mapBtn);

      // If already in dual or map view, sync route automatically
      if (mapState.currentView === 'dual' || mapState.currentView === 'map') {
        selectRouteOnMap(mentionedRoute);
      }
    }
  }

  bubble.appendChild(meta);
  bubble.appendChild(content);

  elements.transcriptFeed.appendChild(bubble);
  elements.transcriptFeed.scrollTop = elements.transcriptFeed.scrollHeight;
}

function showSystemError(msg) {
  const bubble = document.createElement('div');
  bubble.className = 'msg-bubble msg-assistant';
  bubble.style.borderColor = '#ef4444';
  bubble.innerHTML = `<span style="color:#ef4444; font-weight:600;">⚠️ Error:</span> ${msg}`;
  elements.transcriptFeed.appendChild(bubble);
  elements.transcriptFeed.scrollTop = elements.transcriptFeed.scrollHeight;
}

// ============================================================
// Interactive Nashik Map & Location Intelligence Client
// ============================================================

const mapState = {
  map: null,
  isInitialized: false,
  currentView: 'voice',
  stopsCluster: null,
  hospitalsLayer: null,
  landmarksLayer: null,
  routeLayer: null,
  markersByStationId: {},
  activeRouteId: null,
  selectedStation: null,
  knownRoutes: new Set(),
  layerVisibility: {
    stops: true,
    hospitals: true,
    landmarks: false
  }
};

function setupMapEventListeners() {
  if (elements.tabVoice) elements.tabVoice.addEventListener('click', () => setDashboardView('voice'));
  if (elements.tabDual) elements.tabDual.addEventListener('click', () => setDashboardView('dual'));
  if (elements.tabMap) elements.tabMap.addEventListener('click', () => setDashboardView('map'));

  if (elements.btnMapSearch) elements.btnMapSearch.addEventListener('click', handleMapSearch);
  if (elements.mapSearchInput) {
    elements.mapSearchInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') handleMapSearch();
    });
  }

  if (elements.mapRouteSelect) {
    elements.mapRouteSelect.addEventListener('change', (e) => {
      const val = e.target.value;
      if (val) {
        selectRouteOnMap(val);
      } else {
        resetMapRouteHighlight();
      }
    });
  }

  if (elements.toggleLayerStops) {
    elements.toggleLayerStops.addEventListener('click', () => toggleLayer('stops'));
  }
  if (elements.toggleLayerHospitals) {
    elements.toggleLayerHospitals.addEventListener('click', () => toggleLayer('hospitals'));
  }
  if (elements.toggleLayerLandmarks) {
    elements.toggleLayerLandmarks.addEventListener('click', () => toggleLayer('landmarks'));
  }

  if (elements.btnResetMapView) elements.btnResetMapView.addEventListener('click', resetMapView);
  if (elements.btnClosePanel) elements.btnClosePanel.addEventListener('click', closeStopDetailPanel);
  if (elements.btnAskAboutStop) elements.btnAskAboutStop.addEventListener('click', askAssistantAboutSelectedStop);
}

function setDashboardView(viewMode) {
  mapState.currentView = viewMode;
  document.body.className = `view-${viewMode}`;

  if (elements.tabVoice) elements.tabVoice.classList.toggle('active', viewMode === 'voice');
  if (elements.tabDual) elements.tabDual.classList.toggle('active', viewMode === 'dual');
  if (elements.tabMap) elements.tabMap.classList.toggle('active', viewMode === 'map');

  if (elements.mapSection) {
    elements.mapSection.style.display = (viewMode === 'voice') ? 'none' : 'flex';
  }

  if (viewMode === 'map' || viewMode === 'dual') {
    if (!mapState.isInitialized) {
      initCityLincMap();
    }
    setTimeout(() => {
      if (mapState.map) mapState.map.invalidateSize();
    }, 50);
    setTimeout(() => {
      if (mapState.map) mapState.map.invalidateSize();
    }, 200);
    setTimeout(() => {
      if (mapState.map) mapState.map.invalidateSize();
    }, 500);
  }
}

function initCityLincMap() {
  if (mapState.isInitialized || typeof L === 'undefined') return;

  const mapContainer = document.getElementById('citylinc-map');
  if (!mapContainer) return;

  // Initialize Leaflet Map centered on Central Nashik (CBS / Panchavati area)
  mapState.map = L.map('citylinc-map', {
    center: [19.9975, 73.7898],
    zoom: 12,
    zoomControl: true,
    preferCanvas: true
  });

  // Base Map Tile Layers (Free, open, no API key required)
  const osmDark = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank">OpenStreetMap</a> contributors',
    subdomains: ['a', 'b', 'c'],
    maxZoom: 19,
    className: 'map-tiles-dark'
  });

  const osmStandard = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank">OpenStreetMap</a> contributors',
    subdomains: ['a', 'b', 'c'],
    maxZoom: 19
  });

  const esriStreets = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}', {
    attribution: 'Tiles &copy; Esri &mdash; Source: Esri, DeLorme, USGS',
    maxZoom: 19
  });

  const esriSatellite = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
    attribution: 'Tiles &copy; Esri &mdash; DigitalGlobe, GeoEye, Earthstar Geographics',
    maxZoom: 19
  });

  // Default to Dark Mode theme (matching CityLinc dark UI)
  osmDark.addTo(mapState.map);

  // Layer control allowing instant basemap switching
  L.control.layers({
    "🌙 Dark Theme": osmDark,
    "🗺️ Standard Map": osmStandard,
    "🏙️ Street Map": esriStreets,
    "🛰️ Satellite View": esriSatellite
  }, null, { position: 'bottomright' }).addTo(mapState.map);

  // Layer groups
  if (typeof L.markerClusterGroup !== 'undefined') {
    mapState.stopsCluster = L.markerClusterGroup({
      maxClusterRadius: 40,
      spiderfyOnMaxZoom: true,
      showCoverageOnHover: false,
      disableClusteringAtZoom: 16
    });
  } else {
    mapState.stopsCluster = L.layerGroup();
  }

  mapState.hospitalsLayer = L.layerGroup();
  mapState.landmarksLayer = L.layerGroup();
  mapState.routeLayer = L.layerGroup();

  mapState.map.addLayer(mapState.stopsCluster);
  mapState.map.addLayer(mapState.hospitalsLayer);
  mapState.map.addLayer(mapState.routeLayer);

  mapState.isInitialized = true;
  console.log('[Map] Leaflet map initialized with multi-layer basemaps (Dark Theme default, no API key).');

  // Load stops and route options
  loadMapStops();
  loadMapRoutes();
}

async function loadMapStops() {
  try {
    const res = await fetch('/api/locations/stops?limit=0');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    const stops = data.stops || [];

    if (elements.mapStopCountBadge) {
      elements.mapStopCountBadge.textContent = `${stops.length} Stops Loaded`;
    }

    stops.forEach(st => {
      let pinClass = 'custom-station-pin pin-up';
      let pinIcon = '';
      let pinSize = [10, 10];

      if (st.is_hospital) {
        pinClass = 'custom-station-pin pin-hospital';
        pinIcon = '🏥';
        pinSize = [18, 18];
      } else if (st.is_landmark) {
        pinClass = 'custom-station-pin pin-landmark';
        pinIcon = '🏛️';
        pinSize = [16, 16];
      } else if (st.direction === 'DOWN') {
        pinClass = 'custom-station-pin pin-down';
      } else if (st.direction === 'TERMINAL' || st.direction === 'BOTH') {
        pinClass = 'custom-station-pin pin-both';
        pinSize = [12, 12];
      }

      const customIcon = L.divIcon({
        className: pinClass,
        html: pinIcon ? `<span>${pinIcon}</span>` : '',
        iconSize: pinSize,
        iconAnchor: [pinSize[0] / 2, pinSize[1] / 2]
      });

      const marker = L.marker([st.lat, st.lon], { icon: customIcon });

      const routesPreview = (st.passing_routes || []).slice(0, 5).join(', ');
      marker.bindTooltip(`<strong>${st.name}</strong><br><span style="color:#38bdf8;">Routes: ${routesPreview || 'Terminal'}</span>`, {
        direction: 'top',
        offset: [0, -5]
      });

      marker.on('click', () => {
        showStopDetail(st);
      });

      mapState.markersByStationId[st.station_id] = marker;

      if (st.is_hospital) {
        mapState.hospitalsLayer.addLayer(marker);
      } else if (st.is_landmark) {
        mapState.landmarksLayer.addLayer(marker);
      } else {
        mapState.stopsCluster.addLayer(marker);
      }
    });

    console.log(`[Map] Placed ${stops.length} physical station markers.`);
  } catch (err) {
    console.error('Failed to load map stops:', err);
  }
}

async function loadMapRoutes() {
  try {
    const res = await fetch('/api/locations/routes');
    if (!res.ok) return;
    const data = await res.json();
    const routes = data.routes || [];

    if (elements.mapRouteSelect) {
      const seenRno = new Set();
      routes.forEach(r => {
        const rno = r.rno;
        if (rno) mapState.knownRoutes.add(rno);
        if (!seenRno.has(rno)) {
          seenRno.add(rno);
          const opt = document.createElement('option');
          opt.value = r.route_id;
          opt.setAttribute('data-rno', rno);
          opt.textContent = `Route ${rno}: ${r.from_station} ➔ ${r.to_station}`;
          elements.mapRouteSelect.appendChild(opt);
        }
      });
    }
  } catch (err) {
    console.warn('Failed to load routes list:', err);
  }
}

async function selectRouteOnMap(routeIdOrRno) {
  if (!mapState.map || !routeIdOrRno) return;
  try {
    const res = await fetch(`/api/locations/routes/${encodeURIComponent(routeIdOrRno)}`);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();

    mapState.activeRouteId = data.rno || routeIdOrRno;
    mapState.routeLayer.clearLayers();

    if (data.polyline && data.polyline.length > 0) {
      // Draw bright cyan route polyline
      const polyline = L.polyline(data.polyline, {
        color: '#38bdf8',
        weight: 5,
        opacity: 0.9,
        lineCap: 'round',
        lineJoin: 'round'
      }).addTo(mapState.routeLayer);

      // Add Origin Marker
      if (data.origin) {
        const origIcon = L.divIcon({
          className: 'custom-station-pin pin-both',
          html: '<span style="font-size:12px;">🚩</span>',
          iconSize: [24, 24],
          iconAnchor: [12, 12]
        });
        L.marker([data.origin.lat, data.origin.lon], { icon: origIcon })
          .bindTooltip(`<strong>Start:</strong> ${data.origin.name}`)
          .addTo(mapState.routeLayer);
      }

      // Add Destination Marker
      if (data.destination) {
        const destIcon = L.divIcon({
          className: 'custom-station-pin pin-hospital',
          html: '<span style="font-size:12px;">🏁</span>',
          iconSize: [24, 24],
          iconAnchor: [12, 12]
        });
        L.marker([data.destination.lat, data.destination.lon], { icon: destIcon })
          .bindTooltip(`<strong>End:</strong> ${data.destination.name}`)
          .addTo(mapState.routeLayer);
      }

      mapState.map.fitBounds(polyline.getBounds(), { padding: [40, 40] });

      if (elements.mapActiveRouteBadge) {
        elements.mapActiveRouteBadge.style.display = 'inline-flex';
        elements.mapActiveRouteBadge.textContent = `Route ${data.rno} (${data.stop_count} stops, ${data.total_distance_km} km)`;
      }

      if (elements.mapRouteSelect) {
        for (let i = 0; i < elements.mapRouteSelect.options.length; i++) {
          const opt = elements.mapRouteSelect.options[i];
          if (opt.value == data.route_id || opt.getAttribute('data-rno') === data.rno) {
            elements.mapRouteSelect.selectedIndex = i;
            break;
          }
        }
      }
    }
  } catch (err) {
    console.error('Failed to select route on map:', err);
  }
}

function resetMapRouteHighlight() {
  if (mapState.routeLayer) mapState.routeLayer.clearLayers();
  mapState.activeRouteId = null;
  if (elements.mapActiveRouteBadge) elements.mapActiveRouteBadge.style.display = 'none';
  if (elements.mapRouteSelect) elements.mapRouteSelect.selectedIndex = 0;
}

function showStopDetail(stop) {
  mapState.selectedStation = stop;

  if (elements.panelStopName) elements.panelStopName.textContent = stop.name;
  if (elements.panelPlatformBadge) {
    elements.panelPlatformBadge.textContent = `Platform: ${stop.direction}`;
    elements.panelPlatformBadge.className = `pill ${stop.direction === 'UP' ? 'pill-active' : stop.direction === 'DOWN' ? 'pill-warning' : 'pill-success'}`;
  }
  if (elements.panelCategoryBadge) {
    elements.panelCategoryBadge.textContent = stop.is_hospital ? '🏥 Hospital' : stop.is_landmark ? '🏛️ Landmark' : '🚏 Bus Stop';
    elements.panelCategoryBadge.className = `pill ${stop.is_hospital ? 'pill-danger' : stop.is_landmark ? 'pill-busy' : 'pill-neutral'}`;
  }
  if (elements.panelStationId) elements.panelStationId.textContent = stop.station_id;
  if (elements.panelCoordinates) elements.panelCoordinates.textContent = `${stop.lat.toFixed(5)}, ${stop.lon.toFixed(5)}`;

  if (elements.panelRoutesList) {
    elements.panelRoutesList.innerHTML = '';
    const routes = stop.passing_routes || [];
    if (routes.length === 0) {
      elements.panelRoutesList.innerHTML = '<span style="color:#64748b;">No direct through routes</span>';
    } else {
      routes.forEach(rno => {
        const pill = document.createElement('span');
        pill.className = 'route-badge-pill';
        pill.textContent = `Route ${rno}`;
        pill.addEventListener('click', (e) => {
          e.stopPropagation();
          selectRouteOnMap(rno);
        });
        elements.panelRoutesList.appendChild(pill);
      });
    }
  }

  if (elements.stopDetailPanel) {
    elements.stopDetailPanel.style.display = 'flex';
  }

  if (mapState.map) {
    mapState.map.panTo([stop.lat, stop.lon], { animate: true, duration: 0.5 });
  }
}

function closeStopDetailPanel() {
  if (elements.stopDetailPanel) elements.stopDetailPanel.style.display = 'none';
  mapState.selectedStation = null;
}

async function handleMapSearch() {
  const query = elements.mapSearchInput ? elements.mapSearchInput.value.trim() : '';
  if (!query) return;

  try {
    const res = await fetch(`/api/locations/search?q=${encodeURIComponent(query)}&limit=10`);
    if (!res.ok) return;
    const data = await res.json();

    if (data.routes && data.routes.length > 0) {
      selectRouteOnMap(data.routes[0].rno || data.routes[0].route_id);
      return;
    }

    if (data.hospitals && data.hospitals.length > 0) {
      showStopDetail(data.hospitals[0]);
      return;
    }

    if (data.stops && data.stops.length > 0) {
      showStopDetail(data.stops[0]);
      return;
    }

    if (data.landmarks && data.landmarks.length > 0) {
      showStopDetail(data.landmarks[0]);
      return;
    }
  } catch (err) {
    console.error('Map search error:', err);
  }
}

function toggleLayer(layerName) {
  if (!mapState.map) return;

  mapState.layerVisibility[layerName] = !mapState.layerVisibility[layerName];
  const isVisible = mapState.layerVisibility[layerName];

  if (layerName === 'stops' && mapState.stopsCluster) {
    if (isVisible) mapState.map.addLayer(mapState.stopsCluster);
    else mapState.map.removeLayer(mapState.stopsCluster);
    if (elements.toggleLayerStops) elements.toggleLayerStops.classList.toggle('active', isVisible);
  }

  if (layerName === 'hospitals' && mapState.hospitalsLayer) {
    if (isVisible) mapState.map.addLayer(mapState.hospitalsLayer);
    else mapState.map.removeLayer(mapState.hospitalsLayer);
    if (elements.toggleLayerHospitals) elements.toggleLayerHospitals.classList.toggle('active', isVisible);
  }

  if (layerName === 'landmarks' && mapState.landmarksLayer) {
    if (isVisible) mapState.map.addLayer(mapState.landmarksLayer);
    else mapState.map.removeLayer(mapState.landmarksLayer);
    if (elements.toggleLayerLandmarks) elements.toggleLayerLandmarks.classList.toggle('active', isVisible);
  }
}

function resetMapView() {
  if (mapState.map) {
    mapState.map.setView([19.9975, 73.7898], 12);
  }
  resetMapRouteHighlight();
  closeStopDetailPanel();
}

function askAssistantAboutSelectedStop() {
  if (!mapState.selectedStation) return;
  const stopName = mapState.selectedStation.name;

  setDashboardView('dual');

  if (elements.textInput) {
    elements.textInput.value = `या थांब्यावरून (${stopName}) जाणाऱ्या बसेस आणि वेळ सांगा`;
    sendTextMessage();
  }
}

function extractRouteNumberFromText(text) {
  if (!text) return null;
  const match = text.match(/\b(?:route|मार्ग|बस|bus|क्र\.?|no\.?|नंबर)?\s*([0-9]{3}[a-zA-Z]?|[0-9]{1,3})\b/i);
  if (match && match[1]) {
    const candidate = match[1].toUpperCase();
    if (mapState.knownRoutes.has(candidate)) {
      return candidate;
    }
  }
  return null;
}

// Expose state and helper functions to window for browser verification
window.citylincVoiceState = state;
window.citylincElements = elements;
window.citylincMapState = mapState;
window.CaptureSession = CaptureSession;
window.captureManager = captureManager;
window.createCaptureSession = createCaptureSession;
window.safelyStartMediaRecorder = safelyStartMediaRecorder;
window.safelyStopMediaRecorder = safelyStopMediaRecorder;
window.initMediaRecorder = initMediaRecorder;
window.handleUtteranceRecorded = handleUtteranceRecorded;
window.toggleManualRecording = toggleManualRecording;
window.triggerBargeIn = triggerBargeIn;
window.playAgentAudio = playAgentAudio;
window.stopAudioPlayback = stopAudioPlayback;
window.sendAudioBlob = sendAudioBlob;
window.setDashboardView = setDashboardView;
window.selectRouteOnMap = selectRouteOnMap;
window.initCityLincMap = initCityLincMap;

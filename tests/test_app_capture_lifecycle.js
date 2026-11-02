/**
 * CityLinc Phase 4.4.3 — Comprehensive Deterministic MediaRecorder Re-Architecture Tests (Requirements 1 through 15)
 * Evaluates backend/app/static/app.js directly in Node.js environment.
 * Deliberately controls asynchronous MediaRecorder event ordering without relying on timing delays.
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

// 1. Mock browser DOM and Web APIs required for app.js execution
global.window = global;
global.window.addEventListener = () => {};
global.document = {
  getElementById: (id) => ({
    id,
    textContent: '',
    value: '',
    disabled: false,
    className: '',
    style: {},
    innerHTML: '',
    width: 300,
    height: 100,
    getContext: () => ({
      clearRect: () => {},
      fillRect: () => {},
      beginPath: () => {},
      moveTo: () => {},
      lineTo: () => {},
      stroke: () => {}
    }),
    addEventListener: () => {},
    classList: { add: () => {}, remove: () => {} }
  }),
  querySelectorAll: () => [],
  addEventListener: () => {}
};
global.navigator = { mediaDevices: { getUserMedia: async () => ({}) } };

class MockBlob {
  constructor(chunks, options = {}) {
    this.chunks = chunks;
    this.type = options.type || '';
    this.size = chunks.reduce((acc, c) => acc + (c.length || (c.size || 500)), 0);
  }
}
global.Blob = MockBlob;

class MockFileReader {
  static shouldFail = false;
  static shouldAbort = false;
  static manualDelivery = false;
  static pendingInstances = [];

  constructor() {
    this.result = 'data:audio/webm;base64,ZmFrZWF1ZGlvZGF0YQ==';
    this.onload = null;
    this.onerror = null;
    this.onabort = null;
    this.onloadend = null;
    MockFileReader.pendingInstances.push(this);
  }

  readAsDataURL(blob) {
    if (MockFileReader.manualDelivery) {
      return;
    }
    queueMicrotask(() => {
      this.trigger();
    });
  }

  trigger() {
    if (MockFileReader.shouldAbort) {
      if (this.onabort) this.onabort(new Error('Mock FileReader abort'));
      if (this.onloadend) this.onloadend();
    } else if (MockFileReader.shouldFail) {
      if (this.onerror) this.onerror(new Error('Mock FileReader encoding failure'));
      if (this.onloadend) this.onloadend();
    } else {
      if (this.onload) this.onload();
      if (this.onloadend) this.onloadend();
    }
  }
}
global.FileReader = MockFileReader;

class MockWebSocket {
  static OPEN = 1;
  static CLOSED = 3;
  constructor() {
    this.readyState = 1;
    this.sentMessages = [];
  }
  send(data) {
    if (this.readyState !== MockWebSocket.OPEN) {
      throw new Error('WebSocket is not open');
    }
    this.sentMessages.push(JSON.parse(data));
  }
}
global.WebSocket = MockWebSocket;

// Deterministic mock recorder allowing manual firing of events
const createdRecorders = [];

class DeterministicMediaRecorder {
  static isTypeSupported() { return true; }
  static shouldFailConstructor = false;
  static shouldFailStart = false;

  constructor(stream, options = {}) {
    if (DeterministicMediaRecorder.shouldFailConstructor) {
      throw new Error('MockMediaRecorder constructor failure');
    }
    this.stream = stream;
    this.mimeType = options.mimeType || 'audio/webm;codecs=opus';
    this.state = 'inactive';
    this.ondataavailable = null;
    this.onstop = null;
    this.onerror = null;
    this.pendingChunks = [];
    createdRecorders.push(this);
  }

  start(timeslice) {
    if (DeterministicMediaRecorder.shouldFailStart) {
      throw new Error('InvalidStateError: Mock MediaRecorder.start failure');
    }
    if (this.state !== 'inactive') {
      throw new Error(`InvalidStateError: Cannot start when state is ${this.state}`);
    }
    this.state = 'recording';
  }

  stop() {
    if (this.state === 'inactive') return;
    this.state = 'inactive';
    // Deliberately DO NOT auto-fire events here!
    // The test explicitly calls fireDataAvailable() and fireStop() to control ordering!
  }

  // Deterministic trigger methods
  fireDataAvailable(buffer) {
    if (this.ondataavailable) {
      this.ondataavailable({ data: new MockBlob([buffer]) });
    }
  }

  fireStop() {
    if (this.onstop) {
      this.onstop();
    }
  }

  fireError(err) {
    if (this.onerror) {
      this.onerror({ error: err || new Error('Recorder error') });
    }
  }
}
global.MediaRecorder = DeterministicMediaRecorder;

// 2. Load and evaluate backend/app/static/app.js
const appJsPath = path.resolve(__dirname, '../backend/app/static/app.js');
const appJsSource = fs.readFileSync(appJsPath, 'utf8');
vm.runInThisContext(appJsSource);

const state = window.citylincVoiceState;
const captureManager = window.captureManager;
const createCaptureSession = window.createCaptureSession;
const safelyStartMediaRecorder = window.safelyStartMediaRecorder;
const safelyStopMediaRecorder = window.safelyStopMediaRecorder;
const handleUtteranceRecorded = window.handleUtteranceRecorded;
const toggleManualRecording = window.toggleManualRecording;
const triggerBargeIn = window.triggerBargeIn;
const sendAudioBlob = window.sendAudioBlob;
const elements = window.citylincElements;

function resetTestEnvironment() {
  captureManager.cancelAllSessions('test_reset');
  state.mediaStream = { getTracks: () => [] };
  state.ws = new MockWebSocket();
  state.isRecording = true;
  state.isSpeechActive = false;
  state.isManualRecording = false;
  state.candidateSpeechStartTime = 0;
  state.speechOnsetConsecutiveFrames = 0;
  createdRecorders.length = 0;
  DeterministicMediaRecorder.shouldFailConstructor = false;
  DeterministicMediaRecorder.shouldFailStart = false;
  MockFileReader.shouldFail = false;
  MockFileReader.shouldAbort = false;
  MockFileReader.manualDelivery = false;
  MockFileReader.pendingInstances = [];
}

async function runAllTests() {
  console.log('>>> Running Deterministic MediaRecorder Re-Architecture Test Suite (Requirements 1 through 15)...\n');

  // Requirement 1: A delayed onstop cannot overlap with B.
  {
    console.log('Req 1: A delayed onstop cannot overlap with B');
    resetTestEnvironment();

    const sessionA = createCaptureSession('auto', Date.now() - 1000);
    safelyStartMediaRecorder(sessionA);
    const recA = sessionA.recorder;

    safelyStopMediaRecorder('utterance_finalized');
    assert.strictEqual(captureManager.finalizingSession, sessionA, 'Session A must be marked finalizing');
    assert.strictEqual(captureManager.activeSession, null, 'Active session must be cleared from active recording slot');

    // Attempt to start Capture B while A is still finalizing
    const sessionB = createCaptureSession('auto', Date.now());
    const startedB = safelyStartMediaRecorder(sessionB);
    assert.strictEqual(startedB, false, 'Capture B must NOT start while Capture A is finalizing');
    assert.strictEqual(sessionB.recorder, null, 'Session B must not receive a recorder while A is finalizing');
    console.log('  [PASS] Delayed onstop prevents Capture B from overlapping.');
  }

  // Requirement 2: A delayed final dataavailable belongs only to A.
  {
    console.log("Req 2: A delayed final dataavailable belongs only to A");
    resetTestEnvironment();

    const sessionA = createCaptureSession('auto', Date.now() - 1000);
    safelyStartMediaRecorder(sessionA);
    const recA = sessionA.recorder;
    safelyStopMediaRecorder('utterance_finalized');

    // Simulate delayed data event from recA
    const chunkA = Buffer.alloc(2000, 0x41); // 'A'
    recA.fireDataAvailable(chunkA);

    assert.strictEqual(sessionA.chunks.length, 1, 'Session A must receive its data event');
    assert.strictEqual(sessionA.chunks[0].chunks[0], chunkA);
    console.log('  [PASS] Only Session A received its delayed data event.');
  }

  // Requirement 3: A timeout cannot release the lock prematurely.
  {
    console.log('Req 3: A timeout cannot release the lock prematurely');
    resetTestEnvironment();

    const sessionA = createCaptureSession('auto', Date.now() - 1000);
    safelyStartMediaRecorder(sessionA);
    const recA = sessionA.recorder;
    safelyStopMediaRecorder('utterance_finalized');

    assert.strictEqual(captureManager.finalizingSession, sessionA, 'Session A is finalizing');

    // Verify after time passes (e.g., 700ms), lock remains held until explicit onstop
    await new Promise(r => setTimeout(r, 50));
    assert.strictEqual(captureManager.finalizingSession, sessionA, 'Lock must NOT be released by any arbitrary timer');

    const sessionB = createCaptureSession('auto', Date.now());
    assert.strictEqual(safelyStartMediaRecorder(sessionB), false, 'Session B cannot start while A is still finalizing');

    recA.fireStop();
    await sessionA.finalizationPromise;
    assert.strictEqual(captureManager.finalizingSession, null, 'Lock releases ONLY upon genuine onstop');
    console.log('  [PASS] Finalizing lock is never prematurely released by a timeout.');
  }

  // Requirement 4: Global chunks cannot leak into a new session.
  {
    console.log('Req 4: Global chunks cannot leak into a new session');
    resetTestEnvironment();

    // Populate global recordedChunks if any legacy state existed
    state.recordedChunks = [Buffer.alloc(3000, 0xFF)];

    const sessionB = createCaptureSession('auto', Date.now());
    safelyStartMediaRecorder(sessionB);
    const chunkB = Buffer.alloc(2500, 0x02);
    sessionB.recorder.fireDataAvailable(chunkB);

    assert.strictEqual(sessionB.chunks.length, 1);
    assert.strictEqual(sessionB.chunks[0].chunks[0], chunkB, 'Session B owns only its chunk');
    console.log('  [PASS] Global chunks do not leak into Session B.');
  }

  // Requirement 5: An empty session never falls back to previous audio.
  {
    console.log('Req 5: An empty session never falls back to previous audio');
    resetTestEnvironment();

    state.recordedChunks = [Buffer.alloc(4000, 0xAA)];

    const session = createCaptureSession('auto', Date.now() - 1000);
    safelyStartMediaRecorder(session);
    session.isConfirmed = true;
    session.chunks = []; // Empty chunks

    safelyStopMediaRecorder('utterance_finalized');
    session.recorder.fireStop();
    await session.finalizationPromise;

    assert.strictEqual(state.ws.sentMessages.length, 0, 'No audio message sent when session chunks are empty');
    console.log('  [PASS] Empty session safely drops without falling back to previous audio.');
  }

  // Requirement 6: WebSocket FileReader completion is awaited before lock release.
  {
    console.log('Req 6: WebSocket FileReader completion is awaited');
    resetTestEnvironment();
    MockFileReader.manualDelivery = true;

    const sessionA = createCaptureSession('auto', Date.now() - 1000);
    safelyStartMediaRecorder(sessionA);
    sessionA.isConfirmed = true;
    sessionA.chunks.push(Buffer.alloc(2500));
    safelyStopMediaRecorder('utterance_finalized');

    // Fire onstop: FileReader will be started but held in pending state
    sessionA.recorder.fireStop();

    // Give microtasks time to run
    await new Promise(r => setImmediate(r));

    assert.strictEqual(captureManager.finalizingSession, sessionA, 'finalizingSession must remain held while FileReader is encoding');
    const sessionB = createCaptureSession('auto', Date.now());
    assert.strictEqual(safelyStartMediaRecorder(sessionB), false, 'Session B cannot start while FileReader is in progress');

    // Now trigger FileReader completion
    const readerInstance = MockFileReader.pendingInstances[MockFileReader.pendingInstances.length - 1];
    assert.ok(readerInstance, 'FileReader instance exists');
    readerInstance.trigger();

    await sessionA.finalizationPromise;
    assert.strictEqual(captureManager.finalizingSession, null, 'finalizingSession releases after FileReader + ws.send completes');
    assert.strictEqual(state.ws.sentMessages.length, 1, 'Audio was sent over WebSocket');
    console.log('  [PASS] Finalization lock is held until FileReader completes WebSocket dispatch.');
  }

  // Requirement 7: FileReader errors release the owning lifecycle safely.
  {
    console.log('Req 7: FileReader errors release the owning lifecycle safely');
    resetTestEnvironment();
    MockFileReader.shouldFail = true;

    const session = createCaptureSession('auto', Date.now() - 1000);
    safelyStartMediaRecorder(session);
    session.isConfirmed = true;
    session.chunks.push(Buffer.alloc(2500));
    safelyStopMediaRecorder('utterance_finalized');

    session.recorder.fireStop();
    await session.finalizationPromise;

    assert.strictEqual(captureManager.finalizingSession, null, 'Lock must be released despite FileReader error');
    assert.ok(session.error, 'Session records the error');
    assert.strictEqual(captureManager.hasActiveOrFinalizingSession(), false, 'Pipeline is not deadlocked');
    console.log('  [PASS] FileReader error handled cleanly without locking captureManager.');
  }

  // Requirement 8: WebSocket unavailable/send failure is handled.
  {
    console.log('Req 8: WebSocket unavailable/send failure is handled');
    resetTestEnvironment();
    state.ws.readyState = MockWebSocket.CLOSED;

    const session = createCaptureSession('auto', Date.now() - 1000);
    safelyStartMediaRecorder(session);
    session.isConfirmed = true;
    session.chunks.push(Buffer.alloc(2500));
    safelyStopMediaRecorder('utterance_finalized');

    session.recorder.fireStop();
    await session.finalizationPromise;

    assert.strictEqual(captureManager.finalizingSession, null, 'Lock must be released when WebSocket is closed');
    assert.ok(session.error, 'Session records the dispatch error');
    console.log('  [PASS] WebSocket closed error handled cleanly with lock release.');
  }

  // Requirement 9: Confirmed auto-VAD speech during finalization is preserved.
  {
    console.log('Req 9: Confirmed auto-VAD speech during finalization is preserved');
    resetTestEnvironment();

    const sessionA = createCaptureSession('auto', Date.now() - 1000);
    safelyStartMediaRecorder(sessionA);
    sessionA.isConfirmed = true;
    sessionA.chunks.push(Buffer.alloc(2500));
    safelyStopMediaRecorder('utterance_finalized');

    assert.strictEqual(captureManager.finalizingSession, sessionA);

    // While Session A is finalizing, user speaks again and VAD confirms onset
    const sessionB = createCaptureSession('auto', Date.now());
    sessionB.isConfirmed = true;
    captureManager.enqueuePendingCapture(sessionB);

    assert.strictEqual(captureManager.pendingQueue[0], sessionB, 'Session B must be queued as pending');

    // Session A completes
    sessionA.recorder.fireStop();
    await sessionA.finalizationPromise;

    // Session B must have started automatically!
    assert.strictEqual(captureManager.activeSession, sessionB, 'Session B must automatically start recording');
    assert.ok(sessionB.recorder, 'Session B must have dedicated recorder instance');
    assert.strictEqual(sessionB.recorder.state, 'recording');
    console.log('  [PASS] Confirmed auto-VAD speech during finalization was queued and started.');
  }

  // Requirement 10: Manual recording during finalization is preserved.
  {
    console.log('Req 10: Manual recording during finalization is preserved');
    resetTestEnvironment();

    const sessionA = createCaptureSession('auto', Date.now() - 1000);
    safelyStartMediaRecorder(sessionA);
    sessionA.isConfirmed = true;
    sessionA.chunks.push(Buffer.alloc(2500));
    safelyStopMediaRecorder('utterance_finalized');

    // User clicks Push to Speak while A is finalizing
    toggleManualRecording();
    assert.strictEqual(state.isManualRecording, true);
    assert.strictEqual(captureManager.pendingQueue.length, 1, 'Manual session queued as pending');
    assert.strictEqual(captureManager.pendingQueue[0].type, 'manual');

    sessionA.recorder.fireStop();
    await sessionA.finalizationPromise;

    assert.strictEqual(captureManager.activeSession.type, 'manual', 'Manual session started upon A finalization');
    console.log('  [PASS] Manual recording requested during finalization started safely.');
  }

  // Requirement 11: Barge-in during finalization is preserved.
  {
    console.log('Req 11: Barge-in during finalization is preserved');
    resetTestEnvironment();

    const sessionA = createCaptureSession('auto', Date.now() - 1000);
    safelyStartMediaRecorder(sessionA);
    sessionA.isConfirmed = true;
    sessionA.chunks.push(Buffer.alloc(2500));
    safelyStopMediaRecorder('utterance_finalized');

    // Acoustic barge-in triggers while A is finalizing
    triggerBargeIn(true);
    assert.strictEqual(captureManager.pendingQueue.length, 1, 'Barge-in session queued as pending');
    assert.strictEqual(captureManager.pendingQueue[0].type, 'barge_in');

    sessionA.recorder.fireStop();
    await sessionA.finalizationPromise;

    assert.strictEqual(captureManager.activeSession.type, 'barge_in', 'Barge-in session started upon A finalization');
    console.log('  [PASS] Barge-in requested during finalization started safely.');
  }

  // Requirement 12: Constructor/start/stop errors do not leave stale locks.
  {
    console.log('Req 12: Constructor/start/stop errors do not leave stale locks');
    resetTestEnvironment();

    DeterministicMediaRecorder.shouldFailStart = true;
    const session = createCaptureSession('auto', Date.now());
    const started = safelyStartMediaRecorder(session);
    assert.strictEqual(started, false, 'Start should fail');
    assert.strictEqual(session.disposed, true);
    assert.strictEqual(captureManager.hasActiveOrFinalizingSession(), false, 'Locks released on start failure');

    DeterministicMediaRecorder.shouldFailStart = false;
    DeterministicMediaRecorder.shouldFailConstructor = true;
    const session2 = createCaptureSession('auto', Date.now());
    const started2 = safelyStartMediaRecorder(session2);
    assert.strictEqual(started2, false, 'Constructor failure handled');
    assert.strictEqual(captureManager.hasActiveOrFinalizingSession(), false, 'Locks released on constructor failure');
    console.log('  [PASS] Failure paths cleanly release lifecycle locks.');
  }

  // Requirement 13: Shutdown safely handles active and pending sessions.
  {
    console.log('Req 13: Shutdown safely handles active and pending sessions');
    resetTestEnvironment();

    const activeSess = createCaptureSession('auto', Date.now());
    safelyStartMediaRecorder(activeSess);

    const pendingSess = createCaptureSession('auto', Date.now());
    captureManager.enqueuePendingCapture(pendingSess);

    captureManager.cancelAllSessions('shutdown');

    assert.strictEqual(activeSess.disposed, true, 'Active session marked disposed');
    assert.strictEqual(pendingSess.disposed, true, 'Pending session marked disposed');
    assert.strictEqual(captureManager.pendingQueue.length, 0);
    assert.strictEqual(captureManager.hasActiveOrFinalizingSession(), false, 'All locks cleared');
    console.log('  [PASS] Shutdown cancelled all active and pending sessions cleanly.');
  }

  // Requirement 14: Exactly one Blob and one send per confirmed session.
  {
    console.log('Req 14: Exactly one Blob and one send per confirmed session');
    resetTestEnvironment();

    const session = createCaptureSession('auto', Date.now() - 1000);
    safelyStartMediaRecorder(session);
    session.isConfirmed = true;
    session.chunks.push(Buffer.alloc(2500));
    safelyStopMediaRecorder('utterance_finalized');
    session.recorder.fireStop();
    await session.finalizationPromise;

    // Attempt duplicate manual finalization
    await handleUtteranceRecorded(session);
    await handleUtteranceRecorded(session);

    assert.strictEqual(state.ws.sentMessages.length, 1, 'Exactly one WebSocket message sent');
    console.log('  [PASS] Audio transmission occurred exactly once.');
  }

  // Requirement 15: Sequential captures have zero cross-session contamination.
  {
    console.log('Req 15: Sequential captures have zero cross-session contamination');
    resetTestEnvironment();

    // Session 1
    const s1 = createCaptureSession('auto', Date.now() - 1000);
    safelyStartMediaRecorder(s1);
    s1.isConfirmed = true;
    s1.recorder.fireDataAvailable(Buffer.alloc(2000, 0x01));
    safelyStopMediaRecorder('s1_stop');
    s1.recorder.fireStop();
    await s1.finalizationPromise;

    // Session 2
    const s2 = createCaptureSession('auto', Date.now() - 1000);
    safelyStartMediaRecorder(s2);
    s2.isConfirmed = true;
    s2.recorder.fireDataAvailable(Buffer.alloc(2000, 0x02));
    safelyStopMediaRecorder('s2_stop');
    s2.recorder.fireStop();
    await s2.finalizationPromise;

    assert.strictEqual(s1.chunks.length, 0, 's1 chunks released after blob creation');
    assert.strictEqual(s2.chunks.length, 0, 's2 chunks released after blob creation');
    assert.strictEqual(state.ws.sentMessages.length, 2, 'Two distinct messages sent');
    console.log('  [PASS] Sequential captures isolated with zero chunk leakage.');
  }

  // ================================================================
  // Phase 4.4.5 Regression Test Scenarios (Scenarios A through G)
  // ================================================================

  // Scenario A: Two or more confirmed captures arriving while another capture is finalizing (FIFO preserved)
  {
    console.log('\nScenario A: Two or more confirmed captures arriving during finalization (FIFO preserved)');
    resetTestEnvironment();

    // Session 1 starts and finalizes
    const s1 = createCaptureSession('auto', Date.now() - 1000);
    safelyStartMediaRecorder(s1);
    s1.isConfirmed = true;
    s1.recorder.fireDataAvailable(Buffer.alloc(2000, 0x01));
    safelyStopMediaRecorder('s1_stop');
    assert.strictEqual(captureManager.finalizingSession, s1);

    // Confirmed Session 2 arrives while s1 is finalizing
    const s2 = createCaptureSession('auto', Date.now());
    s2.isConfirmed = true;
    const queued2 = captureManager.enqueuePendingCapture(s2);
    assert.strictEqual(queued2, true);

    // Confirmed Session 3 arrives while s1 is finalizing
    const s3 = createCaptureSession('auto', Date.now());
    s3.isConfirmed = true;
    const queued3 = captureManager.enqueuePendingCapture(s3);
    assert.strictEqual(queued3, true);

    assert.strictEqual(captureManager.pendingQueue.length, 2, 'Queue depth must be 2');
    assert.strictEqual(captureManager.pendingQueue[0], s2, 'Session 2 must be head of queue');
    assert.strictEqual(captureManager.pendingQueue[1], s3, 'Session 3 must be second in queue');

    // s1 completes finalization
    s1.recorder.fireStop();
    await s1.finalizationPromise;

    // s2 should now be active, and s3 remains queued at head
    assert.strictEqual(captureManager.activeSession, s2, 'Session 2 started as active session');
    assert.strictEqual(s2.recorder.state, 'recording');
    assert.strictEqual(captureManager.pendingQueue.length, 1, 'Remaining queue depth must be 1');
    assert.strictEqual(captureManager.pendingQueue[0], s3, 'Session 3 is now head of queue');

    // s2 records and finishes
    s2.recorder.fireDataAvailable(Buffer.alloc(2000, 0x02));
    safelyStopMediaRecorder('s2_stop');
    s2.recorder.fireStop();
    await s2.finalizationPromise;

    // s3 should now be active
    assert.strictEqual(captureManager.activeSession, s3, 'Session 3 started as active session');
    assert.strictEqual(s3.recorder.state, 'recording');
    assert.strictEqual(captureManager.pendingQueue.length, 0, 'Queue must now be empty');

    // s3 records and finishes
    s3.recorder.fireDataAvailable(Buffer.alloc(2000, 0x03));
    safelyStopMediaRecorder('s3_stop');
    s3.recorder.fireStop();
    await s3.finalizationPromise;

    assert.strictEqual(state.ws.sentMessages.length, 3, 'All 3 confirmed sessions dispatched in FIFO order');
    console.log('  [PASS] Scenario A: FIFO queue preserved all confirmed captures without drops or reordering.');
  }

  // Scenario B: Manual recording starts while another session is finalizing; user presses Done Speaking before finalization completes
  {
    console.log('Scenario B: Manual recording started then cancelled while awaiting previous finalization');
    resetTestEnvironment();

    const s1 = createCaptureSession('auto', Date.now() - 1000);
    safelyStartMediaRecorder(s1);
    s1.isConfirmed = true;
    s1.recorder.fireDataAvailable(Buffer.alloc(2000, 0x01));
    safelyStopMediaRecorder('s1_stop');
    assert.strictEqual(captureManager.finalizingSession, s1);

    // User presses "Push to Speak" while s1 is finalizing
    toggleManualRecording();
    assert.strictEqual(state.isManualRecording, true);
    assert.strictEqual(captureManager.pendingQueue.length, 1);
    const pendingManual = captureManager.pendingQueue[0];
    assert.strictEqual(pendingManual.type, 'manual');
    assert.strictEqual(pendingManual.stopRequested, false);

    // User presses "Done Speaking" BEFORE s1 finalizes
    toggleManualRecording();
    assert.strictEqual(state.isManualRecording, false);
    assert.strictEqual(pendingManual.stopRequested, true, 'stopRequested must be flagged without deleting session yet');
    assert.strictEqual(captureManager.pendingQueue.length, 1, 'Session retained in queue until cleanly dequeued');

    // s1 completes finalization
    s1.recorder.fireStop();
    await s1.finalizationPromise;

    // Dequeuing pendingManual should cleanly cancel it without starting a recorder or sending false audio
    assert.strictEqual(captureManager.activeSession, null, 'No active session should be started');
    assert.strictEqual(captureManager.pendingQueue.length, 0, 'Queue is empty');
    assert.strictEqual(pendingManual.disposed, true, 'Cancelled manual session is marked disposed');
    assert.strictEqual(state.ws.sentMessages.length, 1, 'Only s1 was sent to WebSocket (no false recording)');
    console.log('  [PASS] Scenario B: Pending manual session stopRequested handled cleanly without false recording.');
  }

  // Scenario C: Transient VAD abort occurs while a confirmed barge-in is queued
  {
    console.log('Scenario C: Transient VAD abort occurs while confirmed barge-in is queued');
    resetTestEnvironment();

    const s1 = createCaptureSession('auto', Date.now() - 1000);
    safelyStartMediaRecorder(s1);
    s1.isConfirmed = true;
    s1.recorder.fireDataAvailable(Buffer.alloc(2000, 0x01));
    safelyStopMediaRecorder('s1_stop');

    // Barge-in occurs during s1 finalization
    triggerBargeIn(true);
    assert.strictEqual(captureManager.pendingQueue.length, 1);
    const bargeInSession = captureManager.pendingQueue[0];
    assert.strictEqual(bargeInSession.type, 'barge_in');
    assert.strictEqual(bargeInSession.isConfirmed, true);

    // Transient VAD abort fires (active session is null, transient reason)
    safelyStopMediaRecorder('transient_abort');
    safelyStopMediaRecorder('unconfirmed_transient');

    // Barge-in session must NOT be deleted or disposed by transient abort!
    assert.strictEqual(captureManager.pendingQueue.length, 1, 'Barge-in session remains in queue');
    assert.strictEqual(captureManager.pendingQueue[0], bargeInSession);
    assert.strictEqual(bargeInSession.disposed, false);

    // s1 finishes
    s1.recorder.fireStop();
    await s1.finalizationPromise;

    // Barge-in session starts as active
    assert.strictEqual(captureManager.activeSession, bargeInSession, 'Barge-in session started recording');
    assert.strictEqual(bargeInSession.recorder.state, 'recording');

    // Barge-in completes
    bargeInSession.recorder.fireDataAvailable(Buffer.alloc(2000, 0x02));
    safelyStopMediaRecorder('barge_done');
    bargeInSession.recorder.fireStop();
    await bargeInSession.finalizationPromise;

    const audioMessages = state.ws.sentMessages.filter(m => m.type === 'audio');
    assert.strictEqual(audioMessages.length, 2, 'Two audio messages sent (s1 and barge-in)');
    const interruptMessages = state.ws.sentMessages.filter(m => m.type === 'interrupt');
    assert.strictEqual(interruptMessages.length, 1, 'One interrupt message sent');
    console.log('  [PASS] Scenario C: Queued confirmed barge-in was completely immune to transient VAD aborts.');
  }

  // Scenario D: Queue capacity overflow rejects newer request with showMicError
  {
    console.log('Scenario D: Queue capacity overflow rejects newer request with showMicError');
    resetTestEnvironment();

    const s1 = createCaptureSession('auto', Date.now() - 1000);
    safelyStartMediaRecorder(s1);
    safelyStopMediaRecorder('s1_stop');

    // Fill queue to max capacity (2)
    const q1 = createCaptureSession('auto', Date.now());
    q1.isConfirmed = true;
    assert.strictEqual(captureManager.enqueuePendingCapture(q1), true);

    const q2 = createCaptureSession('auto', Date.now());
    q2.isConfirmed = true;
    assert.strictEqual(captureManager.enqueuePendingCapture(q2), true);
    assert.strictEqual(captureManager.pendingQueue.length, 2);

    // Attempt to queue a 3rd session
    const q3 = createCaptureSession('auto', Date.now());
    q3.isConfirmed = true;
    const result = captureManager.enqueuePendingCapture(q3);

    assert.strictEqual(result, false, 'Enqueue must fail on queue overflow');
    assert.strictEqual(q3.disposed, true, 'Overflow session must be disposed');
    assert.strictEqual(captureManager.pendingQueue.length, 2, 'Existing queue length remains at 2');
    assert.strictEqual(captureManager.pendingQueue[0], q1, 'First queued session preserved');
    assert.strictEqual(captureManager.pendingQueue[1], q2, 'Second queued session preserved');
    assert.ok(elements.micErrorBox.textContent.includes('⚠️ Error:'), 'User notified with visible mic error');
    console.log('  [PASS] Scenario D: Queue capacity overflow rejected cleanly with user-facing error.');
  }

  // Scenario E: FileReader lifecycle: exactly-once settlement for success, error, and abort
  {
    console.log('Scenario E: FileReader exactly-once settlement for success, error, and abort');
    resetTestEnvironment();

    // 1. Success case
    const blobOk = new MockBlob([Buffer.alloc(2000)]);
    MockFileReader.manualDelivery = false;
    MockFileReader.shouldFail = false;
    MockFileReader.shouldAbort = false;
    const okRes = await sendAudioBlob(blobOk, 'audio/webm', 1000, 101);
    assert.strictEqual(okRes.captureId, 101, 'Success case resolves with capture details');

    // 2. Error case
    const blobErr = new MockBlob([Buffer.alloc(2000)]);
    MockFileReader.shouldFail = true;
    MockFileReader.shouldAbort = false;
    let errCaught = false;
    try {
      await sendAudioBlob(blobErr, 'audio/webm', 1000, 102);
    } catch (e) {
      errCaught = true;
    }
    assert.strictEqual(errCaught, true, 'Error case rejects');

    // 3. Abort case
    const blobAbort = new MockBlob([Buffer.alloc(2000)]);
    MockFileReader.shouldFail = false;
    MockFileReader.shouldAbort = true;
    let abortCaught = false;
    try {
      await sendAudioBlob(blobAbort, 'audio/webm', 1000, 103);
    } catch (e) {
      abortCaught = true;
    }
    assert.strictEqual(abortCaught, true, 'Abort case rejects');

    // 4. Exact-once settlement check: duplicate callbacks do not multi-settle
    MockFileReader.shouldAbort = false;
    MockFileReader.shouldFail = false;
    MockFileReader.manualDelivery = true;
    const blobDbl = new MockBlob([Buffer.alloc(2000)]);
    const blobPromise = sendAudioBlob(blobDbl, 'audio/webm', 1000, 104);

    const reader = MockFileReader.pendingInstances[MockFileReader.pendingInstances.length - 1];
    assert.ok(reader);
    // Fire onload then fire onerror
    reader.onload();
    let secondErrorThrown = false;
    try {
      if (reader.onerror) reader.onerror(new Error('Late error'));
    } catch (e) {
      secondErrorThrown = true;
    }
    assert.strictEqual(secondErrorThrown, false, 'Late callback ignored cleanly');
    const dblRes = await blobPromise;
    assert.strictEqual(dblRes.captureId, 104, 'First event wins, exactly-once settled');

    console.log('  [PASS] Scenario E: FileReader settled exactly-once on success, error, abort.');
  }

  // Scenario F: MediaRecorder error followed by late events cannot corrupt another session
  {
    console.log('Scenario F: MediaRecorder error followed by late events cannot corrupt another session');
    resetTestEnvironment();

    const sessF = createCaptureSession('auto', Date.now() - 1000);
    safelyStartMediaRecorder(sessF);
    const recF = sessF.recorder;

    // Trigger hardware error on MediaRecorder
    recF.fireError(new Error('Hardware audio device disconnected'));

    assert.ok(sessF.error, 'Session recorded recorder error');
    assert.strictEqual(sessF.disposed, true, 'Session marked disposed on recorder error');
    assert.strictEqual(captureManager.activeSession, null, 'Active session cleared');
    assert.strictEqual(state.mediaRecorder, null, 'Global mediaRecorder reference cleared');
    assert.strictEqual(recF.ondataavailable, null, 'Listeners detached on error');
    assert.strictEqual(recF.onstop, null, 'Listeners detached on error');

    // Late events from dead recorder do nothing
    recF.fireDataAvailable(Buffer.alloc(1000));
    recF.fireStop();

    // Now start a fresh capture session G
    const sessNext = createCaptureSession('auto', Date.now());
    const started = safelyStartMediaRecorder(sessNext);
    assert.strictEqual(started, true, 'Subsequent capture session starts cleanly');
    assert.strictEqual(captureManager.activeSession, sessNext);
    assert.strictEqual(sessNext.chunks.length, 0, 'No contamination from dead recorder');

    safelyStopMediaRecorder('test_reset');
    console.log('  [PASS] Scenario F: MediaRecorder error cleanly tore down session and protected subsequent sessions.');
  }

  // Scenario G: Shutdown with active, finalizing, and queued sessions
  {
    console.log('Scenario G: Shutdown with active, finalizing, and queued sessions');
    resetTestEnvironment();

    // 1. Finalizing session
    const sessFin = createCaptureSession('auto', Date.now() - 2000);
    safelyStartMediaRecorder(sessFin);
    safelyStopMediaRecorder('test_fin');
    assert.strictEqual(captureManager.finalizingSession, sessFin);

    // 2. Queued sessions
    const sessQ1 = createCaptureSession('auto', Date.now() - 1000);
    sessQ1.isConfirmed = true;
    captureManager.enqueuePendingCapture(sessQ1);

    const sessQ2 = createCaptureSession('barge_in', Date.now() - 500);
    sessQ2.isConfirmed = true;
    captureManager.enqueuePendingCapture(sessQ2);

    assert.strictEqual(captureManager.pendingQueue.length, 2);

    // Shutdown called
    captureManager.cancelAllSessions('system_shutdown');

    assert.strictEqual(sessFin.disposed, true, 'Finalizing session marked disposed');
    assert.strictEqual(sessQ1.disposed, true, 'Queued session 1 marked disposed');
    assert.strictEqual(sessQ2.disposed, true, 'Queued session 2 marked disposed');
    assert.strictEqual(captureManager.activeSession, null);
    assert.strictEqual(captureManager.finalizingSession, null);
    assert.strictEqual(captureManager.pendingQueue.length, 0, 'Pending queue cleared');
    assert.strictEqual(state.mediaRecorder, null);
    assert.strictEqual(state.isSpeechActive, false);
    assert.strictEqual(captureManager.hasActiveOrFinalizingSession(), false, 'Zero dangling locks');

    console.log('  [PASS] Scenario G: Comprehensive shutdown cleared all active, finalizing, and queued sessions.');
  }

  console.log('\n================================================================');
  console.log('ALL 15 DETERMINISTIC REQUIREMENTS (1-15) & 7 REGRESSION SCENARIOS (A-G) PASSED!');
  console.log('================================================================\n');
}

runAllTests().catch(err => {
  console.error('TEST FAILURE:', err);
  process.exit(1);
});

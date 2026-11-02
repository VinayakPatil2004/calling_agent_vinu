# CityLinc Multilingual Real-Time Voice Agent

A real-time, browser-based voice agent for the **CityLinc Nashik Bus Service**, supporting **Marathi (`mr-IN`)**, **Hindi (`hi-IN`)**, and **English (`en-IN`)**.

Powered by:
- **Sarvam AI STT**: `saaras:v4` Automatic Speech Recognition for Indic languages.
- **Sarvam AI TTS**: `bulbul:v3` Natural Voice Synthesis with expressive prosody.
- **FastAPI**: Asynchronous REST & WebSocket backend.
- **CityLinc Knowledge Engine**: Ground-truth timetables, stops, route mappings, and fare pass rules for Nashik.
- **Barge-in / Interruption Engine**: Instant voice activity detection allowing commuters to interrupt the assistant at any time.

---

## 🚀 Quick Start

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure Environment Variables
Copy `.env.example` to `backend/.env`:
```env
SARVAM_API_KEY="your-sarvam-api-key"
SARVAM_STT_MODEL="saaras:v4"
SARVAM_TTS_MODEL="bulbul:v3"
DEFAULT_LANGUAGE="mr-IN"
HOST="0.0.0.0"
PORT=8000
```
> *Note: If `SARVAM_API_KEY` is not yet configured, the system operates in test/simulation mode with local transit intelligence and synthetic audio cues.*

### 3. Launch the Voice Agent Server
From the workspace root directory:
```bash
python -m uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 --reload
```

### 4. Access the Browser Interface
Open your browser and navigate to:
```
http://localhost:8000/
```

---

## 🎙️ Core Features

1. **Multilingual Speech-to-Text (STT)**:
   - Uses Sarvam AI's `saaras:v4` model.
   - Automatically detects spoken language or honors manual selection (`mr-IN`, `hi-IN`, `en-IN`).
2. **Text-to-Speech (TTS)**:
   - Uses Sarvam AI's `bulbul:v3` model.
   - Generates natural, high-fidelity speech in Marathi, Hindi, and English.
3. **Barge-In / Interruption Support**:
   - Commuters can speak at any time while the agent is talking.
   - Client and server immediately halt ongoing playback, drop stale speech chunks, and transition seamlessly to listening.
4. **Context & Language Switching**:
   - Commuters can switch between Marathi, Hindi, and English mid-conversation.
   - The assistant adapts its response language to match the commuter's language while preserving conversation history.
5. **CityLinc Ground Truth Intelligence**:
   - Real data indexed from `data/raw/timetables/citylinc_timetables.json`, `data/raw/fare/bus_passes.json`, and `data/raw/timetables/bus_stop.json`.
   - Accurate bus pass rates:
     - 1-Day Open-Ended Pass: ₹110
     - 7-Day Open-Ended Pass: ₹530
     - 30-Day Open-Ended Pass: ₹2,100
     - 90-Day Open-Ended Pass: ₹5,250
     - 180-Day Open-Ended Pass: ₹9,450
     - Student Passes: 50% concession for 30 days, 66.60% for 90 days.
   - Bus schedules and routes for major corridors (Routes 101, 104, 145, Trimbakeshwar, Gangapur, CBS, etc.).

---

## 🧪 Running Automated Tests

Run the test suite with:
```bash
python -m pytest tests/test_voice_agent.py -v
```
All tests verify:
- Knowledge base loading and indexing.
- Multilingual response accuracy.
- Sarvam STT & TTS clients.
- Barge-in and interruption handling.
- FastAPI REST endpoints & WebSocket voice communication.

---

## 📁 Project Structure

```
├── backend/
│   ├── .env                       # Backend environment variables
│   └── app/
│       ├── main.py                # FastAPI app & static file mounting
│       ├── core/
│       │   ├── config.py          # Settings & environment loading
│       │   ├── exceptions.py      # Custom exception definitions
│       │   ├── logging.py         # Structured logging configuration
│       │   └── security.py        # Input sanitization & validation
│       ├── integrations/
│       │   ├── sarvam/
│       │   │   ├── stt.py         # Sarvam STT client (saaras:v4)
│       │   │   └── tts.py         # Sarvam TTS client (bulbul:v3)
│       │   └── llm/
│       │       └── client.py      # Multilingual LLM orchestrator
│       ├── services/
│       │   └── citylinc_knowledge.py # Nashik routes, timetables & fare passes
│       ├── orchestrator/
│       │   └── conversation_manager.py # Session state & barge-in tracking
│       ├── api/
│       │   ├── rest_routes.py     # Health, status, and testing endpoints
│       │   └── websocket_routes.py# Real-time bidirectional voice WebSocket
│       └── static/
│           ├── index.html         # Voice Agent web interface
│           ├── style.css          # Styling & animated voice orb
│           └── app.js             # Web Audio, VAD, playback & barge-in
├── data/
│   └── raw/                       # CityLinc timetables, routes, fare passes
├── tests/
│   └── test_voice_agent.py        # Automated test suite
├── .env.example                   # Example environment configuration
├── requirements.txt               # Python package dependencies
└── README.md                      # Documentation
```


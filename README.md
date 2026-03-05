# 🎬 Reel Analyzer

**Drop an Instagram Reel. Ask anything. Get AI-powered analysis in seconds.**

Reel Analyzer downloads Instagram Reels, feeds them to a vision model,
and returns a natural language analysis - all through a single API call.
Built to be triggered from an **iOS Shortcut** so you can analyze reels
straight from the share sheet on your phone.

```text
You → Share Reel → iOS Shortcut → FastAPI → yt-dlp → Gemini → 💡
```

---

## ✨ Features

- **One endpoint, full pipeline** - download, process, analyze, respond
- **Swappable AI providers** - Gemini (cloud) or Qwen via Ollama (local)
- **Bearer token auth** - simple, secure
- **iOS Shortcut ready** - analyze reels right from Instagram's share
  sheet
- **Dockerized** - one command to build, one push to deploy

---

## 📡 API

```
POST /analyze
```

**Headers**

| Key             | Value              |
| --------------- | ------------------ |
| `Authorization` | `Bearer <token>`   |
| `Content-Type`  | `application/json` |

**Request**

```json
{
  "url": "https://www.instagram.com/reel/xxxxx/",
  "prompt": "What is happening in this reel?"
}
```

**Response**

```json
{
  "success": true,
  "analysis": "The video shows a man explaining how F1 drivers ...",
  "duration_seconds": 15.6
}
```

**Error**

```json
{
  "success": false,
  "error": "Download failed: file missing or empty",
  "duration_seconds": 2.1
}
```

---

## 🚀 Quickstart

### 1. Clone & install

```bash
git clone https://github.com/YOUR_USERNAME/reel-analyzer.git
cd reel-analyzer
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env.local
```

Edit `.env.local`:

```text
GEMINI_API_KEY=your-gemini-api-key
API_BEARER_TOKEN=your-secret-token
```

> Get a Gemini API key at
> [aistudio.google.com](https://aistudio.google.com/apikey).
> Generate a bearer token however you like - `openssl rand -base64 32`
> works great.

### 3. Run

```bash
uvicorn app.main:app --reload
```

### 4. Test

```bash
curl -s -X POST http://localhost:8000/analyze \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-secret-token" \
  -d '{
    "url": "https://www.instagram.com/reel/xxxxx/",
    "prompt": "Summarize this reel"
  }' | python -m json.tool
```

---

## 🐳 Docker

```bash
docker build -t reel-analyzer .
docker run --rm -p 8000:8000 \
  -e GEMINI_API_KEY="your-key" \
  -e API_BEARER_TOKEN="your-token" \
  reel-analyzer
```

---

## ☁️ Deploy to Railway

1. Push this repo to GitHub
2. [railway.app](https://railway.app) → **New Project** → **Deploy from
   GitHub repo**
3. Add environment variables in the **Variables** tab:
   - `GEMINI_API_KEY`
   - `API_BEARER_TOKEN`
4. Railway auto-detects the Dockerfile, builds, and deploys
5. Go to **Settings → Networking** → generate a public domain
6. You're live 🎉

---

## 📱 iOS Shortcut

Build a shortcut that lets you share a reel from Instagram and get
analysis on your phone:

1. **Receive** `URLs` from Share Sheet (fallback: Ask for URL)
2. **Ask for Input** - "What do you want to know about this reel?"
3. **Get Contents of URL** →
   `POST https://your-railway-url.up.railway.app/analyze` with JSON body
   and auth header
4. **Get Dictionary Value** for `success`
5. **If** `true` → **Show Result** with `analysis`
6. **Otherwise** → **Show Alert** with `error`

**Settings:** Name it "Analyze Reel", enable "Show in Share Sheet" for
URLs.

---

## 🔌 Providers

| Provider | Type  | Model            | Config                                            |
| -------- | ----- | ---------------- | ------------------------------------------------- |
| Gemini   | Cloud | gemini-2.5-flash | `ANALYZER_PROVIDER=gemini` + `GEMINI_API_KEY`     |
| Qwen     | Local | qwen2.5-vl:7b   | `ANALYZER_PROVIDER=qwen` + Ollama running locally |

Switch providers by setting `ANALYZER_PROVIDER` in your environment.

---

## 📂 Project Structure

```text
reel-analyzer/
├── app/
│   ├── main.py              # FastAPI app + /analyze endpoint
│   ├── config.py            # Centralized settings via pydantic-settings
│   ├── auth.py              # Bearer token verification
│   ├── downloader.py        # yt-dlp wrapper
│   └── analyzer/
│       ├── __init__.py      # Factory + singleton pattern
│       ├── base.py          # Abstract base class
│       ├── gemini.py        # Gemini provider
│       └── qwen.py          # Qwen via Ollama provider
├── Dockerfile
├── .dockerignore
├── .env.example
├── requirements.txt
└── README.md
```

---

## ⚙️ Environment Variables

| Variable            | Required | Default                  | Description                |
| ------------------- | -------- | ------------------------ | -------------------------- |
| `GEMINI_API_KEY`    | Yes\*    | -                        | Google Gemini API key      |
| `API_BEARER_TOKEN`  | Yes      | -                        | Auth token for the API     |
| `GEMINI_MODEL`      | No       | `gemini-2.5-flash`       | Gemini model name          |
| `ANALYZER_PROVIDER` | No       | `gemini`                 | `gemini` or `qwen`        |
| `OLLAMA_BASE_URL`   | No       | `http://localhost:11434` | Ollama server URL          |
| `QWEN_MODEL`        | No       | `qwen2.5-vl:7b`         | Qwen model name for Ollama |

\* Required when `ANALYZER_PROVIDER=gemini`

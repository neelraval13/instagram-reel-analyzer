# 🎬 Reel Analyzer

**Drop an Instagram Reel. Ask anything. Get AI-powered analysis in
seconds.**

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

## 📱 iOS Shortcut

Build a shortcut that lets you analyze reels from your phone in a few
taps.

### Setup (step by step)

1. Open **Shortcuts** → tap **+** → name it **Reel Analyzer**

2. **Ask for Input** — type: `URL`, prompt: `Paste the Reel URL`

3. **Set Variable** — name: `ReelURL`, value: `Provided Input`

4. **Ask for Input** — type: `Text`, prompt:
   `What do you want to know about this reel?`, default answer:
   `What is happening in this video?`

5. **Set Variable** — name: `UserPrompt`, value: `Provided Input`

6. **Get Contents of URL** — configure:

   | Field         | Value                                        |
   | ------------- | -------------------------------------------- |
   | URL           | `https://your-deployment-url.com/analyze`    |
   | Method        | `POST`                                       |
   | Request Body  | `JSON`                                       |

   **Headers:**

   | Key             | Value                  |
   | --------------- | ---------------------- |
   | `Authorization` | `Bearer <your-token>`  |
   | `Content-Type`  | `application/json`     |

   **JSON Body:**

   | Key      | Type | Value          |
   | -------- | ---- | -------------- |
   | `url`    | Text | `ReelURL` var  |
   | `prompt` | Text | `UserPrompt` var |

7. **Get Dictionary Value** — get value for key `analysis` in
   `Contents of URL`

8. **Show Content** — display `Dictionary Value`

### Action summary

```text
1. Ask for Input      → Paste the Reel URL
2. Set Variable       → ReelURL
3. Ask for Input      → What do you want to know?
4. Set Variable       → UserPrompt
5. Get Contents of URL → POST to /analyze with headers + JSON body
6. Get Dictionary Value → extract "analysis"
7. Show Content       → display the result
```

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

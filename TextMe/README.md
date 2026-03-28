# TextMe Quiz App

Sends daily multiple choice quiz questions via SMS, generated automatically from PDF study materials using Claude AI.

---

## How It Works

1. On startup the app reads all PDFs in the folder and sends the text to Claude
2. Claude generates multiple choice questions from the material
3. Every day at a set time, one question is texted to everyone in `recipients.txt`
4. Recipients reply with A, B, C, or D
5. The app responds with whether they got it right and an explanation

---

## Setup

### 1. Install dependencies
```bash
py -3.14 -m pip install anthropic flask twilio pypdf apscheduler python-dotenv
```

### 2. Configure `.env`
```
ANTHROPIC_API_KEY=your_anthropic_api_key
TWILIO_ACCOUNT_SID=your_twilio_account_sid
TWILIO_AUTH_TOKEN=your_twilio_auth_token
TWILIO_PHONE_NUMBER=+1XXXXXXXXXX
NUM_QUESTIONS=10
SEND_HOUR=9
SEND_MINUTE=0
PORT=5000
```

### 3. Add recipients
Edit `recipients.txt` — one phone number per line in +1XXXXXXXXXX format:
```
+19783028985
+12145581330
```

### 4. Add PDFs
Drop any PDF study materials into the TextMe folder. The app currently reads:
- `part1.pdf`
- `CriticalCare.pdf`
- `Thoracic.pdf`

---

## Running the App

**Terminal 1 — start the app:**
```bash
cd C:\Users\Emily\PycharmProjects\TextMe
py -3.14 app.py
```

**Terminal 2 — start ngrok (exposes app to internet for Twilio):**
```bash
ngrok http 5000
```

---

## Twilio Setup

1. Buy a toll-free number at twilio.com
2. Go to Phone Numbers → your number → Messaging Configuration
3. Set webhook URL to: `https://your-ngrok-url.ngrok-free.dev/webhook`
4. Save configuration

---

## Testing Locally (without SMS)

Start a quiz session:
```powershell
(Invoke-WebRequest -Uri "http://localhost:5000/webhook" -Method POST -Body "From=+19783028985&Body=START" -UseBasicParsing).Content
```

Submit an answer:
```powershell
(Invoke-WebRequest -Uri "http://localhost:5000/webhook" -Method POST -Body "From=+19783028985&Body=A" -UseBasicParsing).Content
```

---

## Commands

| User texts | App does |
|---|---|
| `START` | Sends a random question immediately |
| `A` / `B` / `C` / `D` | Evaluates answer, sends feedback |

Daily questions are sent automatically at the time set in `SEND_HOUR` and `SEND_MINUTE`.

---

## Files

| File | Purpose |
|---|---|
| `app.py` | Main application |
| `recipients.txt` | Phone numbers to send daily questions to |
| `progress.json` | Tracks which question each recipient is on (auto-created) |
| `.env` | API keys and settings (never commit this) |
| `*.pdf` | Study material PDFs |

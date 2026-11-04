# TextMe Quiz App (Text-The-Resident)

Texts a daily ABSITE-style General Surgery question to everyone on the list, written by Claude. People reply A, B, C, or D and get instant feedback with an explanation. Sending and receiving texts goes through Twilio on a registered A2P 10DLC campaign.

See [How_TextMe_Works.pdf](How_TextMe_Works.pdf) for a diagram.

---

## How It Works

1. **Questions:** on first start, Claude (`claude-opus-5-5`) writes `NUM_QUESTIONS` single-best-answer questions. Topics are drawn from the 44 categories of the [ABS content outline for the ABSITE](https://www.absurgery.org/wp-content/uploads/2023/01/GS-ITE.pdf), weighted like the exam (Trauma 6%, Critical Care 5%, Breast 4%, …). A second Claude pass then answers each question cold. A question is kept only if that pass agrees with the answer key, finds exactly one defensible answer, and is highly confident. The set is cached in `questions_cache.json`.
2. **Daily send:** every day at `SEND_HOUR:SEND_MINUTE`, everyone in `recipients.txt` (except anyone who texted STOP) gets their next `QUESTIONS_PER_DAY` questions (default 3) as separate texts labelled Q1, Q2, Q3. They can answer in any order, at any time, until the next morning's set replaces them. When the list runs low, Claude writes the next batch in the background, with no repeats of earlier questions.
   **Picture questions:** each batch includes `PICTURE_QUESTIONS_PER_BATCH` (default 2) built around a freely licensed image from Wikimedia Commons (public domain, CC0, CC BY or CC BY-SA only). Claude checks that the image clearly shows the finding and gives nothing away, writes the question around it, and the re-check pass answers it with the image. The required credit is added to the text.
3. **Replies:** Twilio forwards incoming texts through ngrok to `/webhook`. The app checks Twilio's signature, then handles the answer or command.
4. **Report:** every opt-in, question sent, answer, flag, and opt-out is logged to `events.jsonl` and summarized at `/report`.

---

## Setup

### 1. Install dependencies
```bash
py -3.14 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

### 2. Configure `.env`
```
ANTHROPIC_API_KEY=your_anthropic_api_key
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=your_twilio_auth_token
TWILIO_PHONE_NUMBER=+1XXXXXXXXXX
CAMPAIGN_DISPLAY_NUMBER=+1 (XXX) XXX-XXXX
NGROK_URL=https://your-domain.ngrok-free.dev
NUM_QUESTIONS=10
SEND_HOUR=7
SEND_MINUTE=0
SEND_ON_START=false
```

### 3. Add recipients
`recipients.txt` has one `phone, first name` per line. Lines starting with `#` are ignored:
```
# Format: +1XXXXXXXXXX, First Name
+15551234567, Emily
```
New people can also join themselves by texting **START** to the number. They're added to this file automatically and asked for their first name. Only add people here directly if they've agreed to get the texts, because the campaign rules require consent.

### 4. Topics
The topic list and weights are `ABSITE_TOPICS` in `app.py`. To start over with a fresh set of questions, delete `questions_cache.json` and restart. The old Thoracic set (generated from PDFs) is saved as `questions_cache_thoracic.json`.

### 5. Twilio
1. The phone number must be in the Messaging Service linked to the approved A2P 10DLC campaign (**Messaging → Services → Sender Pool**).
2. In that service, under **Integration → Incoming Messages**, choose **Send a webhook** and set the Request URL to `https://your-domain.ngrok-free.dev/webhook` (POST).
3. Keep funds in the account. Twilio suspends the account when the balance runs out, and nothing can be sent until it's topped up.

---

## Running the App

Double-click `start_textme.bat`. It starts both ngrok and the app, minimized.

Or start them yourself in two terminals:
```bash
ngrok http 5000
.venv\Scripts\python.exe app.py
```

The computer must stay on and awake for the daily send to go out and for replies to get answered.

---

## Commands people can text

| Text | What happens |
|---|---|
| `START` | Opts in. A new number is added to the daily list and asked for a first name, then gets a first question. |
| `Q1 A`, `q2=b`, `1A 2C 3B` | Answers one or several open questions. Each answer gets right/wrong and an explanation. A bare `A` works when only one question is open. |
| `QUIZ` | Sends an extra question right away, numbered after the day's (Q4, Q5…). It's random, so it may be one they've had. |
| `FLAG Q2 <note>` | Flags a question as possibly wrong (no number = the most recent one). Shows up on the report. |
| `BOARD` | Shows the leaderboard: top 5 by correct answers, with correct/answered and accuracy. |
| `BOARD OFF` / `BOARD ON` | Hides them from the leaderboard, or adds them back. Everyone is on it by default, by first name. |
| `HELP` | Lists the commands. |
| `STOP` | Unsubscribes. Twilio sends the confirmation, and the app stops sending daily questions to them. |

---

## Report

Open **http://localhost:5000/report** on the computer running the app. It shows:
- totals, plus each person's questions sent, answered, right, wrong, and accuracy
- 🚩 flagged questions, with notes
- the hardest questions first
- recent activity, and a CSV download of every event

The report shows phone numbers, so it only opens on this computer. To view it from elsewhere, set `REPORT_KEY` in `.env` and open `https://your-domain.ngrok-free.dev/report?key=<REPORT_KEY>`.

To send today's question to everyone right now (from this computer):
```powershell
Invoke-WebRequest -Method POST http://localhost:5000/send-now -UseBasicParsing
```

---

## Testing locally without texting

`/webhook` rejects anything not signed by Twilio. To test it with `Invoke-WebRequest`, set `VALIDATE_TWILIO_SIGNATURE=false` in `.env`, restart the app, and turn it back on afterwards:
```powershell
(Invoke-WebRequest -Uri "http://localhost:5000/webhook" -Method POST -Body "From=+15550001111&Body=QUIZ" -UseBasicParsing).Content
(Invoke-WebRequest -Uri "http://localhost:5000/webhook" -Method POST -Body "From=+15550001111&Body=A" -UseBasicParsing).Content
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Claude API key (required) |
| `TWILIO_ACCOUNT_SID` | — | Twilio account SID (required) |
| `TWILIO_AUTH_TOKEN` | — | Twilio auth token (required; also used to check webhook signatures) |
| `TWILIO_PHONE_NUMBER` | — | The registered Twilio number that sends the texts (required) |
| `CAMPAIGN_DISPLAY_NUMBER` | — | Number shown on the opt-in/privacy/terms pages |
| `NGROK_URL` | — | Public ngrok URL, used for signature checks and images (required) |
| `NUM_QUESTIONS` | `10` | Questions per batch (each batch also runs the re-check, so larger batches take longer) |
| `SEND_HOUR` / `SEND_MINUTE` | `9` / `0` | Daily send time (24h, this computer's time zone) |
| `SEND_ON_START` | `true` | Send to everyone as soon as the app starts. Set to `false` to avoid surprise texts on restart. |
| `QUESTIONS_PER_DAY` | `3` | Questions sent each morning |
| `PICTURE_QUESTIONS_PER_BATCH` | `2` | Picture questions in each batch of `NUM_QUESTIONS` |
| `REPORT_KEY` | — | Lets the report open through ngrok with `?key=` |
| `VALIDATE_TWILIO_SIGNATURE` | `true` | Reject webhook requests not signed by Twilio |
| `USE_WHATSAPP` / `WHATSAPP_NUMBER` | `false` | Legacy WhatsApp sandbox option |
| `PORT` | `5000` | Flask server port |

---

## Files

| File | Purpose |
|---|---|
| `app.py` | The app |
| `recipients.txt` | Daily list: `phone, first name` |
| `questions_cache.json` | Generated questions. Delete it to start a fresh set. |
| `progress.json` | Which question each person is on |
| `events.jsonl` | Log of every opt-in, send, answer, flag, and opt-out (not committed: has phone numbers) |
| `images/` | Question images, served at `/images/<filename>`. `gs_*` are General Surgery images from Wikimedia Commons; the rest are from the old Thoracic PDFs |
| `How_TextMe_Works.pdf` / `.html` | Diagram of the system, and its source |
| `start_textme.bat` | Starts ngrok and the app |
| `.env` | Keys and settings (never commit) |

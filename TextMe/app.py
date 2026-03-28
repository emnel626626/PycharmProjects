import os
import json
import threading
import random
from flask import Flask, request, Response
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient
from apscheduler.schedulers.background import BackgroundScheduler
import anthropic
from pypdf import PdfReader
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# Config
PDF_PATH = os.getenv("PDF_PATH", r"C:\Users\Emily\PycharmProjects\TextMe\part1.pdf")
NUM_QUESTIONS = int(os.getenv("NUM_QUESTIONS", "10"))
SEND_HOUR = int(os.getenv("SEND_HOUR", "9"))   # 9 AM daily by default
SEND_MINUTE = int(os.getenv("SEND_MINUTE", "0"))

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")

RECIPIENTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recipients.txt")
PROGRESS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "progress.json")

claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
twilio = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# Global state
_questions: list | None = None
_questions_error: str | None = None
sessions: dict = {}  # phone -> pending question waiting for answer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_recipients() -> list:
    if not os.path.exists(RECIPIENTS_FILE):
        return []
    with open(RECIPIENTS_FILE) as f:
        return [l.strip() for l in f if l.strip() and not l.startswith("#")]


def load_progress() -> dict:
    if not os.path.exists(PROGRESS_FILE):
        return {}
    with open(PROGRESS_FILE) as f:
        return json.load(f)


def save_progress(progress: dict) -> None:
    with open(PROGRESS_FILE, "w") as f:
        json.dump(progress, f, indent=2)


def extract_pdf_text() -> str:
    reader = PdfReader(PDF_PATH)
    parts = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def generate_questions(text: str, num_questions: int) -> list:
    text = text[:120_000]
    response = claude.messages.create(
        model="claude-opus-4-6",
        max_tokens=8000,
        messages=[{
            "role": "user",
            "content": (
                f"Based on the following text, generate exactly {num_questions} "
                "multiple-choice questions to test comprehension.\n\n"
                "Return ONLY a valid JSON array — no markdown fences, no extra text. "
                "Each element must follow this exact structure:\n"
                "{\n"
                '  "question": "The question text",\n'
                '  "options": {"A": "...", "B": "...", "C": "...", "D": "..."},\n'
                '  "correct": "A",\n'
                '  "explanation": "Why this answer is correct and the others are wrong"\n'
                "}\n\n"
                f"TEXT:\n{text}"
            )
        }]
    )
    content = response.content[0].text.strip()
    if "```" in content:
        start = content.find("[")
        end = content.rfind("]") + 1
        if start != -1 and end > start:
            content = content[start:end]
    return json.loads(content)


def preload_questions() -> None:
    global _questions, _questions_error
    try:
        print(f"[Quiz] Extracting text from {PDF_PATH}...")
        text = extract_pdf_text()
        print(f"[Quiz] Extracted {len(text):,} characters. Generating {NUM_QUESTIONS} questions...")
        _questions = generate_questions(text, NUM_QUESTIONS)
        print(f"[Quiz] Ready — {len(_questions)} questions loaded.")
    except Exception as exc:
        _questions_error = str(exc)
        print(f"[Quiz] Error: {exc}")


def format_question(q: dict, num: int, total: int) -> str:
    return (
        f"Q{num}/{total}: {q['question']}\n\n"
        f"A) {q['options']['A']}\n"
        f"B) {q['options']['B']}\n"
        f"C) {q['options']['C']}\n"
        f"D) {q['options']['D']}\n\n"
        "Reply A, B, C, or D"
    )


# ---------------------------------------------------------------------------
# Daily sender (runs on schedule)
# ---------------------------------------------------------------------------

def send_daily_questions() -> None:
    if _questions is None:
        print("[Scheduler] Questions not ready, skipping.")
        return

    recipients = load_recipients()
    if not recipients:
        print("[Scheduler] No recipients found in recipients.txt")
        return

    progress = load_progress()

    for phone in recipients:
        idx = progress.get(phone, 0)
        if idx >= len(_questions):
            idx = 0  # loop back to start

        q = _questions[idx]
        body = f"📚 Daily Quiz!\n\n{format_question(q, idx + 1, len(_questions))}"

        try:
            twilio.messages.create(
                to=phone,
                from_=TWILIO_PHONE_NUMBER,
                body=body
            )
            sessions[phone] = {"question": q, "index": idx}
            progress[phone] = idx + 1
            print(f"[Scheduler] Sent Q{idx + 1} to {phone}")
        except Exception as exc:
            print(f"[Scheduler] Failed to send to {phone}: {exc}")

    save_progress(progress)


# ---------------------------------------------------------------------------
# Webhook (handles replies)
# ---------------------------------------------------------------------------

@app.route("/webhook", methods=["POST"])
def webhook():
    from_number = request.form.get("From", "")
    body = request.form.get("Body", "").strip().upper()

    resp = MessagingResponse()

    # ── Answer to a pending question ────────────────────────────────────────
    if from_number in sessions:
        q = sessions[from_number]["question"]

        if body not in ("A", "B", "C", "D"):
            resp.message("Please reply with A, B, C, or D.")
            return Response(str(resp), mimetype="text/xml")

        correct = q["correct"]
        if body == correct:
            feedback = f"✅ Correct!\n\n{q['explanation']}"
        else:
            feedback = (
                f"❌ Incorrect. The correct answer is "
                f"{correct}) {q['options'][correct]}.\n\n"
                f"{q['explanation']}"
            )

        del sessions[from_number]
        resp.message(feedback)
        return Response(str(resp), mimetype="text/xml")

    # ── On-demand question via START ─────────────────────────────────────────
    if body in ("START", "QUIZ", "BEGIN"):
        if _questions is None:
            resp.message("Still loading, try again in a moment! ⏳")
        else:
            q = random.choice(_questions)
            sessions[from_number] = {"question": q}
            resp.message(f"📚 Here's a question!\n\n{format_question(q, 1, 1)}")
        return Response(str(resp), mimetype="text/xml")

    # ── Default ───────────────────────────────────────────────────────────────
    resp.message("📚 You'll get a daily question automatically!\nText START for one right now.")
    return Response(str(resp), mimetype="text/xml")


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

threading.Thread(target=preload_questions, daemon=True).start()

scheduler = BackgroundScheduler()
scheduler.add_job(send_daily_questions, "cron", hour=SEND_HOUR, minute=SEND_MINUTE)
scheduler.start()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(debug=False, port=port)

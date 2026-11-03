import os
import io
import base64
import csv
import html
import json
import threading
import random
import time

from flask import Flask, request, Response, send_from_directory
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient
from apscheduler.schedulers.background import BackgroundScheduler
import anthropic
from pypdf import PdfReader
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# Config
PDF_DIR = os.getenv("PDF_DIR", r"C:\Users\Emily\PycharmProjects\TextMe")
PDF_PATHS = [
    os.path.join(PDF_DIR, "part1.pdf"),
    os.path.join(PDF_DIR, "CriticalCare.pdf"),
    os.path.join(PDF_DIR, "Thoracic.pdf"),
]
NUM_QUESTIONS = int(os.getenv("NUM_QUESTIONS", "10"))
SEND_HOUR = int(os.getenv("SEND_HOUR", "9"))
SEND_MINUTE = int(os.getenv("SEND_MINUTE", "0"))

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
USE_WHATSAPP = os.getenv("USE_WHATSAPP", "false").lower() == "true"
WHATSAPP_NUMBER = os.getenv("WHATSAPP_NUMBER", "whatsapp:+14155238886")

RECIPIENTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recipients.txt")
PROGRESS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "progress.json")
QUESTIONS_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "questions_cache.json")
PREVIOUS_QUESTIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "questions_cache_previous.json")
EVENTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "events.jsonl")
REPORT_KEY = os.getenv("REPORT_KEY", "")
IMAGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images")
NGROK_URL = os.getenv("NGROK_URL", "").rstrip("/")
IMAGE_FREQUENCY = float(os.getenv("IMAGE_FREQUENCY", "1.0"))
SEND_ON_START = os.getenv("SEND_ON_START", "true").lower() == "true"

os.makedirs(IMAGE_DIR, exist_ok=True)

claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
twilio = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# Global state
_questions: list | None = None
_questions_error: str | None = None
_page_image_map: dict = {}   # page_key -> [image filenames]
sessions: dict = {}


# ---------------------------------------------------------------------------
# Recipients / progress
# ---------------------------------------------------------------------------

def load_recipients() -> dict:
    if not os.path.exists(RECIPIENTS_FILE):
        return {}
    result = {}
    with open(RECIPIENTS_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "," in line:
                phone, name = line.split(",", 1)
                result[phone.strip()] = name.strip()
            else:
                result[line] = ""
    return result


def load_progress() -> dict:
    if not os.path.exists(PROGRESS_FILE):
        return {}
    with open(PROGRESS_FILE) as f:
        return json.load(f)


def save_progress(progress: dict) -> None:
    with open(PROGRESS_FILE, "w") as f:
        json.dump(progress, f, indent=2)


# ---------------------------------------------------------------------------
# Event log — one JSON object per line, used by /report
# ---------------------------------------------------------------------------

_events_lock = threading.Lock()


def log_event(event: str, phone: str, name: str = "", **fields) -> None:
    record = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "event": event,
        "phone": phone,
        "name": name,
        **fields,
    }
    with _events_lock:
        with open(EVENTS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_events() -> list:
    if not os.path.exists(EVENTS_FILE):
        return []
    events = []
    with open(EVENTS_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return events


def restore_sessions() -> None:
    # Rebuild unanswered questions from the event log so a restart doesn't
    # drop answers people send afterwards.
    known = list(_questions or [])
    if os.path.exists(PREVIOUS_QUESTIONS_FILE):
        with open(PREVIOUS_QUESTIONS_FILE) as f:
            known += json.load(f)["questions"]
    by_text = {q["question"]: q for q in known}
    pending = {}
    for e in load_events():
        if e["event"] == "question_sent":
            # Newer events carry the full question; older ones are looked up by text.
            q = e.get("q") or by_text.get(e["question"])
            if q:
                pending[e["phone"]] = {"question": q, "name": e.get("name", "")}
        elif e["event"] in ("answer", "opt_out"):
            pending.pop(e["phone"], None)
    for phone, session in pending.items():
        sessions.setdefault(phone, session)
    if pending:
        names = ", ".join(s["name"] or phone for phone, s in pending.items())
        print(f"[Quiz] Restored {len(pending)} unanswered question(s): {names}")


def opted_out_numbers() -> set:
    # A number's latest opt event wins, so texting START after STOP re-subscribes.
    status = {}
    for e in load_events():
        if e["event"] in ("opt_in", "opt_out"):
            status[e["phone"]] = e["event"]
    return {phone for phone, s in status.items() if s == "opt_out"}


# ---------------------------------------------------------------------------
# PDF extraction — text + images tracked per page
# ---------------------------------------------------------------------------

def extract_pdf_content() -> dict:
    """Extract text from all PDFs, tagging each page with a unique ID.
    Also populates _page_image_map with images per page.
    Returns {pdf_name: [tagged page text, ...]} for question generation.
    """
    global _page_image_map
    _page_image_map = {}
    pages_by_pdf = {}

    for pdf_path in PDF_PATHS:
        if not os.path.exists(pdf_path):
            print(f"[Quiz] Warning: {pdf_path} not found, skipping.")
            continue

        pdf_name = os.path.splitext(os.path.basename(pdf_path))[0]
        reader = PdfReader(pdf_path)
        num_pages = len(reader.pages)
        print(f"[Quiz] {pdf_name}: {num_pages} pages")
        pdf_start = time.monotonic()
        text_parts = pages_by_pdf.setdefault(pdf_name, [])

        for page_num, page in enumerate(reader.pages):
            page_start = time.monotonic()
            page_key = f"{pdf_name}_p{page_num}"

            # Text — tag it so Claude can reference the page
            text = page.extract_text()
            if text:
                text_parts.append(f"[PAGE_ID:{page_key}]\n{text}")

            # Images — save and map to this page
            page_images = []
            try:
                for img_num, image in enumerate(page.images):
                    if len(image.data) < 10_000:
                        continue
                    ext = image.name.split(".")[-1].lower()
                    if ext not in ("png", "jpg", "jpeg"):
                        ext = "png"
                    filename = f"{pdf_name}_p{page_num}_{img_num}.{ext}"
                    img_path = os.path.join(IMAGE_DIR, filename)
                    if not os.path.exists(img_path):
                        with open(img_path, "wb") as f:
                            f.write(image.data)
                    page_images.append(filename)
            except Exception as e:
                print(f"[Images] Error on {pdf_name} page {page_num}: {e}")

            if page_images:
                _page_image_map[page_key] = page_images

            page_elapsed = time.monotonic() - page_start
            if page_elapsed > 1.0 or page_num % 20 == 0:
                print(f"[Quiz] {pdf_name} page {page_num}/{num_pages} took {page_elapsed:.1f}s"
                      f"{' (SLOW)' if page_elapsed > 1.0 else ''}")

        print(f"[Quiz] {pdf_name} done in {time.monotonic() - pdf_start:.1f}s")

    total_images = sum(len(v) for v in _page_image_map.values())
    print(f"[Images] {total_images} images mapped across {len(_page_image_map)} pages.")
    return pages_by_pdf


# ---------------------------------------------------------------------------
# Question generation
# ---------------------------------------------------------------------------

MAX_CHARS_PER_PDF = 120_000
MIN_QUESTIONS_PER_PDF = 2


def sample_pages(pages: list, max_chars: int) -> str:
    """Keep evenly spaced pages so a long PDF is covered start to finish."""
    total = sum(len(p) for p in pages)
    if total > max_chars:
        step = -(-total // max_chars)  # ceiling division
        pages = pages[random.randrange(step)::step]
    return "\n\n".join(pages)[:max_chars]


def split_questions(pages_by_pdf: dict, num_questions: int) -> dict:
    """Split questions across PDFs by text size, with a minimum per PDF."""
    sizes = {name: sum(len(p) for p in pages) for name, pages in pages_by_pdf.items() if pages}
    base = min(MIN_QUESTIONS_PER_PDF, num_questions // max(len(sizes), 1))
    counts = {name: base for name in sizes}
    remaining = num_questions - base * len(sizes)
    total = sum(sizes.values())
    shares = {name: remaining * size / total for name, size in sizes.items()}
    for name in sizes:
        counts[name] += int(shares[name])
    # Hand out what rounding left over to the largest fractional shares
    leftover = num_questions - sum(counts.values())
    for name in sorted(sizes, key=lambda n: shares[n] - int(shares[n]), reverse=True)[:leftover]:
        counts[name] += 1
    return counts


def interleave(groups: list) -> list:
    """Round-robin merge so consecutive daily questions rotate between PDFs."""
    merged = []
    for i in range(max((len(g) for g in groups), default=0)):
        merged.extend(g[i] for g in groups if i < len(g))
    return merged


def generate_all_questions(pages_by_pdf: dict, num_questions: int, avoid: list | None = None) -> list:
    groups = []
    for name, count in split_questions(pages_by_pdf, num_questions).items():
        if count == 0:
            continue
        print(f"[Quiz] Generating {count} questions from {name}...")
        groups.append(generate_questions(sample_pages(pages_by_pdf[name], MAX_CHARS_PER_PDF), count, avoid))
    return interleave(groups)


def add_more_questions() -> None:
    """Append a fresh batch so nobody starts repeating questions."""
    print(f"[Quiz] Someone reached the end of the list — generating {NUM_QUESTIONS} more questions...")
    pages_by_pdf = extract_pdf_content()
    new = generate_all_questions(pages_by_pdf, NUM_QUESTIONS, avoid=[q["question"] for q in _questions])
    assign_images(new)
    _questions.extend(new)
    with open(QUESTIONS_CACHE_FILE, "w") as f:
        json.dump({"questions": _questions, "page_image_map": _page_image_map}, f, indent=2)
    print(f"[Quiz] Now {len(_questions)} questions.")


def generate_questions(text: str, num_questions: int, avoid: list | None = None) -> list:
    avoid_text = ""
    if avoid:
        avoid_text = (
            "Do NOT repeat, reword, or test the same fact as any of these existing questions:\n"
            + "\n".join(f"- {a}" for a in avoid) + "\n\n"
        )
    response = claude.messages.create(
        model="claude-opus-4-8",
        max_tokens=8000,
        messages=[{
            "role": "user",
            "content": (
                f"Based on the following text, generate exactly {num_questions} "
                "multiple-choice questions to test comprehension.\n\n"
                "Each page of text is tagged with [PAGE_ID:some_id]. "
                "For each question, record which PAGE_ID the question is primarily based on.\n\n"
                "Return ONLY a valid JSON array — no markdown fences, no extra text. "
                "Each element must follow this exact structure:\n"
                "{\n"
                '  "question": "The question text",\n'
                '  "options": {"A": "...", "B": "...", "C": "...", "D": "..."},\n'
                '  "correct": "A",\n'
                '  "explanation": "A conversational explanation — talk to the student directly, explain the concept in plain language, do NOT reference the text or passage",\n'
                '  "source_page": "the PAGE_ID this question came from"\n'
                "}\n\n"
                "For explanations: be friendly and direct, explain why the concept works this way, briefly mention why wrong answers are off.\n\n"
                f"{avoid_text}TEXT:\n{text}"
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
    global _questions, _questions_error, _page_image_map
    try:
        if os.path.exists(QUESTIONS_CACHE_FILE):
            print("[Quiz] Loading questions from cache (delete questions_cache.json to regenerate)...")
            with open(QUESTIONS_CACHE_FILE) as f:
                cache = json.load(f)
            _questions = cache["questions"]
            _page_image_map = cache["page_image_map"]
        else:
            print("[Quiz] Extracting text and images from PDFs...")
            pages_by_pdf = extract_pdf_content()
            total_chars = sum(len(p) for pages in pages_by_pdf.values() for p in pages)
            print(f"[Quiz] Extracted {total_chars:,} characters. Generating {NUM_QUESTIONS} questions...")
            _questions = generate_all_questions(pages_by_pdf, NUM_QUESTIONS)
            with open(QUESTIONS_CACHE_FILE, "w") as f:
                json.dump({"questions": _questions, "page_image_map": _page_image_map}, f, indent=2)
        if any("image" not in q for q in _questions):
            assign_images(_questions)
            with open(QUESTIONS_CACHE_FILE, "w") as f:
                json.dump({"questions": _questions, "page_image_map": _page_image_map}, f, indent=2)
        print(f"[Quiz] Ready — {len(_questions)} questions loaded.")
        restore_sessions()
        if SEND_ON_START:
            print("[Quiz] Sending startup questions to all recipients...")
            send_daily_questions()
    except Exception as exc:
        _questions_error = str(exc)
        print(f"[Quiz] Error: {exc}")


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

def image_fits_question(q: dict, filename: str) -> bool:
    with open(os.path.join(IMAGE_DIR, filename), "rb") as f:
        data = base64.standard_b64encode(f.read()).decode()
    media_type = "image/png" if filename.lower().endswith(".png") else "image/jpeg"
    response = claude.messages.create(
        model="claude-opus-4-8",
        max_tokens=5,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}},
                {"type": "text", "text": (
                    "This image comes from the same textbook page as the quiz question below. "
                    "Answer YES only if the image directly shows the patient, finding, or concept "
                    "the question is about, so it would help a student reading the question, AND it "
                    "does not reveal the correct answer in a label or caption. Otherwise answer NO. "
                    "Reply with only YES or NO.\n\n"
                    f"Question: {q['question']}\n"
                    f"Correct answer: {q['correct']}) {q['options'][q['correct']]}"
                )},
            ],
        }],
    )
    return response.content[0].text.strip().upper().startswith("YES")


def assign_images(questions: list) -> None:
    """Pick a relevant image for each question (or none), stored as q["image"]."""
    for q in questions:
        if "image" in q:
            continue
        q["image"] = None
        for filename in _page_image_map.get(q.get("source_page", ""), []):
            try:
                if image_fits_question(q, filename):
                    q["image"] = filename
                    break
            except Exception as exc:
                print(f"[Images] Could not check {filename}: {exc}")
        print(f"[Images] {q['source_page']}: {q['image'] or 'no relevant image'}")


def get_image_url_for_question(q: dict) -> str | None:
    """Return a public URL to the question's relevant image, if it has one."""
    if not NGROK_URL or not q.get("image") or random.random() > IMAGE_FREQUENCY:
        return None
    return f"{NGROK_URL}/images/{q['image']}"


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_question(q: dict) -> str:
    return (
        f"{q['question']}\n\n"
        f"A) {q['options']['A']}\n"
        f"B) {q['options']['B']}\n"
        f"C) {q['options']['C']}\n"
        f"D) {q['options']['D']}\n\n"
        "Reply A, B, C, or D"
    )


# ---------------------------------------------------------------------------
# Twilio helpers
# ---------------------------------------------------------------------------

def to_whatsapp(phone: str) -> str:
    return phone if phone.startswith("whatsapp:") else f"whatsapp:{phone}"


def strip_whatsapp(phone: str) -> str:
    return phone.replace("whatsapp:", "").strip()


def get_from_number(phone: str) -> str:
    return WHATSAPP_NUMBER if USE_WHATSAPP else TWILIO_PHONE_NUMBER


# ---------------------------------------------------------------------------
# Daily sender
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
    opted_out = opted_out_numbers()

    if any(progress.get(phone, 0) >= len(_questions) for phone in recipients if phone not in opted_out):
        try:
            add_more_questions()
        except Exception as exc:
            print(f"[Quiz] Could not generate more questions, repeating from the start: {exc}")

    for phone, name in recipients.items():
        if phone in opted_out:
            print(f"[Scheduler] Skipping {name or phone} (opted out)")
            continue
        idx = progress.get(phone, 0)
        if idx >= len(_questions):
            idx = 0

        q = _questions[idx]
        greeting = f"Hi {name}! " if name else ""
        body = f"📚 {greeting}Daily Quiz!\n\n{format_question(q)}"
        image_url = get_image_url_for_question(q)

        to = to_whatsapp(phone) if USE_WHATSAPP else phone
        try:
            msg_params = dict(to=to, from_=get_from_number(phone), body=body)
            if image_url:
                msg_params["media_url"] = [image_url]
            twilio.messages.create(**msg_params)
            sessions[phone] = {"question": q, "index": idx, "name": name}
            progress[phone] = idx + 1
            log_event("question_sent", phone, name, source="daily", question=q["question"], q=q)
            print(f"[Scheduler] Sent Q{idx + 1} to {name or phone}" + (" (with image)" if image_url else ""))
        except Exception as exc:
            print(f"[Scheduler] Failed to send to {phone}: {exc}")

    save_progress(progress)


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------

BRAND_NAME = "Text-The-Resident"
CAMPAIGN_NUMBER = os.getenv("CAMPAIGN_DISPLAY_NUMBER", "[number pending Twilio registration]")
OPT_IN_KEYWORDS = ("START", "YES", "UNSTOP")
OPT_OUT_KEYWORDS = ("STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT")
ON_DEMAND_KEYWORDS = ("QUIZ", "BEGIN")

PAGE_STYLE = """
  body { font-family: Arial, sans-serif; max-width: 600px; margin: 40px auto; padding: 20px; color: #333; }
  h1 { color: #1a73e8; }
  .box { background: #f0f4ff; border-left: 4px solid #1a73e8; padding: 16px; margin: 20px 0; border-radius: 4px; }
  .number { font-size: 1.4em; font-weight: bold; color: #1a73e8; }
  footer { margin-top: 32px; font-size: 0.9em; }
  footer a { color: #1a73e8; }
"""


@app.route("/optin")
def optin_page():
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{BRAND_NAME} - Opt-In</title>
<style>{PAGE_STYLE}</style>
</head>
<body>
  <h1>{BRAND_NAME}</h1>
  <p>{BRAND_NAME} sends educational medical multiple-choice quiz questions via SMS to help the recipient self-study.</p>

  <h2>How to Opt In</h2>
  <div class="box">
    <p>Text the keyword <strong>START</strong> to:</p>
    <p class="number">{CAMPAIGN_NUMBER}</p>
    <p>By texting START, you consent to receive SMS quiz messages from {BRAND_NAME}.</p>
  </div>

  <h2>What You'll Receive</h2>
  <ul>
    <li>1 multiple-choice quiz question per day</li>
    <li>Answer feedback and explanations</li>
    <li>Text START anytime for a bonus question</li>
  </ul>

  <h2>How to Opt Out</h2>
  <p>Reply <strong>STOP</strong> at any time to unsubscribe. Reply <strong>HELP</strong> for help.</p>
  <p><em>Message and data rates may apply. Message frequency varies.</em></p>

  <footer>
    <a href="/privacy">Privacy Policy</a> &nbsp;|&nbsp; <a href="/terms">Terms &amp; Conditions</a>
  </footer>
</body>
</html>"""


@app.route("/privacy")
def privacy_page():
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Privacy Policy</title>
<style>{PAGE_STYLE}</style>
</head>
<body>
  <h1>Privacy Policy</h1>
  <p>This Privacy Policy describes how {BRAND_NAME} ("we", "us") collects, uses, and protects information in connection with our SMS quiz service.</p>

  <h2>Information We Collect</h2>
  <ul>
    <li>Your mobile phone number, when you text START to opt in.</li>
    <li>The content of messages you send us (e.g. your quiz answers A/B/C/D, or keywords like STOP/HELP).</li>
  </ul>

  <h2>How We Use Your Information</h2>
  <p>We use your phone number and message content solely to deliver quiz questions, process your answers, send feedback/explanations, and respond to opt-out or help requests. We do not use this information for any other purpose.</p>

  <h2>Sharing of Information</h2>
  <p>We do not sell or share your SMS opt-in data or personal information with third parties for marketing purposes.</p>
  <p>Your information may be shared with the SMS carriers and messaging platform providers (e.g. Twilio) solely as needed to deliver the service.</p>

  <h2>Data Retention</h2>
  <p>We retain your phone number and quiz progress only for as long as needed to operate the service, or until you opt out by replying STOP.</p>

  <h2>Contact</h2>
  <p>Questions about this policy can be directed to the {BRAND_NAME} program contact.</p>

  <footer>
    <a href="/optin">Opt-In</a> &nbsp;|&nbsp; <a href="/terms">Terms &amp; Conditions</a>
  </footer>
</body>
</html>"""


@app.route("/terms")
def terms_page():
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Terms & Conditions</title>
<style>{PAGE_STYLE}</style>
</head>
<body>
  <h1>Terms &amp; Conditions</h1>
  <p>These Terms &amp; Conditions govern your use of the {BRAND_NAME} SMS quiz service.</p>

  <h2>SMS Terms</h2>
  <ul>
    <li>By texting START to {CAMPAIGN_NUMBER}, you consent to receive recurring automated SMS quiz messages from {BRAND_NAME}.</li>
    <li>Message frequency varies; approximately 1 message per day, plus a reply for each answer you submit.</li>
    <li><strong>Message and data rates may apply.</strong></li>
    <li>You may reply <strong>STOP</strong> at any time to cancel/unsubscribe. After texting STOP, you will receive one final confirmation message and no further messages will be sent.</li>
    <li>Reply <strong>HELP</strong> for assistance.</li>
    <li>Carriers are not liable for delayed or undelivered messages.</li>
  </ul>

  <h2>Use of the Service</h2>
  <p>The {BRAND_NAME} service is provided for educational self-study purposes. Content is generated from study materials and may contain errors; it is not a substitute for professional medical education, advice, or clinical judgment.</p>

  <h2>Changes</h2>
  <p>We may update these terms from time to time; continued use of the service after changes constitutes acceptance of the updated terms.</p>

  <footer>
    <a href="/optin">Opt-In</a> &nbsp;|&nbsp; <a href="/privacy">Privacy Policy</a>
  </footer>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def report_allowed() -> bool:
    # ngrok forwards from localhost but adds X-Forwarded-For, so this only
    # matches requests made on this machine.
    if REPORT_KEY and request.args.get("key") == REPORT_KEY:
        return True
    return request.remote_addr in ("127.0.0.1", "::1") and not request.headers.get("X-Forwarded-For")


def build_report(events: list) -> tuple[list, list]:
    people: dict = {}
    questions: dict = {}
    for e in events:
        p = people.setdefault(e["phone"], {
            "phone": e["phone"], "name": "", "status": "", "opted_in_at": "",
            "sent": 0, "answered": 0, "correct": 0, "last_activity": "",
        })
        p["name"] = e.get("name") or p["name"]
        p["last_activity"] = e["ts"]
        if e["event"] == "opt_in":
            p["status"] = "Opted in"
            p["opted_in_at"] = p["opted_in_at"] or e["ts"]
        elif e["event"] == "opt_out":
            p["status"] = "Opted out"
        elif e["event"] == "question_sent":
            p["sent"] += 1
        elif e["event"] == "answer":
            p["answered"] += 1
            p["correct"] += e["is_correct"]
            qs = questions.setdefault(e["question"], {"question": e["question"], "answered": 0, "correct": 0})
            qs["answered"] += 1
            qs["correct"] += e["is_correct"]

    for p in people.values():
        p["wrong"] = p["answered"] - p["correct"]
        p["accuracy"] = round(100 * p["correct"] / p["answered"]) if p["answered"] else None
    for qs in questions.values():
        qs["accuracy"] = round(100 * qs["correct"] / qs["answered"])

    people_rows = sorted(people.values(), key=lambda p: p["last_activity"], reverse=True)
    question_rows = sorted(questions.values(), key=lambda q: (q["accuracy"], -q["answered"]))
    return people_rows, question_rows


EVENT_LABELS = {
    "opt_in": "Opted in", "opt_out": "Opted out",
    "question_sent": "Question sent", "answer": "Answered",
}


@app.route("/report")
def report_page():
    if not report_allowed():
        return Response("Not found", status=404)

    events = load_events()
    people, questions = build_report(events)
    esc = html.escape
    key_param = f"?key={esc(request.args['key'])}" if request.args.get("key") else ""

    total_answers = sum(p["answered"] for p in people)
    total_correct = sum(p["correct"] for p in people)
    overall = f"{round(100 * total_correct / total_answers)}%" if total_answers else "—"
    opted_in = sum(1 for p in people if p["status"] == "Opted in")

    people_html = "".join(
        f"<tr><td>{esc(p['name']) or '—'}</td><td>{esc(p['phone'])}</td>"
        f"<td>{esc(p['status']) or '—'}</td><td>{esc(p['opted_in_at']) or '—'}</td>"
        f"<td class=n>{p['sent']}</td><td class=n>{p['answered']}</td>"
        f"<td class=n>{p['correct']}</td><td class=n>{p['wrong']}</td>"
        f"<td class=n>{'—' if p['accuracy'] is None else str(p['accuracy']) + '%'}</td>"
        f"<td>{esc(p['last_activity'])}</td></tr>"
        for p in people
    ) or "<tr><td colspan=10 class=empty>No activity yet.</td></tr>"

    questions_html = "".join(
        f"<tr><td>{esc(q['question'])}</td><td class=n>{q['answered']}</td>"
        f"<td class=n>{q['correct']}</td><td class=n>{q['accuracy']}%</td></tr>"
        for q in questions
    ) or "<tr><td colspan=4 class=empty>No answers yet.</td></tr>"

    def detail(e: dict) -> str:
        if e["event"] == "answer":
            mark = "✅ Correct" if e["is_correct"] else f"❌ Wrong (correct: {esc(e['correct_answer'])})"
            return f"Answered {esc(e['answer'])} — {mark}<br><small>{esc(e['question'])}</small>"
        if e["event"] == "question_sent":
            return f"{esc(e.get('source', ''))}<br><small>{esc(e['question'])}</small>"
        return f"Keyword: {esc(e.get('keyword', ''))}"

    log_html = "".join(
        f"<tr><td>{esc(e['ts'])}</td><td>{esc(e.get('name') or e['phone'])}</td>"
        f"<td>{EVENT_LABELS.get(e['event'], esc(e['event']))}</td><td>{detail(e)}</td></tr>"
        for e in reversed(events[-200:])
    ) or "<tr><td colspan=4 class=empty>No activity yet.</td></tr>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{BRAND_NAME} - Report</title>
<style>
  body {{ font-family: Arial, sans-serif; max-width: 1100px; margin: 32px auto; padding: 0 16px; color: #333; }}
  h1 {{ color: #1a73e8; }}
  h2 {{ margin-top: 36px; }}
  .stats {{ display: flex; gap: 16px; flex-wrap: wrap; }}
  .stat {{ background: #f0f4ff; border-left: 4px solid #1a73e8; padding: 12px 18px; border-radius: 4px; min-width: 140px; }}
  .stat b {{ display: block; font-size: 1.6em; color: #1a73e8; }}
  .scroll {{ overflow-x: auto; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 0.92em; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #e3e3e3; vertical-align: top; }}
  th {{ background: #fafafa; }}
  td.n {{ text-align: right; font-variant-numeric: tabular-nums; }}
  td.empty {{ color: #888; text-align: center; }}
  small {{ color: #666; }}
  a {{ color: #1a73e8; }}
</style>
</head>
<body>
  <h1>{BRAND_NAME} Report</h1>
  <p><a href="/report.csv{key_param}">Download all events (CSV)</a></p>
  <div class="stats">
    <div class="stat"><b>{len(people)}</b>People</div>
    <div class="stat"><b>{opted_in}</b>Opted in</div>
    <div class="stat"><b>{total_answers}</b>Answers</div>
    <div class="stat"><b>{overall}</b>Correct overall</div>
  </div>

  <h2>People</h2>
  <div class="scroll"><table>
    <tr><th>Name</th><th>Phone</th><th>Status</th><th>Opted in</th><th>Sent</th><th>Answered</th>
        <th>Right</th><th>Wrong</th><th>Accuracy</th><th>Last activity</th></tr>
    {people_html}
  </table></div>

  <h2>Questions (hardest first)</h2>
  <div class="scroll"><table>
    <tr><th>Question</th><th>Answered</th><th>Right</th><th>Accuracy</th></tr>
    {questions_html}
  </table></div>

  <h2>Recent activity</h2>
  <div class="scroll"><table>
    <tr><th>Time</th><th>Who</th><th>Event</th><th>Details</th></tr>
    {log_html}
  </table></div>
</body>
</html>"""


@app.route("/send-now", methods=["POST"])
def send_now():
    if not report_allowed():
        return Response("Not found", status=404)
    send_daily_questions()
    return "Sent — see /report"


@app.route("/report.csv")
def report_csv():
    if not report_allowed():
        return Response("Not found", status=404)

    out = io.StringIO()
    fields = ["ts", "event", "phone", "name", "keyword", "source", "question", "answer", "correct_answer", "is_correct"]
    writer = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(load_events())
    return Response(
        out.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=textme_events.csv"},
    )


@app.route("/images/<path:filename>")
def serve_image(filename):
    return send_from_directory(IMAGE_DIR, filename)


@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.get_json(silent=True) if request.is_json else {}

    from_number = strip_whatsapp(
        request.form.get("From")
        or request.form.get("from")
        or request.form.get("originator")
        or payload.get("originator")
        or payload.get("from")
        or payload.get("From")
        or ""
    )
    body = (
        request.form.get("Body")
        or request.form.get("body")
        or request.form.get("text")
        or payload.get("body")
        or payload.get("text")
        or payload.get("Body")
        or ""
    ).strip().upper()

    resp = MessagingResponse()

    # ── Opt-out (Twilio sends the STOP reply itself; we just record it) ───────
    if body in OPT_OUT_KEYWORDS:
        sessions.pop(from_number, None)
        log_event("opt_out", from_number, load_recipients().get(from_number, ""), keyword=body)
        return Response(str(resp), mimetype="text/xml")

    # ── Answer to a pending question ────────────────────────────────────────
    if from_number in sessions:
        q = sessions[from_number]["question"]
        name = sessions[from_number].get("name", "")
        thanks = f"Thanks for your answer, {name}!\n\n" if name else "Thanks for your answer!\n\n"

        if body not in ("A", "B", "C", "D"):
            resp.message("Please reply with A, B, C, or D.")
            return Response(str(resp), mimetype="text/xml")

        correct = q["correct"]
        if body == correct:
            feedback = f"{thanks}✅ Correct!\n\n{q['explanation']}"
        else:
            feedback = (
                f"{thanks}❌ Incorrect. The correct answer is "
                f"{correct}) {q['options'][correct]}.\n\n"
                f"{q['explanation']}"
            )

        log_event(
            "answer", from_number, name,
            question=q["question"], answer=body, correct_answer=correct,
            is_correct=body == correct,
        )
        del sessions[from_number]
        resp.message(feedback)
        return Response(str(resp), mimetype="text/xml")

    # ── HELP ─────────────────────────────────────────────────────────────────
    if body == "HELP":
        resp.message(
            f"{BRAND_NAME}: Daily medical quiz via SMS. Msg & data rates may apply, "
            "~1 msg/day. Reply STOP to unsubscribe."
        )
        return Response(str(resp), mimetype="text/xml")

    # ── Opt-in (new subscriber) or on-demand question request ─────────────────
    if body in OPT_IN_KEYWORDS or body in ON_DEMAND_KEYWORDS:
        recipients = load_recipients()
        name = recipients.get(from_number, "")
        if body in OPT_IN_KEYWORDS:
            log_event("opt_in", from_number, name, keyword=body)
            resp.message(
                f"{BRAND_NAME}: You are now opted-in to receive daily medical quiz "
                "questions via SMS. For help, reply HELP. To opt out at any time, "
                "reply STOP. Msg & data rates may apply."
            )
        if _questions is None:
            resp.message("Still loading, try again in a moment! ⏳")
        else:
            q = random.choice(_questions)
            sessions[from_number] = {"question": q, "name": name}
            log_event("question_sent", from_number, name, source="on_demand", question=q["question"], q=q)
            msg = resp.message(f"📚 Here's a question!\n\n{format_question(q)}")
            image_url = get_image_url_for_question(q)
            if image_url:
                msg.media(image_url)
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

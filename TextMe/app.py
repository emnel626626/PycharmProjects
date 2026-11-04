import os
import io
import csv
import html
import json
import threading
import random
import time
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, request, Response, send_from_directory
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient
from twilio.request_validator import RequestValidator
from apscheduler.schedulers.background import BackgroundScheduler
import anthropic
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# Config
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
VALIDATE_TWILIO_SIGNATURE = os.getenv("VALIDATE_TWILIO_SIGNATURE", "true").lower() == "true"
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


_recipients_lock = threading.Lock()


def save_recipient(phone: str, name: str) -> None:
    """Add a recipient, or update their name, keeping comments and order."""
    with _recipients_lock:
        lines = []
        if os.path.exists(RECIPIENTS_FILE):
            with open(RECIPIENTS_FILE) as f:
                lines = f.read().splitlines()
        new_line = f"{phone}, {name}".rstrip(", ") if name else phone
        for i, line in enumerate(lines):
            if not line.strip().startswith("#") and line.split(",")[0].strip() == phone:
                lines[i] = new_line
                break
        else:
            lines.append(new_line)
        with open(RECIPIENTS_FILE, "w") as f:
            f.write("\n".join(lines) + "\n")


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


def latest_of(phone: str, kinds: tuple) -> str | None:
    """The most recent of the given event kinds for this phone, if any."""
    latest = None
    for e in load_events():
        if e["phone"] == phone and e["event"] in kinds:
            latest = e["event"]
    return latest


def awaiting_name(phone: str) -> bool:
    return latest_of(phone, ("joined", "named")) == "joined"


def on_leaderboard(phone: str) -> bool:
    # Everyone is on the board unless they texted BOARD OFF.
    return latest_of(phone, ("board_on", "board_off")) != "board_off"


def last_question_for(phone: str) -> dict | None:
    if phone in sessions:
        return sessions[phone]["question"]
    by_text = {q["question"]: q for q in _questions or []}
    last = None
    for e in load_events():
        if e["phone"] == phone and e["event"] == "question_sent":
            last = e.get("q") or by_text.get(e["question"]) or last
    return last


def opted_out_numbers() -> set:
    # A number's latest opt event wins, so texting START after STOP re-subscribes.
    status = {}
    for e in load_events():
        if e["event"] in ("opt_in", "opt_out"):
            status[e["phone"]] = e["event"]
    return {phone for phone, s in status.items() if s == "opt_out"}


# ---------------------------------------------------------------------------
# Question generation — General Surgery, weighted by the ABSITE content outline
# ---------------------------------------------------------------------------

# Categories and target weights (%) from the ABS "General Surgery Content Outline
# for the ABSITE" (updated 01/2021): https://www.absurgery.org/wp-content/uploads/2023/01/GS-ITE.pdf
ABSITE_TOPICS = [
    ("Abdomen - General", 3), ("Abdomen - Hernia", 2.5), ("Abdomen - Biliary", 3.5),
    ("Abdomen - Liver", 3.5), ("Abdomen - Pancreas", 3.5), ("Abdomen - Spleen", 1),
    ("Esophagus", 2.5), ("Stomach", 3.5), ("Small Intestine", 3), ("Large Intestine", 3.5),
    ("Anorectal", 2), ("Endoscopy", 2), ("Breast", 4), ("Endocrine", 4),
    ("Skin and Soft Tissue", 3.5), ("Surgical Critical Care", 5), ("Trauma", 6),
    ("Vascular - Arterial Disease", 2), ("Vascular - Venous", 1), ("Vascular - Access", 1),
    ("Transplantation", 2), ("Thoracic Surgery", 2), ("Pediatric Surgery", 2),
    ("Plastic Surgery", 2), ("Genitourinary", 1), ("Gynecology", 1), ("Head and Neck", 1),
    ("Nervous System", 1),
    ("Anesthesia", 1), ("Biostatistics and Evaluation of Evidence", 1),
    ("Fluids, Electrolytes and Acid-Base Balance", 3.5),
    ("Geriatric Surgery and End-of-Life Care", 1.5), ("Immunology", 1),
    ("Infection and Antimicrobial Therapy", 3), ("Minimally Invasive Surgery - Principles", 1),
    ("Nutrition and Metabolism", 2), ("Oncology and Tumor Biology", 2), ("Pharmacology", 1),
    ("Preoperative Evaluation and Perioperative Care", 4),
    ("Transfusion and Disorders of Coagulation", 2), ("Wound Healing", 1),
    ("Radiology", 1), ("Outcomes", 2), ("Ethics", 1),
]

CLAUDE_MODEL = "claude-opus-5-5"
# On a safety decline, the API retries the same request on another model instead of failing.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_GENERATION_ROUNDS = 3

QUESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "question": {"type": "string"},
                    "options": {
                        "type": "object",
                        "properties": {k: {"type": "string"} for k in "ABCD"},
                        "required": list("ABCD"),
                        "additionalProperties": False,
                    },
                    "correct": {"type": "string", "enum": list("ABCD")},
                    "explanation": {"type": "string"},
                },
                "required": ["topic", "question", "options", "correct", "explanation"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["questions"],
    "additionalProperties": False,
}

CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "enum": list("ABCD")},
        "single_best_answer": {"type": "boolean"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "issue": {"type": "string"},
    },
    "required": ["answer", "single_best_answer", "confidence", "issue"],
    "additionalProperties": False,
}


def ask_claude_json(prompt: str, schema: dict, effort: str) -> dict:
    # Streamed so long generations don't sit on an idle connection until it drops
    with claude.beta.messages.stream(
        model=CLAUDE_MODEL,
        max_tokens=32000,
        betas=[FALLBACK_BETA],
        fallbacks="default",
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        response = stream.get_final_message()
    if response.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined the request ({getattr(response.stop_details, 'category', None)})")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Claude's reply was cut off (max_tokens)")
    return json.loads(next(b.text for b in response.content if b.type == "text"))


def pick_topics(n: int) -> list:
    """Weighted sample of ABSITE categories, without repeats until all have been used."""
    picked = []
    while len(picked) < n:
        # Efraimidis-Spirakis weighted sampling without replacement
        ranked = sorted(ABSITE_TOPICS, key=lambda t: random.random() ** (1 / t[1]), reverse=True)
        picked += [name for name, _ in ranked[: n - len(picked)]]
    return picked


def generate_questions(topics: list, avoid: list) -> list:
    avoid_text = ""
    if avoid:
        avoid_text = (
            "\nDo NOT repeat, reword, or test the same fact as any of these existing questions:\n"
            + "\n".join(f"- {a}" for a in avoid) + "\n"
        )
    topic_list = "\n".join(f"{i}. {t}" for i, t in enumerate(topics, 1))
    prompt = (
        "You are writing practice questions for general surgery residents preparing for the "
        "ABSITE (American Board of Surgery In-Training Examination).\n\n"
        f"Write exactly {len(topics)} single-best-answer multiple-choice questions, one for each "
        "ABSITE content category below, in this order. Set each question's \"topic\" to its category.\n\n"
        f"{topic_list}\n\n"
        "Match the ABSITE's style and level:\n"
        "- About 4 in 5 should be clinical management vignettes (a patient, findings, what to do "
        "next or the best treatment); the rest applied science (anatomy, physiology, pathophysiology).\n"
        "- Test current, guideline-based practice that a chief resident is expected to know. Avoid "
        "trivia, controversial areas, and anything that depends on an institution's preference.\n"
        "- Exactly one option must be clearly best; the other three plausible but wrong.\n"
        "- Spread the correct letter across A, B, C and D.\n\n"
        "These are sent by text message, so keep them short:\n"
        "- Question stem under 350 characters; each option under 90.\n"
        "- Explanation under 450 characters: talk to the resident directly, say why the answer is "
        "right and briefly why the main distractor is wrong. Don't refer to 'the question' or 'the text'.\n"
        "- Plain text only, no markdown.\n"
        f"{avoid_text}"
    )
    return ask_claude_json(prompt, QUESTION_SCHEMA, effort="high")["questions"]


def question_passes_check(q: dict) -> bool:
    """Have Claude answer the question cold; keep it only if it agrees with the key and is unambiguous."""
    options = "\n".join(f"{k}) {v}" for k, v in q["options"].items())
    prompt = (
        "You are an attending general surgeon reviewing an ABSITE practice question before it goes "
        "to residents. Answer it yourself, then judge it.\n\n"
        f"{q['question']}\n\n{options}\n\n"
        "Give the single best answer. Set single_best_answer to false if more than one option could "
        "reasonably be defended, the stem is missing something needed to decide, or it rests on "
        "outdated practice. Set confidence to how sure you are of your answer under current "
        "guidelines. Put any problem in issue (empty string if none)."
    )
    check = ask_claude_json(prompt, CHECK_SCHEMA, effort="high")
    passed = check["answer"] == q["correct"] and check["single_best_answer"] and check["confidence"] == "high"
    if not passed:
        print(f"[Quiz] Dropped a {q['topic']} question (key {q['correct']}, reviewer {check['answer']}, "
              f"single best={check['single_best_answer']}, confidence={check['confidence']}): {check['issue'][:120]}")
    return passed


def generate_checked_questions(n: int, avoid: list) -> list:
    """Generate n questions that pass the re-check, retrying dropped ones on fresh topics."""
    kept = []
    for _ in range(MAX_GENERATION_ROUNDS):
        need = n - len(kept)
        if need <= 0:
            break
        # Ask for a few extra so dropped questions don't leave the batch short
        topics = pick_topics(need + max(1, need // 3))
        print(f"[Quiz] Generating {len(topics)} General Surgery questions...")
        candidates = generate_questions(topics, avoid + [q["question"] for q in kept])
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(question_passes_check, candidates))
        for q, ok in zip(candidates, results):
            if ok and len(kept) < n:
                q["image"] = None
                kept.append(q)
        print(f"[Quiz] {sum(results)} of {len(candidates)} passed the re-check; {len(kept)}/{n} kept.")
    if not kept:
        raise RuntimeError("No questions passed the re-check")
    return kept


def save_questions() -> None:
    with open(QUESTIONS_CACHE_FILE, "w") as f:
        json.dump({"questions": _questions, "page_image_map": _page_image_map}, f, indent=2)


def add_more_questions() -> None:
    """Append a fresh batch so nobody starts repeating questions."""
    print(f"[Quiz] Someone reached the end of the list — generating {NUM_QUESTIONS} more questions...")
    _questions.extend(generate_checked_questions(NUM_QUESTIONS, avoid=[q["question"] for q in _questions]))
    save_questions()
    print(f"[Quiz] Now {len(_questions)} questions.")


def preload_questions() -> None:
    # Retry so a temporary API or network problem at startup doesn't leave the app with no questions
    for attempt in range(1, 6):
        if load_or_generate_questions():
            return
        print(f"[Quiz] Startup attempt {attempt} failed; retrying in 2 minutes...")
        time.sleep(120)


def load_or_generate_questions() -> bool:
    global _questions, _questions_error, _page_image_map
    try:
        if os.path.exists(QUESTIONS_CACHE_FILE):
            print("[Quiz] Loading questions from cache (delete questions_cache.json to regenerate)...")
            with open(QUESTIONS_CACHE_FILE) as f:
                cache = json.load(f)
            _questions = cache["questions"]
            _page_image_map = cache.get("page_image_map", {})
        else:
            _questions = generate_checked_questions(NUM_QUESTIONS, avoid=[])
            save_questions()
        print(f"[Quiz] Ready — {len(_questions)} questions loaded.")
        restore_sessions()
        if SEND_ON_START:
            print("[Quiz] Sending startup questions to all recipients...")
            send_daily_questions()
        return True
    except Exception as exc:
        _questions_error = str(exc)
        print(f"[Quiz] Error: {exc}")
        return False


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

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
# First words that are commands, so they're never mistaken for a new subscriber's name
ALL_KEYWORDS = OPT_IN_KEYWORDS + OPT_OUT_KEYWORDS + ON_DEMAND_KEYWORDS + (
    "HELP", "FLAG", "BOARD", "LEADERBOARD", "RANK", "A", "B", "C", "D",
)
LEADERBOARD_SIZE = 5

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
    "joined": "Joined (new number)", "named": "Gave name", "flag": "🚩 Flagged",
    "board_on": "Joined leaderboard", "board_off": "Left leaderboard",
}


def build_flags(events: list) -> list:
    flags: dict = {}
    for e in events:
        if e["event"] == "flag":
            f = flags.setdefault(e["question"], {"question": e["question"], "count": 0, "notes": [], "last": ""})
            f["count"] += 1
            f["last"] = e["ts"]
            if e.get("note"):
                f["notes"].append(f"{e.get('name') or e['phone']}: {e['note']}")
    return sorted(flags.values(), key=lambda f: (f["count"], f["last"]), reverse=True)


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
        if e["event"] == "flag":
            return f"{esc(e.get('note') or 'No note')}<br><small>{esc(e['question'])}</small>"
        if e.get("keyword"):
            return f"Keyword: {esc(e['keyword'])}"
        return ""

    board_html = "".join(
        f"<tr><td class=n>{rank}</td><td>{esc(p['name']) or 'Someone'}</td>"
        f"<td class=n>{p['correct']}</td><td class=n>{p['answered']}</td><td class=n>{p['accuracy']}%</td></tr>"
        for rank, p in enumerate(leaderboard(people), 1)
    ) or "<tr><td colspan=5 class=empty>No answers yet.</td></tr>"

    flags_html = "".join(
        f"<tr><td>{esc(f['question'])}</td><td class=n>{f['count']}</td>"
        f"<td>{'<br>'.join(esc(n) for n in f['notes']) or '—'}</td><td>{esc(f['last'])}</td></tr>"
        for f in build_flags(events)
    ) or "<tr><td colspan=4 class=empty>No flagged questions.</td></tr>"

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

  <h2>🏆 Leaderboard</h2>
  <p><small>Everyone is included unless they texted BOARD OFF. They can see the top {LEADERBOARD_SIZE} by texting BOARD.</small></p>
  <div class="scroll"><table>
    <tr><th>#</th><th>Name</th><th>Correct</th><th>Answered</th><th>Accuracy</th></tr>
    {board_html}
  </table></div>

  <h2>🚩 Flagged questions</h2>
  <div class="scroll"><table>
    <tr><th>Question</th><th>Flags</th><th>Notes</th><th>Last flagged</th></tr>
    {flags_html}
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


def send_picture_test() -> str:
    """Send everyone a question with an approved picture, without advancing their daily order."""
    pool = [q for q in _questions or [] if q.get("image")]
    if not pool or not NGROK_URL:
        return "No questions with an approved picture."

    opted_out = opted_out_numbers()
    sent = []
    for phone, name in load_recipients().items():
        if phone in opted_out:
            continue
        q = random.choice(pool)
        greeting = f"Hi {name}! " if name else ""
        try:
            twilio.messages.create(
                to=phone, from_=TWILIO_PHONE_NUMBER,
                body=f"📚 {greeting}Picture question!\n\n{format_question(q)}",
                media_url=[f"{NGROK_URL}/images/{q['image']}"],
            )
            sessions[phone] = {"question": q, "name": name}
            log_event("question_sent", phone, name, source="picture_test", question=q["question"], q=q)
            sent.append(name or phone)
        except Exception as exc:
            print(f"[Picture test] Failed to send to {phone}: {exc}")
    return f"Sent a picture question to: {', '.join(sent) or 'nobody'}"


@app.route("/send-now", methods=["POST"])
def send_now():
    if not report_allowed():
        return Response("Not found", status=404)
    if request.args.get("picture"):
        return send_picture_test()
    send_daily_questions()
    return "Sent — see /report"


@app.route("/report.csv")
def report_csv():
    if not report_allowed():
        return Response("Not found", status=404)

    out = io.StringIO()
    fields = ["ts", "event", "phone", "name", "keyword", "source", "question", "answer", "correct_answer", "is_correct", "note"]
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


def twilio_request_valid() -> bool:
    if not VALIDATE_TWILIO_SIGNATURE:
        return True
    # Twilio signs the public URL it called, not the localhost URL Flask sees.
    url = f"{NGROK_URL}{request.full_path.rstrip('?')}" if NGROK_URL else request.url
    return RequestValidator(TWILIO_AUTH_TOKEN).validate(
        url, request.form.to_dict(), request.headers.get("X-Twilio-Signature", "")
    )


def clean_name(raw: str) -> str | None:
    words = raw.replace(".", " ").replace(",", " ").replace("!", " ").split()[:2]
    name = " ".join(words)[:30]
    if len(name) < 2 or any(ch.isdigit() for ch in name):
        return None
    return name.title() if name.islower() or name.isupper() else name


def send_question_reply(resp: MessagingResponse, phone: str, name: str, intro: str) -> None:
    if _questions is None:
        resp.message("Still loading, try again in a moment! ⏳")
        return
    q = random.choice(_questions)
    sessions[phone] = {"question": q, "name": name}
    log_event("question_sent", phone, name, source="on_demand", question=q["question"], q=q)
    msg = resp.message(f"{intro}\n\n{format_question(q)}")
    image_url = get_image_url_for_question(q)
    if image_url:
        msg.media(image_url)


def leaderboard(people: list) -> list:
    """Everyone who has answered and hasn't left the board or opted out, best first."""
    board = [p for p in people if on_leaderboard(p["phone"]) and p["answered"] and p["status"] != "Opted out"]
    return sorted(board, key=lambda p: (p["correct"], p["accuracy"] or 0), reverse=True)


def leaderboard_text(phone: str) -> str:
    people, _ = build_report(load_events())
    board = leaderboard(people)

    lines = ["🏆 Leaderboard (correct answers)"]
    for rank, p in enumerate(board[:LEADERBOARD_SIZE], 1):
        lines.append(f"{rank}. {p['name'] or 'Someone'} - {p['correct']} ({p['accuracy']}%)")
    if not board:
        lines.append("No one is on the board yet.")

    my_rank = next((i for i, p in enumerate(board, 1) if p["phone"] == phone), None)
    if my_rank:
        lines.append(f"\nYou're #{my_rank} of {len(board)}. Text BOARD OFF to leave.")
    elif on_leaderboard(phone):
        lines.append("\nYou'll show up once you answer a question.")
    else:
        lines.append("\nYou're hidden from the board. Text BOARD ON to rejoin.")
    return "\n".join(lines)


def reply(resp: MessagingResponse) -> Response:
    return Response(str(resp), mimetype="text/xml")


@app.route("/webhook", methods=["POST"])
def webhook():
    if not twilio_request_valid():
        print(f"[Webhook] Rejected request with a bad Twilio signature from {request.remote_addr}")
        return Response("Forbidden", status=403)

    from_number = strip_whatsapp(request.form.get("From", ""))
    raw_body = request.form.get("Body", "").strip()
    body = raw_body.upper()
    resp = MessagingResponse()
    name = load_recipients().get(from_number, "")

    # ── Opt-out (Twilio sends the STOP reply itself; we just record it) ───────
    if body in OPT_OUT_KEYWORDS:
        sessions.pop(from_number, None)
        log_event("opt_out", from_number, name, keyword=body)
        return reply(resp)

    # ── New subscriber replying with their first name ─────────────────────────
    if (awaiting_name(from_number) and from_number not in sessions
            and body.split(" ")[0] not in ALL_KEYWORDS):
        new_name = clean_name(raw_body)
        if not new_name:
            resp.message("Just reply with your first name 🙂")
            return reply(resp)
        save_recipient(from_number, new_name)
        log_event("named", from_number, new_name)
        send_question_reply(
            resp, from_number, new_name,
            f"Thanks, {new_name}! You'll get a new question every day at "
            f"{SEND_HOUR % 12 or 12}:{SEND_MINUTE:02d} {'AM' if SEND_HOUR < 12 else 'PM'}. "
            "Here's your first one:",
        )
        return reply(resp)

    # ── HELP ─────────────────────────────────────────────────────────────────
    if body == "HELP":
        resp.message(
            f"{BRAND_NAME}: Daily medical quiz via SMS. Text QUIZ for a question now, "
            "FLAG to report a question that seems wrong, BOARD for the leaderboard. "
            "Msg & data rates may apply, ~1 msg/day. Reply STOP to unsubscribe."
        )
        return reply(resp)

    # ── FLAG a question that seems wrong ──────────────────────────────────────
    if body.split(" ")[0] == "FLAG":
        q = last_question_for(from_number)
        if not q:
            resp.message("There's no question to flag yet.")
            return reply(resp)
        note = raw_body[4:].strip(" :-")
        log_event("flag", from_number, name, question=q["question"], note=note)
        resp.message(
            f"🚩 Thanks{', ' + name if name else ''}! That question is flagged for review."
            + ("\n\nYou can still answer it with A, B, C, or D." if from_number in sessions else "")
        )
        return reply(resp)

    # ── Leaderboard ──────────────────────────────────────────────────────────
    if body in ("BOARD ON", "LEADERBOARD ON"):
        log_event("board_on", from_number, name)
        resp.message("You're on the leaderboard!\n\n" + leaderboard_text(from_number))
        return reply(resp)
    if body in ("BOARD OFF", "LEADERBOARD OFF"):
        log_event("board_off", from_number, name)
        resp.message("You've been removed from the leaderboard. Text BOARD ON to rejoin.")
        return reply(resp)
    if body in ("BOARD", "LEADERBOARD", "RANK"):
        resp.message(leaderboard_text(from_number))
        return reply(resp)

    # ── Answer to a pending question ────────────────────────────────────────
    if from_number in sessions:
        q = sessions[from_number]["question"]
        name = sessions[from_number].get("name", "") or name
        thanks = f"Thanks for your answer, {name}!\n\n" if name else "Thanks for your answer!\n\n"

        if body not in ("A", "B", "C", "D"):
            resp.message("Please reply with A, B, C, or D.")
            return reply(resp)

        correct = q["correct"]
        if body == correct:
            feedback = f"{thanks}✅ Correct!\n\n{q['explanation']}"
        else:
            feedback = (
                f"{thanks}❌ Incorrect. The correct answer is "
                f"{correct}) {q['options'][correct]}.\n\n"
                f"{q['explanation']}"
            )
        feedback += "\n\nThink this question is wrong? Reply FLAG."

        log_event(
            "answer", from_number, name,
            question=q["question"], answer=body, correct_answer=correct,
            is_correct=body == correct,
        )
        del sessions[from_number]
        resp.message(feedback)
        return reply(resp)

    # ── Opt-in ───────────────────────────────────────────────────────────────
    if body in OPT_IN_KEYWORDS:
        log_event("opt_in", from_number, name, keyword=body)
        resp.message(
            f"{BRAND_NAME}: You are now opted-in to receive daily medical quiz "
            "questions via SMS. For help, reply HELP. To opt out at any time, "
            "reply STOP. Msg & data rates may apply."
        )
        if from_number not in load_recipients():
            # New number: add them to the daily list, then get their name.
            save_recipient(from_number, "")
            log_event("joined", from_number)
            resp.message("What's your first name? Reply with it and we'll send your first question.")
        else:
            send_question_reply(resp, from_number, name, "📚 Here's a question!")
        return reply(resp)

    # ── On-demand question ───────────────────────────────────────────────────
    if body in ON_DEMAND_KEYWORDS:
        send_question_reply(resp, from_number, name, "📚 Here's a question!")
        return reply(resp)

    # ── Default ───────────────────────────────────────────────────────────────
    resp.message(
        "📚 You'll get a daily question automatically!\n"
        "Text QUIZ for one now, BOARD for the leaderboard, or HELP for more."
    )
    return reply(resp)


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

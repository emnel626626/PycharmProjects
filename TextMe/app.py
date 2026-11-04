import os
import io
import base64
import hashlib
import re
import urllib.parse
import urllib.request
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
PICTURE_QUESTIONS_PER_BATCH = int(os.getenv("PICTURE_QUESTIONS_PER_BATCH", "2"))
QUESTIONS_PER_DAY = int(os.getenv("QUESTIONS_PER_DAY", "3"))
SEND_ON_START = os.getenv("SEND_ON_START", "true").lower() == "true"

os.makedirs(IMAGE_DIR, exist_ok=True)

claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
twilio = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# Global state
_questions: list | None = None
_questions_error: str | None = None
_page_image_map: dict = {}   # page_key -> [image filenames]
# phone -> {"name": str, "open": {"1": question, "2": question, ...}} (unanswered, by label)
sessions: dict = {}
_generation_lock = threading.Lock()


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


def open_questions(phone: str) -> dict:
    return sessions.get(phone, {}).get("open", {})


def open_question(phone: str, name: str, label: str, q: dict) -> None:
    session = sessions.setdefault(phone, {"name": name, "open": {}, "count": 0})
    session["name"] = name or session["name"]
    session["open"][label] = q
    session["count"] = max(session.get("count", 0), int(label))


def next_label(phone: str) -> str:
    return str(sessions.get(phone, {}).get("count", 0) + 1)


def labels_text(phone: str) -> str:
    return ", ".join(f"Q{label}" for label in sorted(open_questions(phone), key=int))


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
        phone = e["phone"]
        if e["event"] == "question_sent":
            # Newer events carry the full question; older ones are looked up by text.
            q = e.get("q") or by_text.get(e["question"])
            # A new day's set (or an old one-at-a-time event with no label) replaces what was open
            if e.get("new_day") or "label" not in e:
                pending[phone] = {"name": e.get("name", ""), "open": {}, "count": 0}
            if q:
                session = pending.setdefault(phone, {"name": e.get("name", ""), "open": {}, "count": 0})
                label = e.get("label", "1")
                session["open"][label] = q
                session["count"] = max(session["count"], int(label))
        elif e["event"] == "answer":
            if "label" in e:
                pending.get(phone, {}).get("open", {}).pop(e["label"], None)
            else:
                pending.pop(phone, None)
        elif e["event"] == "opt_out":
            pending.pop(phone, None)
    for phone, session in pending.items():
        sessions.setdefault(phone, session)
    waiting = {p: s for p, s in pending.items() if s["open"]}
    if waiting:
        names = ", ".join(f"{s['name'] or phone} ({len(s['open'])})" for phone, s in waiting.items())
        print(f"[Quiz] Restored unanswered questions: {names}")


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


def last_question_for(phone: str, label: str | None = None) -> dict | None:
    open_qs = open_questions(phone)
    if label:
        return open_qs.get(label)
    if open_qs:
        return open_qs[max(open_qs, key=int)]
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


def ask_claude_json(prompt: str | list, schema: dict, effort: str) -> dict:
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


def question_passes_check(q: dict, image: tuple | None = None) -> bool:
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
    content = [image_block(*image), {"type": "text", "text": prompt}] if image else prompt
    check = ask_claude_json(content, CHECK_SCHEMA, effort="high")
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


# ---------------------------------------------------------------------------
# Picture questions — freely licensed images from Wikimedia Commons
# ---------------------------------------------------------------------------

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
HTTP_HEADERS = {"User-Agent": "TextMeQuiz/1.0 (educational SMS quiz for surgery residents)"}
MAX_IMAGE_BYTES = 3_000_000

QUESTION_ITEM_SCHEMA = QUESTION_SCHEMA["properties"]["questions"]["items"]

IDEAS_SCHEMA = {
    "type": "object",
    "properties": {
        "ideas": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "finding": {"type": "string"},
                    "search_query": {"type": "string"},
                },
                "required": ["topic", "finding", "search_query"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["ideas"],
    "additionalProperties": False,
}

IMAGE_CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "shows_finding": {"type": "boolean"},
        "clinical_image": {"type": "boolean"},
        "answer_visible": {"type": "boolean"},
        "description": {"type": "string"},
    },
    "required": ["shows_finding", "clinical_image", "answer_visible", "description"],
    "additionalProperties": False,
}


def image_block(data: bytes, mime: str) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": mime,
                                         "data": base64.standard_b64encode(data).decode()}}


def free_license(name: str) -> bool:
    """Public domain, CC0, CC BY or CC BY-SA. No NonCommercial or NoDerivatives."""
    lic = name.lower()
    if re.search(r"\b(nc|nd)\b", lic):
        return False
    return lic.startswith(("public domain", "pd", "cc0", "cc by"))


def commons_images(query: str, limit: int = 8) -> list:
    params = urllib.parse.urlencode({
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": f"{query} filetype:bitmap", "gsrnamespace": 6, "gsrlimit": limit,
        "prop": "imageinfo", "iiprop": "url|mime|extmetadata", "iiurlwidth": 800,
    })
    req = urllib.request.Request(f"{COMMONS_API}?{params}", headers=HTTP_HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        pages = json.load(r).get("query", {}).get("pages", {})
    results = []
    for page in sorted(pages.values(), key=lambda p: p.get("index", 0)):
        info = (page.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata", {})
        license_name = meta.get("LicenseShortName", {}).get("value", "")
        if info.get("mime") not in ("image/jpeg", "image/png") or not free_license(license_name):
            continue
        artist = html.unescape(re.sub(r"<[^>]+>", "", meta.get("Artist", {}).get("value", ""))).strip()
        artist = re.sub(r"\s*\d{1,2}:\d{2}, \d{1,2} \w+ \d{4} \(UTC\)", "", artist).strip()
        results.append({
            "url": info.get("thumburl") or info["url"],
            "mime": info["mime"],
            "license": license_name,
            "artist": (artist or "Unknown author")[:40],
            "page": info.get("descriptionshorturl") or info.get("descriptionurl", ""),
        })
    return results


def download(url: str) -> bytes:
    req = urllib.request.Request(url, headers=HTTP_HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("image too large")
    return data


def find_commons_image(idea: dict) -> tuple | None:
    """First freely licensed Commons image that Claude confirms clearly shows the finding."""
    for cand in commons_images(idea["search_query"]):
        try:
            data = download(cand["url"])
        except Exception:
            continue
        check = ask_claude_json([
            image_block(data, cand["mime"]),
            {"type": "text", "text": (
                f"This image may be used in a surgery quiz question about: {idea['finding']}.\n"
                "shows_finding: does it clearly and typically show that finding?\n"
                "clinical_image: is it a real clinical image (radiograph, CT, MRI, ultrasound, endoscopy, "
                "operative or clinical photo, histology) of usable quality, not a diagram or drawing?\n"
                "answer_visible: does any text, label, arrow caption or annotation in the image name the "
                "diagnosis or give away the answer?\n"
                "description: one sentence on what the image shows."
            )},
        ], IMAGE_CHECK_SCHEMA, effort="medium")
        if check["shows_finding"] and check["clinical_image"] and not check["answer_visible"]:
            return cand, data
    return None


def write_picture_question(idea: dict, data: bytes, mime: str) -> dict:
    prompt = (
        "You are writing a practice question for general surgery residents preparing for the ABSITE.\n\n"
        f"Write one single-best-answer multiple-choice question built around this image, which shows "
        f"{idea['finding']}. ABSITE category: {idea['topic']} (set \"topic\" to it).\n"
        "- The stem is a short clinical vignette that refers to 'the image' and needs it to answer. "
        "Don't name the finding in the stem.\n"
        "- Ask for the diagnosis or, better, the best next step in management.\n"
        "- Exactly one option must be clearly best; the other three plausible but wrong.\n"
        "- Question stem under 300 characters; each option under 90.\n"
        "- Explanation under 450 characters: say what the image shows, why the answer is right, and "
        "briefly why the main distractor is wrong. Plain text only, no markdown."
    )
    return ask_claude_json([image_block(data, mime), {"type": "text", "text": prompt}],
                           QUESTION_ITEM_SCHEMA, effort="high")


def generate_picture_questions(n: int, avoid: list) -> list:
    """Up to n picture questions with Commons images, each re-checked with the image."""
    if n <= 0:
        return []
    avoid_text = ("\nAvoid findings already covered by these questions:\n"
                  + "\n".join(f"- {a}" for a in avoid) + "\n") if avoid else ""
    ideas = ask_claude_json(
        f"Suggest {n * 3} different classic findings a general surgery resident should recognize on an "
        "image for the ABSITE (plain films, CT, ultrasound, contrast studies, endoscopy, clinical photos), "
        "spread across these ABSITE categories:\n"
        + "\n".join(f"- {t}" for t in sorted(set(pick_topics(n * 3))))
        + "\n\nFor each, give its category as topic, the finding, and a 2-5 word Wikimedia Commons search "
        "query likely to return a real clinical image of it (e.g. 'pneumoperitoneum chest x-ray', "
        "'sigmoid volvulus x-ray').\n" + avoid_text,
        IDEAS_SCHEMA, effort="medium",
    )["ideas"]

    kept = []
    for idea in ideas:
        if len(kept) >= n:
            break
        try:
            found = find_commons_image(idea)
            if not found:
                print(f"[Pictures] No usable free image for: {idea['finding']}")
                continue
            cand, data = found
            q = write_picture_question(idea, data, cand["mime"])
            if not question_passes_check(q, image=(data, cand["mime"])):
                continue
            ext = "png" if cand["mime"] == "image/png" else "jpg"
            filename = f"gs_{hashlib.sha1(cand['url'].encode()).hexdigest()[:12]}.{ext}"
            with open(os.path.join(IMAGE_DIR, filename), "wb") as f:
                f.write(data)
            q["image"] = filename
            q["image_credit"] = f"Image: {cand['artist']}, {cand['license']}, Wikimedia Commons"
            q["image_source"] = cand["page"]
            kept.append(q)
            print(f"[Pictures] Added {idea['topic']} picture question: {idea['finding']}")
        except Exception as exc:
            print(f"[Pictures] Skipped {idea.get('finding')}: {exc}")
    return kept


def spread(questions: list, extras: list) -> list:
    """Place extras evenly through questions so picture questions don't bunch up."""
    merged = list(questions)
    step = max(1, len(merged) // (len(extras) + 1))
    for i, q in enumerate(extras):
        merged.insert(min(len(merged), (i + 1) * step + i), q)
    return merged


def build_batch(n: int, avoid: list) -> list:
    """n new questions: up to PICTURE_QUESTIONS_PER_BATCH with pictures, the rest text."""
    try:
        pictures = generate_picture_questions(min(PICTURE_QUESTIONS_PER_BATCH, n), avoid)
    except Exception as exc:
        print(f"[Pictures] Picture questions failed, continuing with text only: {exc}")
        pictures = []
    texts = generate_checked_questions(n - len(pictures), avoid + [q["question"] for q in pictures])
    return spread(texts, pictures)


def save_questions() -> None:
    with open(QUESTIONS_CACHE_FILE, "w") as f:
        json.dump({"questions": _questions, "page_image_map": _page_image_map}, f, indent=2)


def add_more_questions(wait: bool = True) -> None:
    """Append a fresh batch so nobody starts repeating questions."""
    if not _generation_lock.acquire(blocking=wait):
        return  # a batch is already being generated
    try:
        print(f"[Quiz] Running low on questions — generating {NUM_QUESTIONS} more...")
        _questions.extend(build_batch(NUM_QUESTIONS, avoid=[q["question"] for q in _questions]))
        save_questions()
        print(f"[Quiz] Now {len(_questions)} questions.")
    except Exception as exc:
        print(f"[Quiz] Could not generate more questions: {exc}")
    finally:
        _generation_lock.release()


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
            _questions = build_batch(NUM_QUESTIONS, avoid=[])
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
    """Return a public URL to the question's image, if it has one. Picture questions need it to be answerable."""
    if not NGROK_URL or not q.get("image"):
        return None
    return f"{NGROK_URL}/images/{q['image']}"


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_question(q: dict, with_image: bool = False, label: str | None = None) -> str:
    # Commons licenses (CC BY / BY-SA) require crediting the image wherever it's shown
    credit = f"\n\n{q['image_credit']}" if with_image and q.get("image_credit") else ""
    prefix = f"Q{label}: " if label else ""
    how = f"Reply Q{label} + A, B, C, or D (e.g. Q{label} B)" if label else "Reply A, B, C, or D"
    return (
        f"{prefix}{q['question']}\n\n"
        f"A) {q['options']['A']}\n"
        f"B) {q['options']['B']}\n"
        f"C) {q['options']['C']}\n"
        f"D) {q['options']['D']}\n\n"
        f"{how}"
        f"{credit}"
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

    active = [phone for phone in recipients if phone not in opted_out]
    if any(progress.get(phone, 0) + QUESTIONS_PER_DAY > len(_questions) for phone in active):
        add_more_questions()  # falls back to repeating from the start if this fails

    n = QUESTIONS_PER_DAY
    for phone, name in recipients.items():
        if phone in opted_out:
            print(f"[Scheduler] Skipping {name or phone} (opted out)")

    # Each person's set goes out in its own thread, since waiting on delivery takes a while
    with ThreadPoolExecutor(max_workers=8) as pool:
        for phone in active:
            pool.submit(send_day_to, phone, recipients[phone], progress.get(phone, 0), n)
    for phone in active:
        progress[phone] = progress.get(phone, 0) + n

    save_progress(progress)

    # Write the next batch now, in the background, so tomorrow's send never waits on it
    if active and len(_questions) - max(progress.get(p, 0) for p in active) < 2 * n:
        threading.Thread(target=add_more_questions, kwargs={"wait": False}, daemon=True).start()


def wait_for_delivery(sid: str, timeout: float = 45) -> None:
    """Block until a message is delivered (or fails). Picture messages travel slower than plain
    texts, so without this a plain Q2 can overtake a picture Q1."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if twilio.messages(sid).fetch().status in ("delivered", "undelivered", "failed"):
            return
        time.sleep(2)


def send_day_to(phone: str, name: str, start: int, n: int) -> None:
    try:
        greeting = f"Hi {name}! " if name else ""
        to = to_whatsapp(phone) if USE_WHATSAPP else phone
        # Today's set replaces yesterday's unanswered questions
        sessions[phone] = {"name": name, "open": {}, "count": 0}

        for i in range(n):
            q = _questions[(start + i) % len(_questions)]
            label = str(i + 1)
            image_url = get_image_url_for_question(q)
            header = ""
            if i == 0:
                header = (f"📚 {greeting}Today's {n} questions! Answer any time, in any order: reply with "
                          "the number and letter, like Q1 A (or all at once: 1A 2C 3B).\n\n")
            body = header + format_question(q, bool(image_url), label)
            try:
                msg_params = dict(to=to, from_=get_from_number(phone), body=body)
                if image_url:
                    msg_params["media_url"] = [image_url]
                message = twilio.messages.create(**msg_params)
                open_question(phone, name, label, q)
                log_event("question_sent", phone, name, source="daily", label=label, new_day=i == 0,
                          question=q["question"], q=q)
                print(f"[Scheduler] Sent Q{label} (#{(start + i) % len(_questions) + 1}) to {name or phone}"
                      + (" (with image)" if image_url else ""))
                if i < n - 1:
                    wait_for_delivery(message.sid)  # keeps Q1, Q2, Q3 arriving in order
            except Exception as exc:
                print(f"[Scheduler] Failed to send Q{label} to {phone}: {exc}")
    except Exception as exc:
        print(f"[Scheduler] Failed to send today's questions to {phone}: {exc}")


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
        label = next_label(phone)
        try:
            twilio.messages.create(
                to=phone, from_=TWILIO_PHONE_NUMBER,
                body=f"📚 {greeting}Picture question!\n\n{format_question(q, True, label)}",
                media_url=[f"{NGROK_URL}/images/{q['image']}"],
            )
            open_question(phone, name, label, q)
            log_event("question_sent", phone, name, source="picture_test", label=label, question=q["question"], q=q)
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
    label = next_label(phone)
    open_question(phone, name, label, q)
    log_event("question_sent", phone, name, source="on_demand", label=label, question=q["question"], q=q)
    image_url = get_image_url_for_question(q)
    msg = resp.message(f"{intro}\n\n{format_question(q, bool(image_url), label)}")
    if image_url:
        msg.media(image_url)


def leaderboard(people: list) -> list:
    """Everyone who has answered and hasn't left the board or opted out, best first."""
    board = [p for p in people if on_leaderboard(p["phone"]) and p["answered"] and p["status"] != "Opted out"]
    return sorted(board, key=lambda p: (p["correct"], p["accuracy"] or 0), reverse=True)


def leaderboard_text(phone: str) -> str:
    people, _ = build_report(load_events())
    board = leaderboard(people)

    lines = ["🏆 Leaderboard"]
    for rank, p in enumerate(board[:LEADERBOARD_SIZE], 1):
        lines.append(f"{rank}. {p['name'] or 'Someone'} - {p['correct']}/{p['answered']} correct ({p['accuracy']}%)")
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


def send_time_text() -> str:
    return f"{SEND_HOUR % 12 or 12}:{SEND_MINUTE:02d} {'AM' if SEND_HOUR < 12 else 'PM'}"


ANSWER_RE = re.compile(r"\bQ?\s*(\d{1,2})\s*[=:.)\-]?\s*([ABCD])\b")


def parse_answers(body: str, open_qs: dict) -> list:
    """[(label, letter), ...] from 'Q1 A', '1A 2C', 'q3=d'; a bare letter answers the only open question."""
    pairs = ANSWER_RE.findall(body)
    if pairs:
        return [(str(int(label)), letter) for label, letter in pairs]
    if body in ("A", "B", "C", "D") and len(open_qs) == 1:
        return [(next(iter(open_qs)), body)]
    return []


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
    if (awaiting_name(from_number) and not open_questions(from_number)
            and body.split(" ")[0] not in ALL_KEYWORDS):
        new_name = clean_name(raw_body)
        if not new_name:
            resp.message("Just reply with your first name 🙂")
            return reply(resp)
        save_recipient(from_number, new_name)
        log_event("named", from_number, new_name)
        send_question_reply(
            resp, from_number, new_name,
            f"Thanks, {new_name}! You'll get {QUESTIONS_PER_DAY} new questions every day at "
            f"{send_time_text()}. Here's your first one:",
        )
        return reply(resp)

    # ── HELP ─────────────────────────────────────────────────────────────────
    if body == "HELP":
        resp.message(
            f"{BRAND_NAME}: Daily ABSITE quiz via SMS. Answer like Q1 A (or 1A 2C 3B). "
            "Text QUIZ for an extra question, FLAG Q1 to report a question that seems wrong, "
            "BOARD for the leaderboard. Msg & data rates may apply, ~3 msgs/day. Reply STOP to unsubscribe."
        )
        return reply(resp)

    # ── FLAG a question that seems wrong ──────────────────────────────────────
    if body.split(" ")[0] == "FLAG":
        # FLAG [Qn] [note] — no number means the most recent question
        m = re.match(r"FLAG\s*(?:Q\s*(\d{1,2})\b|(\d{1,2})\b)?\s*[:\-]?\s*(.*)", raw_body, re.I | re.S)
        label = m.group(1) or m.group(2)
        q = last_question_for(from_number, label)
        if not q:
            resp.message(f"There's no open Q{label} to flag." if label else "There's no question to flag yet.")
            return reply(resp)
        log_event("flag", from_number, name, question=q["question"], note=m.group(3).strip(), label=label)
        still_open = any(oq is q for oq in open_questions(from_number).values())
        resp.message(
            f"🚩 Thanks{', ' + name if name else ''}! That question is flagged for review."
            + ("\n\nYou can still answer it." if still_open else "")
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

    # ── Answers: "Q1 A", "q2=b", "1A 2C 3B", or a bare letter when one is open ──
    open_qs = open_questions(from_number)
    if open_qs:
        name = sessions[from_number].get("name", "") or name
        answers = parse_answers(body, open_qs)
        if not answers:
            resp.message(
                f"You have {len(open_qs)} question{'s' if len(open_qs) > 1 else ''} open ({labels_text(from_number)}). "
                "Reply with the number and letter, like Q1 A."
            )
            return reply(resp)

        replies = []
        for label, letter in answers:
            q = open_qs.get(label)
            if not q:
                replies.append(f"Q{label} isn't open. Open now: {labels_text(from_number) or 'none'}.")
                continue
            correct = q["correct"]
            if letter == correct:
                text = f"Q{label}: ✅ Correct!\n\n{q['explanation']}"
            else:
                text = (f"Q{label}: ❌ Incorrect. The correct answer is "
                        f"{correct}) {q['options'][correct]}.\n\n{q['explanation']}")
            replies.append(text)
            log_event("answer", from_number, name, label=label,
                      question=q["question"], answer=letter, correct_answer=correct,
                      is_correct=letter == correct)
            del open_qs[label]

        replies[0] = (f"Thanks, {name}!\n\n" if name else "Thanks!\n\n") + replies[0]
        remaining = labels_text(from_number)
        replies[-1] += ("\n\nStill open: " + remaining) if remaining else ""
        replies[-1] += "\n\nThink one is wrong? Reply FLAG Q#."
        for text in replies:
            resp.message(text)
        return reply(resp)

    if ANSWER_RE.search(body) or body in ("A", "B", "C", "D"):
        resp.message(
            "You don't have any open questions right now (already answered, or replaced by this "
            f"morning's set). New ones come at {send_time_text()}, or text QUIZ for one now."
        )
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

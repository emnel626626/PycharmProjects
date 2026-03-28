import os
import json
import threading
import random
from flask import Flask, request, Response
from twilio.twiml.messaging_response import MessagingResponse
import anthropic
from pypdf import PdfReader
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

PDF_PATH = os.getenv("PDF_PATH", r"C:\Users\Emily\PycharmProjects\TextMe\part1.pdf")
NUM_QUESTIONS = int(os.getenv("NUM_QUESTIONS", "10"))

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# Global state
_questions: list | None = None
_questions_error: str | None = None
sessions: dict = {}  # phone_number -> session dict


# ---------------------------------------------------------------------------
# PDF + question generation
# ---------------------------------------------------------------------------

def extract_pdf_text() -> str:
    reader = PdfReader(PDF_PATH)
    parts = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def generate_questions(text: str, num_questions: int) -> list:
    """Ask Claude to produce `num_questions` MCQs from `text` as a JSON list."""
    # Trim to ~120K chars to stay well within the 200K context window
    text = text[:120_000]

    response = client.messages.create(
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

    # Strip accidental markdown code fences
    if "```" in content:
        start = content.find("[")
        end = content.rfind("]") + 1
        if start != -1 and end > start:
            content = content[start:end]

    return json.loads(content)


def preload_questions() -> None:
    """Run at startup in a background thread."""
    global _questions, _questions_error
    try:
        print(f"[Quiz] Extracting text from {PDF_PATH} ...")
        text = extract_pdf_text()
        print(f"[Quiz] Extracted {len(text):,} characters. Generating {NUM_QUESTIONS} questions ...")
        _questions = generate_questions(text, NUM_QUESTIONS)
        print(f"[Quiz] Ready — {len(_questions)} questions loaded.")
    except Exception as exc:
        _questions_error = str(exc)
        print(f"[Quiz] Error during preload: {exc}")


# Kick off question generation immediately so it's ready before the first user texts
threading.Thread(target=preload_questions, daemon=True).start()


# ---------------------------------------------------------------------------
# SMS formatting helpers
# ---------------------------------------------------------------------------

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
# Twilio webhook
# ---------------------------------------------------------------------------

@app.route("/webhook", methods=["POST"])
def webhook():
    from_number = request.form.get("From", "")
    body = request.form.get("Body", "").strip().upper()

    resp = MessagingResponse()

    # ── START command ────────────────────────────────────────────────────────
    if body in ("START", "BEGIN", "QUIZ", "RESTART", "RESET"):
        if _questions_error:
            resp.message(
                f"Sorry, the quiz failed to load: {_questions_error[:120]}\n"
                "Please contact the administrator."
            )
        elif _questions is None:
            resp.message("The quiz is still loading — please try again in a moment! ⏳")
        else:
            selected = random.sample(_questions, min(NUM_QUESTIONS, len(_questions)))
            sessions[from_number] = {
                "questions": selected,
                "index": 0,
                "score": 0,
            }
            first_q = format_question(selected[0], 1, len(selected))
            resp.message(f"📚 Quiz starting! {len(selected)} questions.\n\n{first_q}")
        return Response(str(resp), mimetype="text/xml")

    # ── Active session: process an answer ───────────────────────────────────
    if from_number in sessions:
        session = sessions[from_number]
        questions = session["questions"]
        idx = session["index"]

        if body not in ("A", "B", "C", "D"):
            resp.message("Please reply with A, B, C, or D.")
            return Response(str(resp), mimetype="text/xml")

        q = questions[idx]
        correct = q["correct"]

        if body == correct:
            session["score"] += 1
            feedback = f"✅ Correct!\n\n{q['explanation']}"
        else:
            feedback = (
                f"❌ Incorrect. The correct answer is "
                f"{correct}) {q['options'][correct]}.\n\n"
                f"{q['explanation']}"
            )

        session["index"] += 1

        if session["index"] >= len(questions):
            # Quiz finished
            score = session["score"]
            total = len(questions)
            pct = round(score / total * 100)
            del sessions[from_number]
            resp.message(
                f"{feedback}\n\n"
                "───────────────\n"
                f"🎉 Quiz complete!\n"
                f"Score: {score}/{total} ({pct}%)\n\n"
                "Text START to try again!"
            )
        else:
            next_q = format_question(
                questions[session["index"]],
                session["index"] + 1,
                len(questions)
            )
            resp.message(f"{feedback}\n\n───────────────\n\n{next_q}")

        return Response(str(resp), mimetype="text/xml")

    # ── Default: prompt to start ─────────────────────────────────────────────
    resp.message("📚 Text START to begin the quiz!")
    return Response(str(resp), mimetype="text/xml")


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(debug=False, port=port)

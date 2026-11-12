import os, json, re
from dotenv import load_dotenv
from google import genai

load_dotenv()

def ask(prompt):
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key or key == "PASTE_YOUR_GEMINI_API_KEY":
        raise ValueError("Add your Gemini API key to .env.")
    client = genai.Client(api_key=key)
    response = client.models.generate_content(
        model=os.getenv("GEMINI_MODEL", "gemini-2.0-flash"), contents=prompt
    )
    return (response.text or "").strip()

def summarize(text):
    return ask("Create accurate study notes with overview, headings, bullet points, key terms and revision checklist. Use only this source:\n" + text[:40000])

def answer(context, question):
    return ask("Answer the student's question using ONLY these document excerpts. If the answer is absent, say so.\nEXCERPTS:\n" + context[:25000] + "\nQUESTION:\n" + question[:2000])

def quiz_questions(text, count=5, difficulty="Medium"):
    prompt = f"""Create exactly {count} {difficulty} multiple-choice questions from this text.
Return ONLY a JSON array. Each item: {{"question":"...","options":["...","...","...","..."],"answer":0,"explanation":"..."}}
answer is the zero-based index of the correct option. No markdown.
TEXT:
{text[:25000]}"""
    raw = ask(prompt)
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.I)
    match = re.search(r"\[.*\]", raw, flags=re.S)
    if not match:
        raise ValueError("Gemini did not return valid quiz JSON. Try again.")
    items = json.loads(match.group())
    valid = []
    for q in items[:count]:
        opts, ans = q.get("options", []), q.get("answer")
        if len(opts) == 4 and isinstance(ans, int) and 0 <= ans < 4:
            valid.append({"question": str(q["question"]), "options": [str(x) for x in opts],
                          "answer": ans, "explanation": str(q.get("explanation", ""))})
    if len(valid) < 3:
        raise ValueError("Gemini returned too few valid questions. Try again.")
    return valid

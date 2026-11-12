# EduSmart – AI-Powered Smart Learning Platform

College mini-project using HTML, CSS, Bootstrap 5, JavaScript, Python FastAPI, SQLite (or MySQL), Gemini API and PyMuPDF.

## Features
- Email and password sign-in and account creation
- Separate admin sign-in and server-protected platform overview
- Private study-material upload and text extraction (PDF, DOCX, PPTX, XLSX, ODT, ODP, ODS, TXT, Markdown, CSV, TSV, JSON, XML, HTML, RTF, YAML, and INI)
- Gemini study notes/summaries
- Study-material question-answering using simple keyword-ranked excerpts
- Gemini-generated MCQs, automatic scoring and explanations
- Dashboard statistics and score chart

## Requirements
Python 3.10+, internet for CDN and Gemini. SQLite is the local default; MySQL 8+ is optional.

## 1. Configure environment
Copy `.env.example` to `.env`:

Windows PowerShell:
```powershell
Copy-Item .env.example .env
```

The example uses a local SQLite database, created automatically. Replace `JWT_SECRET` with a private random string of at least 32 characters before starting the app. To use MySQL instead, create the database and user, then set `DATABASE_URL` to your SQLAlchemy MySQL connection string. Set `GEMINI_API_KEY` from Google AI Studio for the AI study features. Keep secrets in `.env`, never in frontend JavaScript or GitHub.

The admin portal is at `/admin/login`; `/admin` and its overview API require the configured administrator. `ADMIN_EMAIL` is prefilled in `.env.example`. Create a bcrypt hash for the admin password without echoing the password, then set the printed hash as `ADMIN_PASSWORD_HASH` in `.env`:

```powershell
python -c "from getpass import getpass; from passlib.context import CryptContext; print(CryptContext(schemes=['bcrypt']).hash(getpass('Admin password: ')))"
```

Keep the password hash private in `.env`; do not put the password or hash in frontend files or commit either. Public registration, email-code sign-in, and Google sign-in cannot create or authenticate the configured admin account. If `ADMIN_PASSWORD_HASH` is empty, the reserved admin email cannot use public sign-in and the admin portal stays unavailable until configured. Restart EduSmart after updating `.env`.

## 2. Install dependencies
From the folder containing requirements.txt:

Windows:
```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

macOS/Linux:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 3. Run locally
```bash
uvicorn app.main:app --reload
```
Website: http://127.0.0.1:8000  
API docs: http://127.0.0.1:8000/docs  
Health check: http://127.0.0.1:8000/api/health

Tables are created automatically at startup. Run the command from the project root, not from inside app/.

## 4. Deploy

GitHub Pages hosts the frontend; Render hosts the FastAPI backend. GitHub Pages cannot run the API or database.

1. Push this repository to GitHub. The workflow in `.github/workflows/pages.yml` publishes the frontend from `app/static` using GitHub Pages. In the repository settings, set **Pages → Build and deployment → Source** to **GitHub Actions**.
2. In Render, create a Blueprint from the repository and deploy `render.yaml`. The free service uses SQLite on temporary storage, so accounts, uploaded materials, and quiz results may be lost when the service restarts or redeploys.
3. In the GitHub repository, add the Actions variable `EDUSMART_API_BASE` with the full Render service URL, such as `https://edusmart-api.onrender.com`, then rerun the Pages workflow.
4. The Render service's `CORS_ORIGINS` must include the GitHub Pages origin (for this repository, `https://python-masterr.github.io`). Set `GEMINI_API_KEY` in Render to enable AI features. Do not commit `.env` or secrets.

The GitHub Pages site URL is `https://python-masterr.github.io/edusmart`. If the repository owner or name changes, update the URL and the backend `CORS_ORIGINS` value.

## 5. Manual test checklist
1. Create a student account with a name, email, and password.
2. Log out and log in with the same email and password.
3. Upload a supported study file smaller than 15 MB (for example PDF, DOCX, PPTX, XLSX, TXT, or CSV).
4. Open it and generate AI notes.
5. Ask a question whose answer is in that study material.
6. Generate a quiz, answer all questions and submit.
7. Confirm dashboard counts and score chart update.
8. Register a second account and verify it cannot see the first account's files.

To run the focused admin access tests, install test dependencies with `pip install -r requirements-dev.txt`, then run `python -m pytest`.

## Troubleshooting
- Open the URL printed by Uvicorn after running `uvicorn app.main:app --reload` from the project root. Do not use `python -m http.server` or open `index.html` directly; those do not run the login API.
- MySQL access denied: verify username/password, host and grants in DATABASE_URL.
- Unknown database: create edusmart first.
- Gemini errors: verify API key, model availability, quota and installed Google Gen AI SDK.
- No readable document text: scanned/image-only PDFs and embedded images are not supported in this MVP; OCR is a future upgrade. Legacy binary Office formats (`.doc`, `.ppt`, `.xls`) are not supported; use their modern `.docx`, `.pptx`, or `.xlsx` equivalents.
- Port in use: `uvicorn app.main:app --reload --port 8001`.

## Important
This is an educational MVP, not production-ready. It uses local file storage and basic keyword retrieval rather than a full vector database. Before public deployment, add migrations, rate limiting, strict CORS, malware scanning, upload/resource limits, HTTPS, backups, audit logs and stronger operational security. Review AI outputs; they can be inaccurate.

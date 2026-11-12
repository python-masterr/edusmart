import csv, hashlib, hmac, io, json, logging, os, re, secrets, smtplib, ssl, uuid, zipfile, zlib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parseaddr
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree
from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.starlette_client import OAuth
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel, EmailStr, Field
import fitz
from dotenv import load_dotenv
from .database import Base, engine, get_db
from .models import User, Document, Quiz, QuizResult, EmailLoginCode
from .security import SECRET, hash_password, verify_password, create_token, token_user, token_is_admin
from .ai import summarize, answer, quiz_questions

load_dotenv()
logger = logging.getLogger(__name__)
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip().lower()
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "").strip()
if ADMIN_PASSWORD_HASH and not re.fullmatch(r"\$2[aby]\$\d\d\$[./A-Za-z0-9]{53}", ADMIN_PASSWORD_HASH):
    raise RuntimeError("ADMIN_PASSWORD_HASH must be a bcrypt hash generated with the project password hasher.")

def is_admin_email(email: str) -> bool:
    return bool(ADMIN_EMAIL) and email.strip().lower() == ADMIN_EMAIL

def admin_auth_configured() -> bool:
    return bool(ADMIN_EMAIL and ADMIN_PASSWORD_HASH)

def require_admin_configuration():
    if not admin_auth_configured():
        raise HTTPException(503, "Admin access is not configured.")

def utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
HERE = Path(__file__).resolve().parent
UPLOADS = HERE / "uploads"
UPLOADS.mkdir(exist_ok=True)
LIMIT = int(os.getenv("MAX_UPLOAD_MB", "15")) * 1024 * 1024
MAX_DOCUMENT_TEXT = 500_000
MAX_ARCHIVE_SIZE = 50 * 1024 * 1024
DOCUMENT_EXTENSIONS = {
    ".pdf", ".docx", ".pptx", ".xlsx", ".odt", ".odp", ".ods",
    ".txt", ".md", ".rst", ".csv", ".tsv", ".json", ".xml",
    ".html", ".htm", ".rtf", ".log", ".yaml", ".yml", ".ini",
}
ARCHIVE_EXTENSIONS = {".docx", ".pptx", ".xlsx", ".odt", ".odp", ".ods"}
app = FastAPI(title="EduSmart API", version="1.0")
cors_origins = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", "*").split(",")
    if origin.strip()
]
app.add_middleware(CORSMiddleware, allow_origins=cors_origins, allow_credentials=False,
                   allow_methods=["*"], allow_headers=["*"])
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("JWT_SECRET", ""),
    session_cookie="edusmart_oauth",
    max_age=600,
    same_site="lax",
    https_only=os.getenv("COOKIE_SECURE", "false").lower() == "true",
)
Base.metadata.create_all(bind=engine)
security = HTTPBearer(auto_error=False)
oauth = OAuth()
google_client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
google_client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
if google_client_id and google_client_secret:
    oauth.register(
        name="google",
        client_id=google_client_id,
        client_secret=google_client_secret,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )

class _StudyHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip_depth = 0

    def handle_starttag(self, tag, _attrs):
        if tag in {"script", "style"}:
            self.skip_depth += 1
        elif tag in {"br", "p", "div", "li", "tr", "h1", "h2", "h3"} and not self.skip_depth:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.skip_depth:
            self.skip_depth -= 1
        elif tag in {"p", "div", "li", "tr", "h1", "h2", "h3"} and not self.skip_depth:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip_depth:
            self.parts.append(data)


def _xml_paragraphs(data: bytes) -> list[str]:
    root = ElementTree.fromstring(data)
    paragraphs = [
        "".join(node.itertext()).strip()
        for node in root.iter()
        if node.tag.rsplit("}", 1)[-1] in {"p", "h"}
    ]
    return [paragraph for paragraph in paragraphs if paragraph]


def _decode_text(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("Text documents must use UTF-8, UTF-16, or Windows-1252 encoding.")


def _extract_archive_text(raw: bytes, suffix: str) -> str:
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        infos = archive.infolist()
        if sum(info.file_size for info in infos) > MAX_ARCHIVE_SIZE:
            raise ValueError("This document expands beyond the supported size limit.")
        names = set(archive.namelist())

        if suffix == ".docx":
            path = "word/document.xml"
            if path not in names:
                raise ValueError("This is not a valid DOCX document.")
            return "\n".join(_xml_paragraphs(archive.read(path)))

        if suffix == ".pptx":
            slides = sorted(
                (name for name in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
                key=lambda name: int(re.search(r"slide(\d+)\.xml$", name).group(1)),
            )
            if not slides:
                raise ValueError("This is not a valid PPTX presentation.")
            return "\n".join(
                paragraph for name in slides
                for paragraph in _xml_paragraphs(archive.read(name))
            )

        if suffix == ".xlsx":
            if "xl/workbook.xml" not in names:
                raise ValueError("This is not a valid XLSX workbook.")
            shared_strings = []
            if "xl/sharedStrings.xml" in names:
                root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
                shared_strings = [
                    "".join(node.itertext())
                    for node in root
                    if node.tag.rsplit("}", 1)[-1] == "si"
                ]
            sheets = sorted(
                (name for name in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)),
                key=lambda name: int(re.search(r"sheet(\d+)\.xml$", name).group(1)),
            )
            rows = []
            for name in sheets:
                root = ElementTree.fromstring(archive.read(name))
                for row in root.iter():
                    if row.tag.rsplit("}", 1)[-1] != "row":
                        continue
                    cells = []
                    for cell in row:
                        if cell.tag.rsplit("}", 1)[-1] != "c":
                            continue
                        cell_type = cell.attrib.get("t")
                        value = next(
                            (child.text or "" for child in cell
                             if child.tag.rsplit("}", 1)[-1] == "v"),
                            "",
                        )
                        if cell_type == "s" and value:
                            value = shared_strings[int(value)]
                        elif cell_type == "inlineStr":
                            value = "".join(cell.itertext())
                        if value:
                            cells.append(value)
                    if cells:
                        rows.append("\t".join(cells))
            return "\n".join(rows)

        if suffix in {".odt", ".odp", ".ods"}:
            if "content.xml" not in names:
                raise ValueError("This is not a valid OpenDocument file.")
            return "\n".join(_xml_paragraphs(archive.read("content.xml")))

    raise ValueError("This document format is not supported.")


def extract_document_text(raw: bytes, suffix: str) -> str:
    if suffix not in DOCUMENT_EXTENSIONS:
        supported = ", ".join(sorted(DOCUMENT_EXTENSIONS))
        raise ValueError(f"Unsupported file type. Supported formats: {supported}.")

    if suffix == ".pdf":
        if not raw.startswith(b"%PDF"):
            raise ValueError("Invalid PDF file.")
        with fitz.open(stream=raw, filetype="pdf") as pdf:
            text = "\n".join(page.get_text() for page in pdf)
    elif suffix in ARCHIVE_EXTENSIONS:
        text = _extract_archive_text(raw, suffix)
    elif suffix in {".html", ".htm"}:
        parser = _StudyHTMLParser()
        parser.feed(_decode_text(raw))
        text = "".join(parser.parts)
    elif suffix == ".rtf":
        source = _decode_text(raw)
        text = re.sub(r"\\'[0-9a-fA-F]{2}", " ", source)
        text = re.sub(r"\\[a-zA-Z]+-?\d* ?", " ", text)
        text = re.sub(r"[{}]", " ", text)
    elif suffix == ".csv":
        source = _decode_text(raw)
        text = "\n".join("\t".join(row) for row in csv.reader(io.StringIO(source)))
    elif suffix == ".tsv":
        source = _decode_text(raw)
        text = "\n".join("\t".join(row) for row in csv.reader(io.StringIO(source), delimiter="\t"))
    elif suffix == ".xml":
        text = " ".join(ElementTree.fromstring(raw).itertext())
    else:
        text = _decode_text(raw)

    text = text.strip()
    if not text:
        raise ValueError("No readable text was found in this document.")
    return text[:MAX_DOCUMENT_TEXT]

class Register(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)
class Login(BaseModel):
    email: EmailStr
    password: str
class AdminLogin(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=72)
class RequestEmailCode(BaseModel):
    email: EmailStr
    name: str = Field(default="", max_length=120)
class VerifyEmailCode(BaseModel):
    email: EmailStr
    code: str = Field(pattern=r"^\d{6}$")
    name: str = Field(default="", max_length=120)
    create_account: bool = False
class Chat(BaseModel):
    question: str = Field(min_length=2, max_length=2000)
class QuizRequest(BaseModel):
    count: int = Field(default=5, ge=3, le=10)
    difficulty: str = "Medium"
class Submit(BaseModel):
    answers: list[int]

def get_user(
    request: Request,
    creds: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
):
    token = creds.credentials if creds else request.cookies.get("edusmart_token")
    if not token:
        raise HTTPException(401, "Please log in.")
    uid = token_user(token)
    user = db.get(User, uid) if uid else None
    if not user:
        raise HTTPException(401, "Invalid or expired token.")
    return user

def get_admin_user(
    request: Request,
    creds: HTTPAuthorizationCredentials = Depends(security),
    configured: None = Depends(require_admin_configuration),
    user: User = Depends(get_user),
):
    token = creds.credentials if creds else request.cookies.get("edusmart_token")
    if not token or not token_is_admin(token) or not is_admin_email(user.email):
        raise HTTPException(403, "Admin access required.")
    return user

def get_doc(doc_id, user, db):
    doc = db.query(Document).filter_by(id=doc_id, user_id=user.id).first()
    if not doc:
        raise HTTPException(404, "Document not found.")
    return doc

def hash_login_code(email: str, code: str) -> str:
    return hmac.new(SECRET.encode(), f"{email}:{code}".encode(), hashlib.sha256).hexdigest()

def send_login_code(email: str, code: str) -> None:
    host = os.getenv("SMTP_HOST", "").strip()
    sender = os.getenv("SMTP_FROM", "").strip()
    username = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    if not host or not sender:
        raise HTTPException(
            503,
            "Email sign-in is not configured. Set SMTP_HOST and SMTP_FROM in .env, then restart EduSmart.",
        )
    if "\r" in host or "\n" in host or "\r" in sender or "\n" in sender or not parseaddr(sender)[1]:
        raise HTTPException(503, "SMTP_HOST or SMTP_FROM is invalid. Check the email server settings in .env.")
    if bool(username) != bool(password):
        raise HTTPException(503, "Configure both SMTP_USER and SMTP_PASSWORD, or leave both blank.")

    use_ssl = os.getenv("SMTP_USE_SSL", "false").lower() == "true"
    use_starttls = os.getenv("SMTP_STARTTLS", "true").lower() == "true"
    if use_ssl and use_starttls:
        raise HTTPException(503, "Set only one of SMTP_USE_SSL or SMTP_STARTTLS to true.")
    try:
        port = int(os.getenv("SMTP_PORT", "587"))
    except ValueError as exc:
        raise HTTPException(503, "SMTP_PORT must be a valid port number.") from exc
    if not 1 <= port <= 65535:
        raise HTTPException(503, "SMTP_PORT must be between 1 and 65535.")

    message = EmailMessage()
    message["Subject"] = "Your EduSmart sign-in code"
    message["From"] = sender
    message["To"] = email
    message.set_content(
        f"Your EduSmart sign-in code is {code}.\n\n"
        "It expires in 10 minutes and can only be used once. "
        "If you did not request this code, you can ignore this email."
    )
    try:
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, timeout=15, context=ssl.create_default_context()) as server:
                if username:
                    server.login(username, password)
                server.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=15) as server:
                server.ehlo()
                if use_starttls:
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                if username:
                    server.login(username, password)
                server.send_message(message)
    except (OSError, ssl.SSLError, smtplib.SMTPException):
        logger.exception("Could not send EduSmart sign-in code")
        raise HTTPException(502, "Could not send the sign-in email. Check SMTP settings and try again.") from None

@app.get("/api/health")
def health():
    return {"status": "ok"}

@app.get("/api/auth/options")
def auth_options():
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_from = os.getenv("SMTP_FROM", "").strip()
    return {
        "email_code_enabled": bool(smtp_host and smtp_from),
        "google_enabled": bool(google_client_id and google_client_secret),
    }

@app.post("/api/auth/register")
def register(data: Register, db: Session = Depends(get_db)):
    email = data.email.lower()
    if is_admin_email(email):
        raise HTTPException(403, "This account cannot be registered through public sign-up.")
    if db.query(User).filter_by(email=email).first():
        raise HTTPException(409, "Email already registered.")
    u = User(name=data.name.strip(), email=email, password_hash=hash_password(data.password))
    db.add(u); db.commit(); db.refresh(u)
    return {"token": create_token(u.id), "user": {"id": u.id, "name": u.name, "email": u.email}}

@app.post("/api/auth/login")
def login(data: Login, db: Session = Depends(get_db)):
    email = data.email.lower()
    if is_admin_email(email):
        raise HTTPException(401, "Incorrect email or password.")
    u = db.query(User).filter_by(email=email).first()
    if not u or not verify_password(data.password, u.password_hash):
        raise HTTPException(401, "Incorrect email or password.")
    return {"token": create_token(u.id), "user": {"id": u.id, "name": u.name, "email": u.email}}

@app.post("/api/admin/login")
def admin_login(data: AdminLogin, request: Request, db: Session = Depends(get_db)):
    if not admin_auth_configured():
        raise HTTPException(503, "Admin access is not configured.")
    if len(data.password.encode("utf-8")) > 72:
        raise HTTPException(401, "Incorrect email or password.")
    password_matches = verify_password(data.password, ADMIN_PASSWORD_HASH)
    if not is_admin_email(str(data.email)) or not password_matches:
        raise HTTPException(401, "Incorrect email or password.")

    user = db.query(User).filter_by(email=ADMIN_EMAIL).first()
    if not user:
        user = User(
            name=os.getenv("ADMIN_NAME", "Administrator").strip()[:120] or "Administrator",
            email=ADMIN_EMAIL,
            password_hash=hash_password(secrets.token_urlsafe(48)),
        )
        db.add(user)
        try:
            db.commit()
            db.refresh(user)
        except IntegrityError:
            db.rollback()
            user = db.query(User).filter_by(email=ADMIN_EMAIL).first()
            if not user:
                raise

    response = JSONResponse({
        "message": "Signed in.",
        "user": {"name": user.name, "email": user.email},
    })
    response.set_cookie(
        "edusmart_token",
        create_token(user.id, is_admin=True),
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        max_age=int(os.getenv("ACCESS_TOKEN_MINUTES", "1440")) * 60,
        path="/",
    )
    return response

@app.post("/api/auth/request-code")
def request_email_code(data: RequestEmailCode, request: Request, db: Session = Depends(get_db)):
    email = str(data.email).strip().lower()
    if is_admin_email(email):
        return {"message": "If this email can receive sign-in codes, a code has been sent."}
    now = utc_now_naive()
    client_ip = request.client.host if request.client else "unknown"
    recent_email_codes = db.query(EmailLoginCode).filter(
        EmailLoginCode.email == email,
        EmailLoginCode.created_at >= now - timedelta(hours=1),
    )
    if recent_email_codes.count() >= 5:
        raise HTTPException(429, "Too many sign-in codes requested. Try again in an hour.")
    if recent_email_codes.filter(EmailLoginCode.created_at >= now - timedelta(seconds=60)).first():
        raise HTTPException(429, "Please wait a minute before requesting another sign-in code.")
    recent_ip_codes = db.query(EmailLoginCode).filter(
        EmailLoginCode.request_ip == client_ip,
        EmailLoginCode.created_at >= now - timedelta(hours=1),
    )
    if recent_ip_codes.count() >= 20:
        raise HTTPException(429, "Too many sign-in codes requested from this connection. Try again later.")

    code = f"{secrets.randbelow(1_000_000):06d}"
    db.query(EmailLoginCode).filter(
        EmailLoginCode.email == email,
        EmailLoginCode.consumed_at.is_(None),
    ).update({EmailLoginCode.consumed_at: now}, synchronize_session=False)
    login_code = EmailLoginCode(
        email=email,
        code_hash=hash_login_code(email, code),
        request_ip=client_ip[:45],
        display_name=data.name.strip()[:120] or None,
        created_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    db.add(login_code)
    db.commit()
    try:
        send_login_code(email, code)
    except HTTPException:
        db.delete(login_code)
        db.commit()
        raise

    return {"message": "If this email can receive sign-in codes, a code has been sent."}

@app.post("/api/auth/verify-code")
def verify_email_code(data: VerifyEmailCode, db: Session = Depends(get_db)):
    email = str(data.email).strip().lower()
    if is_admin_email(email):
        raise HTTPException(401, "That code is invalid or expired. Request a new sign-in code.")
    now = utc_now_naive()
    login_code = db.query(EmailLoginCode).filter(
        EmailLoginCode.email == email,
        EmailLoginCode.consumed_at.is_(None),
    ).order_by(EmailLoginCode.created_at.desc()).first()
    if not login_code or login_code.expires_at <= now or login_code.attempts >= 5:
        raise HTTPException(400, "That code is invalid or expired. Request a new sign-in code.")

    submitted_hash = hash_login_code(email, data.code)
    if not hmac.compare_digest(login_code.code_hash, submitted_hash):
        login_code.attempts += 1
        if login_code.attempts >= 5:
            login_code.consumed_at = now
        db.commit()
        raise HTTPException(400, "That code is incorrect. Check your email and try again.")

    user = db.query(User).filter_by(email=email).first()
    if not user and not data.create_account:
        raise HTTPException(404, "No account found for this email. Choose Create account to sign up.")

    consumed = db.execute(
        update(EmailLoginCode)
        .where(
            EmailLoginCode.id == login_code.id,
            EmailLoginCode.consumed_at.is_(None),
            EmailLoginCode.attempts < 5,
            EmailLoginCode.expires_at > now,
        )
        .values(consumed_at=now)
    )
    if consumed.rowcount != 1:
        db.rollback()
        raise HTTPException(400, "That code is invalid or expired. Request a new sign-in code.")
    db.commit()

    if not user:
        name = (data.name.strip() or login_code.display_name or email.split("@", 1)[0])[:120]
        user = User(name=name, email=email, password_hash=hash_password(secrets.token_urlsafe(48)))
        db.add(user)
        try:
            db.commit()
            db.refresh(user)
        except IntegrityError:
            db.rollback()
            user = db.query(User).filter_by(email=email).first()
            if not user:
                raise

    return {
        "token": create_token(user.id),
        "user": {"id": user.id, "name": user.name, "email": user.email},
    }

@app.get("/api/auth/google/login")
async def google_login(request: Request):
    if not google_client_id or not google_client_secret:
        return RedirectResponse("/?google_error=not_configured", status_code=303)
    callback_url = request.url_for("google_callback")
    return await oauth.google.authorize_redirect(request, callback_url)

@app.get("/api/auth/google/callback", name="google_callback")
async def google_callback(request: Request, db: Session = Depends(get_db)):
    try:
        token = await oauth.google.authorize_access_token(request)
    except OAuthError:
        return RedirectResponse("/?google_error=sign_in_failed", status_code=303)

    profile = token.get("userinfo")
    if not profile:
        raise HTTPException(401, "Google did not return a user profile.")
    email = str(profile.get("email", "")).strip().lower()
    if not email or profile.get("email_verified") is not True:
        raise HTTPException(403, "Use a Google account with a verified email address.")
    if is_admin_email(email):
        return RedirectResponse("/admin/login?error=use_admin_sign_in", status_code=303)

    user = db.query(User).filter_by(email=email).first()
    if not user:
        name = str(profile.get("name") or email.split("@", 1)[0]).strip()[:120]
        user = User(name=name, email=email, password_hash=hash_password(secrets.token_urlsafe(48)))
        db.add(user)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            user = db.query(User).filter_by(email=email).first()
            if not user:
                raise
        db.refresh(user)

    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        "edusmart_token",
        create_token(user.id),
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        max_age=int(os.getenv("ACCESS_TOKEN_MINUTES", "1440")) * 60,
        path="/",
    )
    return response

@app.post("/api/auth/logout")
def auth_logout():
    response = JSONResponse({"message": "Logged out."})
    response.delete_cookie("edusmart_token", path="/", httponly=True, samesite="lax")
    return response

@app.post("/api/admin/logout")
def admin_logout():
    response = JSONResponse({"message": "Logged out."})
    response.delete_cookie("edusmart_token", path="/", httponly=True, samesite="lax")
    return response

@app.get("/api/auth/me")
def me(user: User = Depends(get_user)):
    return {"id": user.id, "name": user.name, "email": user.email}

@app.get("/api/dashboard")
def dashboard(user: User = Depends(get_user), db: Session = Depends(get_db)):
    docs = db.query(Document).filter_by(user_id=user.id).count()
    results = db.query(QuizResult).filter_by(user_id=user.id).order_by(QuizResult.created_at.desc()).all()
    avg = sum((r.score / r.total * 100) for r in results if r.total) / len([r for r in results if r.total]) if any(r.total for r in results) else 0
    return {"documents": docs, "quizzes_taken": len(results), "average_score": round(avg, 1),
            "recent_results": [{"score": r.score, "total": r.total, "date": r.created_at.isoformat()} for r in results[:5]]}

@app.post("/api/documents")
async def upload(file: UploadFile = File(...), subject: str = Form("General"),
                 user: User = Depends(get_user), db: Session = Depends(get_db)):
    if not file.filename:
        raise HTTPException(400, "Choose a document to upload.")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in DOCUMENT_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type: {suffix or 'no extension'}.")
    raw = await file.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise HTTPException(413, f"Document exceeds {LIMIT // 1048576} MB.")
    try:
        text = extract_document_text(raw, suffix)
    except (ValueError, UnicodeDecodeError, csv.Error, zipfile.BadZipFile,
            ElementTree.ParseError, IndexError, RuntimeError, NotImplementedError,
            OSError, zlib.error) as error:
        raise HTTPException(400, str(error)) from error
    stored = uuid.uuid4().hex + suffix
    path = UPLOADS / stored
    path.write_bytes(raw)
    d = Document(user_id=user.id, filename=Path(file.filename).name[:255],
                 stored_name=stored, subject=subject[:120], extracted_text=text)
    db.add(d); db.commit(); db.refresh(d)
    return {"id": d.id, "filename": d.filename, "subject": d.subject, "characters": len(text)}

@app.get("/api/documents")
def documents(user: User = Depends(get_user), db: Session = Depends(get_db)):
    rows = db.query(Document).filter_by(user_id=user.id).order_by(Document.created_at.desc()).all()
    return [{"id": d.id, "filename": d.filename, "subject": d.subject, "characters": len(d.extracted_text)} for d in rows]

@app.delete("/api/documents/{doc_id}")
def delete_doc(doc_id: int, user: User = Depends(get_user), db: Session = Depends(get_db)):
    d = get_doc(doc_id, user, db)
    (UPLOADS / d.stored_name).unlink(missing_ok=True)
    db.delete(d); db.commit()
    return {"message": "Document deleted."}

@app.post("/api/documents/{doc_id}/summary")
def summary(doc_id: int, user: User = Depends(get_user), db: Session = Depends(get_db)):
    d = get_doc(doc_id, user, db)
    try:
        return {"summary": summarize(d.extracted_text)}
    except Exception as e:
        raise HTTPException(502, f"Gemini error: {str(e)[:250]}")

@app.post("/api/documents/{doc_id}/chat")
def chat(doc_id: int, data: Chat, user: User = Depends(get_user), db: Session = Depends(get_db)):
    d = get_doc(doc_id, user, db)
    terms = {x.lower() for x in re.findall(r"\w+", data.question) if len(x) > 2}
    chunks = [d.extracted_text[i:i+1200] for i in range(0, len(d.extracted_text), 900)]
    chunks.sort(key=lambda c: sum(w in c.lower() for w in terms), reverse=True)
    try:
        return {"answer": answer("\n\n".join(chunks[:5]), data.question)}
    except Exception as e:
        raise HTTPException(502, f"Gemini error: {str(e)[:250]}")

@app.post("/api/documents/{doc_id}/quiz")
def create_quiz(doc_id: int, data: QuizRequest, user: User = Depends(get_user), db: Session = Depends(get_db)):
    d = get_doc(doc_id, user, db)
    if data.difficulty not in ["Easy", "Medium", "Hard"]:
        raise HTTPException(400, "Difficulty must be Easy, Medium, or Hard.")
    try:
        questions = quiz_questions(d.extracted_text, data.count, data.difficulty)
    except Exception as e:
        raise HTTPException(502, f"Quiz generation error: {str(e)[:250]}")
    q = Quiz(user_id=user.id, document_id=d.id, title=f"{d.subject} Quiz", questions=questions)
    db.add(q); db.commit(); db.refresh(q)
    return {"quiz_id": q.id, "title": q.title,
            "questions": [{"question": x["question"], "options": x["options"]} for x in questions]}

@app.post("/api/quizzes/{quiz_id}/submit")
def submit(quiz_id: int, data: Submit, user: User = Depends(get_user), db: Session = Depends(get_db)):
    q = db.query(Quiz).filter_by(id=quiz_id, user_id=user.id).first()
    if not q:
        raise HTTPException(404, "Quiz not found.")
    if len(data.answers) != len(q.questions) or any(a not in range(4) for a in data.answers):
        raise HTTPException(400, "Submit one valid answer for every question.")
    score = sum(a == item["answer"] for a, item in zip(data.answers, q.questions))
    db.add(QuizResult(user_id=user.id, quiz_id=q.id, score=score, total=len(q.questions))); db.commit()
    details = [{"question": item["question"], "selected": a, "correct": item["answer"],
                "explanation": item["explanation"], "is_correct": a == item["answer"]}
               for a, item in zip(data.answers, q.questions)]
    return {"score": score, "total": len(q.questions), "percentage": round(score*100/len(q.questions), 1), "details": details}

@app.get("/api/analytics")
def analytics(user: User = Depends(get_user), db: Session = Depends(get_db)):
    rows = db.query(QuizResult).filter_by(user_id=user.id).order_by(QuizResult.created_at.asc()).all()
    return {"labels": [r.created_at.strftime("%d %b") for r in rows],
            "scores": [round(r.score*100/r.total, 1) if r.total else 0 for r in rows],
            "total_attempts": len(rows)}

@app.get("/api/admin/overview")
def admin_overview(user: User = Depends(get_admin_user), db: Session = Depends(get_db)):
    return {
        "admin": {"name": user.name, "email": user.email},
        "users": db.query(User).count(),
        "documents": db.query(Document).count(),
        "quizzes": db.query(Quiz).count(),
    }

app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
@app.get("/")
def home():
    return FileResponse(HERE / "static" / "index.html")

@app.get("/admin/login")
def admin_login_page():
    return FileResponse(HERE / "templates" / "admin_login.html")

@app.get("/admin")
def admin_page(user: User = Depends(get_admin_user)):
    return FileResponse(HERE / "templates" / "admin.html")

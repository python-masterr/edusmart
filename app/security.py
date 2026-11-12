import os
from datetime import datetime, timedelta, timezone
from jose import jwt, JWTError
from passlib.context import CryptContext
from dotenv import load_dotenv

load_dotenv()
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
SECRET = os.getenv("JWT_SECRET", "")
ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
if len(SECRET) < 32:
    raise RuntimeError("JWT_SECRET must be at least 32 characters. Set it in .env.")

def hash_password(value):
    return pwd.hash(value)

def verify_password(value, hashed):
    return pwd.verify(value, hashed)

def create_token(user_id, is_admin=False):
    exp = datetime.now(timezone.utc) + timedelta(minutes=int(os.getenv("ACCESS_TOKEN_MINUTES", "1440")))
    claims = {"sub": str(user_id), "exp": exp}
    if is_admin:
        claims["role"] = "admin"
    return jwt.encode(claims, SECRET, algorithm=ALGORITHM)

def token_user(token):
    try:
        return int(jwt.decode(token, SECRET, algorithms=[ALGORITHM])["sub"])
    except (JWTError, ValueError, KeyError):
        return None

def token_is_admin(token):
    try:
        return jwt.decode(token, SECRET, algorithms=[ALGORITHM]).get("role") == "admin"
    except JWTError:
        return False

import os
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["JWT_SECRET"] = "test-only-secret-key-with-at-least-32-characters"
os.environ["ADMIN_EMAIL"] = "admin@example.com"
os.environ["ADMIN_PASSWORD_HASH"] = ""

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import main
from app.database import Base, get_db
from app.models import EmailLoginCode, User
from app.security import create_token, hash_password


class AdminAccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        cls.session_factory = sessionmaker(bind=cls.engine)
        main.app.dependency_overrides[get_db] = cls.override_get_db
        cls.client = TestClient(main.app)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        main.app.dependency_overrides.clear()
        cls.engine.dispose()

    @classmethod
    def override_get_db(cls):
        db = cls.session_factory()
        try:
            yield db
        finally:
            db.close()

    def setUp(self):
        Base.metadata.drop_all(self.engine)
        Base.metadata.create_all(self.engine)
        main.ADMIN_EMAIL = "admin@example.com"
        main.ADMIN_PASSWORD_HASH = hash_password("test-admin-password")
        self.client.cookies.clear()
        with self.session_factory() as db:
            db.add(User(
                name="Student",
                email="student@example.com",
                password_hash=hash_password("student-password"),
            ))
            db.commit()

    def tearDown(self):
        self.client.cookies.clear()

    def test_admin_login_is_required_for_page_and_overview(self):
        self.assertEqual(self.client.get("/admin").status_code, 401)
        self.assertEqual(self.client.get("/api/admin/overview").status_code, 401)
        self.assertEqual(self.client.get("/admin/login").status_code, 200)
        self.assertEqual(self.client.get("/static/admin.html").status_code, 404)

        rejected = self.client.post("/api/admin/login", json={
            "email": "admin@example.com",
            "password": "wrong-password",
        })
        self.assertEqual(rejected.status_code, 401)
        self.assertNotIn("edusmart_token", self.client.cookies)

        signed_in = self.client.post("/api/admin/login", json={
            "email": "admin@example.com",
            "password": "test-admin-password",
        })
        self.assertEqual(signed_in.status_code, 200)
        self.assertTrue(signed_in.headers["set-cookie"].lower().find("httponly") >= 0)
        self.assertNotIn("test-admin-password", signed_in.text)
        self.assertEqual(self.client.get("/admin").status_code, 200)
        overview = self.client.get("/api/admin/overview")
        self.assertEqual(overview.status_code, 200)
        self.assertEqual(overview.json()["admin"]["email"], "admin@example.com")
        self.assertEqual(self.client.post("/api/admin/logout").status_code, 200)
        self.assertEqual(self.client.get("/api/admin/overview").status_code, 401)

    def test_student_keeps_regular_access_but_cannot_open_admin_page_or_api(self):
        signed_in = self.client.post("/api/auth/login", json={
            "email": "student@example.com",
            "password": "student-password",
        })
        self.assertEqual(signed_in.status_code, 200)
        headers = {"Authorization": f"Bearer {signed_in.json()['token']}"}

        self.assertEqual(self.client.get("/api/auth/me", headers=headers).status_code, 200)
        self.assertEqual(self.client.get("/api/dashboard", headers=headers).status_code, 200)
        self.assertEqual(self.client.get("/admin", headers=headers).status_code, 403)
        self.assertEqual(
            self.client.get("/api/admin/overview", headers=headers).status_code,
            403,
        )

        self.client.cookies.set("edusmart_token", signed_in.json()["token"])
        self.assertEqual(self.client.get("/admin").status_code, 403)
        self.assertEqual(self.client.get("/api/admin/overview").status_code, 403)

    def test_existing_non_admin_token_for_admin_email_is_rejected(self):
        with self.session_factory() as db:
            user = User(
                name="Admin",
                email="admin@example.com",
                password_hash=hash_password("old-password"),
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            old_token = create_token(user.id)

        headers = {"Authorization": f"Bearer {old_token}"}
        self.assertEqual(self.client.get("/admin", headers=headers).status_code, 403)
        self.assertEqual(
            self.client.get("/api/admin/overview", headers=headers).status_code,
            403,
        )

    def test_public_auth_cannot_claim_reserved_admin_email(self):
        registration = self.client.post("/api/auth/register", json={
            "name": "Admin",
            "email": "admin@example.com",
            "password": "test-admin-password",
        })
        self.assertEqual(registration.status_code, 403)

        public_login = self.client.post("/api/auth/login", json={
            "email": "admin@example.com",
            "password": "test-admin-password",
        })
        self.assertEqual(public_login.status_code, 401)

        code_request = self.client.post("/api/auth/request-code", json={
            "email": "admin@example.com",
        })
        self.assertEqual(code_request.status_code, 200)
        verification = self.client.post("/api/auth/verify-code", json={
            "email": "admin@example.com",
            "code": "123456",
            "create_account": True,
        })
        self.assertEqual(verification.status_code, 401)
        with self.session_factory() as db:
            self.assertEqual(db.query(EmailLoginCode).count(), 0)
            self.assertEqual(db.query(User).filter_by(email="admin@example.com").count(), 0)

    def test_google_sign_in_redirects_reserved_admin_to_admin_login(self):
        fake_google = SimpleNamespace(
            authorize_access_token=AsyncMock(return_value={
                "userinfo": {
                    "email": "admin@example.com",
                    "email_verified": True,
                },
            })
        )
        with patch.object(main.oauth, "google", fake_google, create=True):
            response = self.client.get(
                "/api/auth/google/callback",
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/admin/login?error=use_admin_sign_in")
        self.assertNotIn("edusmart_token", self.client.cookies)
        with self.session_factory() as db:
            self.assertEqual(db.query(User).filter_by(email="admin@example.com").count(), 0)

    def test_admin_login_fails_closed_when_password_hash_is_not_configured(self):
        main.ADMIN_PASSWORD_HASH = ""
        response = self.client.post("/api/admin/login", json={
            "email": "admin@example.com",
            "password": "test-admin-password",
        })
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.client.get("/admin").status_code, 503)


if __name__ == "__main__":
    unittest.main()

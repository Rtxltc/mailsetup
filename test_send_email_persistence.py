import asyncio
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


class FastAPI:
    def __init__(self, *args, **kwargs):
        pass

    def add_middleware(self, *args, **kwargs):
        pass

    def get(self, *args, **kwargs):
        def decorator(func):
            return func

        return decorator

    def post(self, *args, **kwargs):
        def decorator(func):
            return func

        return decorator

    def delete(self, *args, **kwargs):
        def decorator(func):
            return func

        return decorator


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class Request:
    pass


class UploadFile:
    pass


class CORSMiddleware:
    pass


class BaseModel:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


sys.modules.setdefault("fastapi", types.SimpleNamespace(FastAPI=FastAPI, HTTPException=HTTPException, Request=Request, UploadFile=UploadFile))
sys.modules.setdefault("fastapi.middleware", types.ModuleType("fastapi.middleware"))
sys.modules.setdefault("fastapi.middleware.cors", types.ModuleType("fastapi.middleware.cors"))
sys.modules["fastapi.middleware.cors"].CORSMiddleware = CORSMiddleware
sys.modules.setdefault("dotenv", types.SimpleNamespace(load_dotenv=lambda: None))
sys.modules.setdefault("requests", types.SimpleNamespace(post=lambda *args, **kwargs: None))
sys.modules.setdefault("uvicorn", types.ModuleType("uvicorn"))
sys.modules.setdefault("pydantic", types.SimpleNamespace(BaseModel=BaseModel))

import test as backend


class SendEmailPersistenceTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "mail_data.db")
        backend.DB_PATH = self.db_path
        backend.init_db()

    def test_send_email_persists_to_database(self):
        class FakeResponse:
            status_code = 200

            def json(self):
                return {"id": "fake-id"}

        with patch("test.requests.post", return_value=FakeResponse()):
            result = asyncio.run(
                backend.send_email(
                    backend.EmailRequest(
                        from_email="sender@example.com",
                        to="recipient@example.com",
                        subject="Hello",
                        text="Body",
                        html="<p>Body</p>",
                    )
                )
            )

        self.assertEqual(result["id"], "fake-id")

        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT sender, recipient, subject, html, text FROM emails ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()

        self.assertIsNotNone(row)
        self.assertEqual(row[0], "sender@example.com")
        self.assertEqual(row[1], "recipient@example.com")
        self.assertEqual(row[2], "Hello")
        self.assertEqual(row[3], "<p>Body</p>")
        self.assertEqual(row[4], "Body")


if __name__ == "__main__":
    unittest.main()

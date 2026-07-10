import json
import os
from datetime import datetime
import requests
import uvicorn
from fastapi import FastAPI, HTTPException, Request, UploadFile
from dotenv import load_dotenv

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    psycopg2 = None
    RealDictCursor = None

load_dotenv()

RESEND_API_KEY = os.getenv("RESEND_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

import hmac, hashlib

MAILGUN_SIGNING_KEY = os.getenv("MAILGUN_SIGNING_KEY")

def verify_mailgun_signature(token: str, timestamp: str, signature: str) -> bool:
    if not MAILGUN_SIGNING_KEY:
        return False
    hmac_digest = hmac.new(
        key=MAILGUN_SIGNING_KEY.encode(),
        msg=f"{timestamp}{token}".encode(),
        digestmod=hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(hmac_digest, signature)

def _is_postgres():
    return bool(DATABASE_URL and psycopg2)


def get_db_connection():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")

    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return conn


def init_db():
    conn = get_db_connection()
    conn.cursor().execute(
        """
        CREATE TABLE IF NOT EXISTS emails (
            id SERIAL PRIMARY KEY,
            sender TEXT,
            recipient TEXT,
            subject TEXT,
            html TEXT,
            text TEXT,
            preview TEXT,
            date TIMESTAMP,
            read BOOLEAN DEFAULT FALSE,
            starred BOOLEAN DEFAULT FALSE,
            deleted BOOLEAN DEFAULT FALSE,
            has_attachment BOOLEAN DEFAULT FALSE,
            raw_json JSONB
        )
        """
    )
    conn.cursor().execute(
        """
        CREATE TABLE IF NOT EXISTS attachments (
            id SERIAL PRIMARY KEY,
            email_id INTEGER NOT NULL,
            filename TEXT,
            path TEXT,
            content_type TEXT,
            size BIGINT,
            FOREIGN KEY (email_id) REFERENCES emails(id) ON DELETE CASCADE
        )
        """
    )
    conn.commit()
    conn.close()


def _extract_attachments(data: dict):
    attachments = []
    raw_attachments = data.get("attachments")

    if raw_attachments is None:
        return attachments

    if isinstance(raw_attachments, str):
        try:
            raw_attachments = json.loads(raw_attachments)
        except json.JSONDecodeError:
            raw_attachments = [raw_attachments]

    if isinstance(raw_attachments, list):
        for item in raw_attachments:
            if isinstance(item, dict):
                attachments.append(
                    {
                        "filename": item.get("filename") or item.get("name") or "",
                        "path": item.get("path") or item.get("url") or "",
                        "content_type": item.get("content_type") or item.get("type") or "",
                        "size": item.get("size") or 0,
                    }
                )
            elif isinstance(item, str):
                attachments.append(
                    {
                        "filename": item,
                        "path": item,
                        "content_type": "",
                        "size": 0,
                    }
                )

    return attachments


def save_incoming_data(data: dict, attachments: list[dict] | None = None):
    conn = get_db_connection()
    sender = data.get("from") or data.get("sender") or data.get("from_email") or ""
    recipient = data.get("to") or data.get("recipient") or data.get("to_email") or ""
    subject = data.get("subject") or ""
    html = data.get("html") or data.get("body-html") or data.get("body_html") or data.get("stripped-html") or ""
    text = data.get("text") or data.get("body-plain") or data.get("body_text") or data.get("stripped-text") or ""
    preview = data.get("preview") or (text[:160] if text else "")
    date_value = data.get("date") or datetime.utcnow().isoformat()
    attachments = attachments or _extract_attachments(data)

    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO emails (
            sender, recipient, subject, html, text, preview, date,
            read, starred, deleted, has_attachment, raw_json
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, FALSE, FALSE, FALSE, %s, %s)
        RETURNING id
        """,
        (
            sender,
            recipient,
            subject,
            html,
            text,
            preview,
            date_value,
            bool(attachments),
            json.dumps(data, default=str),
        ),
    )
    email_id = cursor.fetchone()[0]

    for attachment in attachments:
        cursor.execute(
            """
            INSERT INTO attachments (email_id, filename, path, content_type, size)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                email_id,
                attachment.get("filename", ""),
                attachment.get("path", ""),
                attachment.get("content_type", ""),
                attachment.get("size", 0),
            ),
        )

    conn.commit()
    conn.close()


def _build_email_objects(rows):
    result = []
    current_email = None

    for row in rows:
        if current_email is None or current_email["id"] != row["id"]:
            raw_json_val = row["raw_json"]
            if isinstance(raw_json_val, str):
                try:
                    raw_json_val = json.loads(raw_json_val)
                except Exception:
                    raw_json_val = {}
            elif not isinstance(raw_json_val, dict):
                raw_json_val = {}

            current_email = {
                "id": row["id"],
                "sender": row["sender"],
                "recipient": row["recipient"],
                "subject": row["subject"],
                "html": row["html"],
                "text": row["text"],
                "preview": row["preview"],
                "date": row["date"],
                "read": bool(row["read"]),
                "starred": bool(row["starred"]),
                "deleted": bool(row["deleted"]),
                "has_attachment": bool(row["has_attachment"]),
                "raw_json": raw_json_val,
                "attachments": [],
            }
            result.append(current_email)

        if row["attachment_id"] is not None:
            current_email["attachments"].append(
                {
                    "id": row["attachment_id"],
                    "filename": row["filename"],
                    "path": row["path"],
                    "content_type": row["content_type"],
                    "size": row["size"],
                }
            )

    return result


def get_saved_incoming_data(
    include_deleted: bool = False,
    search_query: str | None = None,
    folder: str | None = None,
):
    where_clauses = []
    params = []

    if folder == "trash":
        where_clauses.append("e.deleted = TRUE")
    elif folder == "starred":
        where_clauses.append("e.deleted = FALSE")
        where_clauses.append("e.starred = TRUE")
    elif folder == "sent":
        where_clauses.append("e.deleted = FALSE")
        where_clauses.append("e.raw_json->>'source' = 'sent'")
    elif folder == "inbox":
        where_clauses.append("e.deleted = FALSE")
        where_clauses.append("(e.raw_json->>'source' IS DISTINCT FROM 'sent')")
    else:
        if not include_deleted:
            where_clauses.append("e.deleted = FALSE")

    if search_query and search_query.strip():
        search_term = f"%{search_query.strip()}%"
        where_clauses.append(
            "(e.sender LIKE %s OR e.recipient LIKE %s OR e.subject LIKE %s OR e.text LIKE %s OR e.preview LIKE %s OR e.raw_json::text LIKE %s)"
        )
        params.extend([search_term] * 6)

    conn = get_db_connection()
    query = """
        SELECT
            e.id,
            e.sender,
            e.recipient,
            e.subject,
            e.html,
            e.text,
            e.preview,
            e.date,
            e.read,
            e.starred,
            e.deleted,
            e.has_attachment,
            e.raw_json,
            a.id AS attachment_id,
            a.filename,
            a.path,
            a.content_type,
            a.size
        FROM emails e
        LEFT JOIN attachments a ON a.email_id = e.id
    """

    if where_clauses:
        query += " WHERE " + " AND ".join(where_clauses)

    query += " ORDER BY e.id DESC, a.id ASC"

    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute(query, params)
    rows = cursor.fetchall()

    conn.close()

    return _build_email_objects(rows)


def get_email_by_id(email_id: int):
    conn = get_db_connection()
    query = """
        SELECT
            e.id,
            e.sender,
            e.recipient,
            e.subject,
            e.html,
            e.text,
            e.preview,
            e.date,
            e.read,
            e.starred,
            e.deleted,
            e.has_attachment,
            e.raw_json,
            a.id AS attachment_id,
            a.filename,
            a.path,
            a.content_type,
            a.size
        FROM emails e
        LEFT JOIN attachments a ON a.email_id = e.id
        WHERE e.id = %s
        ORDER BY a.id ASC
        """

    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute(query, (email_id,))
    rows = cursor.fetchall()

    conn.close()

    emails = _build_email_objects(rows)
    if not emails:
        raise HTTPException(status_code=404, detail="Email not found")
    return emails[0]


def update_email_status(email_id: int, field: str, value: bool):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(f"UPDATE emails SET {field} = %s WHERE id = %s", (value, email_id))
    conn.commit()
    conn.close()

    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Email not found")

    return get_email_by_id(email_id)


init_db()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://frontend-mailsetup.vercel.app",
        "https://ms.soulmatrix.in",
        "http://localhost:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from pydantic import BaseModel

class EmailRequest(BaseModel):
    from_email: str
    to: str
    subject: str
    text: str
    html: str | None = None

@app.get("/")
def home():
    return {"status": "running"}
@app.post("/send-email")
async def send_email(data: EmailRequest):
    payload = {
        "from": data.from_email,
        "to": [data.to],
        "subject": data.subject,
        "text": data.text,
    }

    if data.html:
        payload["html"] = data.html

    response = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
    )

    if response.status_code not in (200, 201):
        raise HTTPException(
            status_code=response.status_code,
            detail=response.json(),
        )

    save_incoming_data(
        {
            "from": data.from_email,
            "to": data.to,
            "subject": data.subject,
            "text": data.text,
            "html": data.html or "",
            "preview": (data.text[:160] if data.text else ""),
            "date": datetime.utcnow().isoformat(),
            "source": "sent",
        },
        attachments=[],
    )

    return response.json()

@app.post("/mail/incoming")
async def incoming(request: Request):
    form = await request.form()
    data = {}
    files = []

    for key, value in form.multi_items():
        if isinstance(value, UploadFile):
            file_bytes = await value.read()
            filename = value.filename or f"upload_{len(files) + 1}"
            safe_name = os.path.basename(filename)
            upload_dir = os.path.join(os.path.dirname(__file__), "uploads")
            os.makedirs(upload_dir, exist_ok=True)
            saved_path = os.path.join(
                upload_dir,
                f"{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}_{safe_name}",
            )
            with open(saved_path, "wb") as file_handle:
                file_handle.write(file_bytes)

            files.append(
                {
                    "filename": safe_name,
                    "path": saved_path,
                    "content_type": value.content_type or "",
                    "size": len(file_bytes),
                }
            )
        else:
            data[key] = str(value)

    if not verify_mailgun_signature(
        data.get("token", ""), data.get("timestamp", ""), data.get("signature", "")
    ):
        raise HTTPException(status_code=401, detail="Invalid signature")

    save_incoming_data(data, attachments=files)
    return {"success": True, "saved": True, "attachments": len(files)}


@app.get("/mail/incoming")
def get_incoming():
    return get_saved_incoming_data(folder="inbox")


@app.get("/mail/sent")
def get_sent():
    return get_saved_incoming_data(folder="sent")


@app.get("/mail/starred")
def get_starred():
    return get_saved_incoming_data(folder="starred")


@app.get("/mail/deleted")
def get_deleted():
    return get_saved_incoming_data(folder="trash")


@app.get("/mail/search")
def search_incoming(q: str):
    return get_saved_incoming_data(search_query=q)


@app.get("/mail/{email_id}")
def get_mail(email_id: int):
    return get_email_by_id(email_id)


@app.post("/mail/{email_id}/delete")
@app.delete("/mail/{email_id}")
def delete_mail(email_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE emails SET deleted = TRUE WHERE id = %s", (email_id,))
    conn.commit()
    conn.close()

    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Email not found")

    return {"success": True, "deleted": True, "id": email_id}


@app.post("/mail/{email_id}/read")
def mark_mail_read(email_id: int):
    return update_email_status(email_id, "read", True)


@app.post("/mail/{email_id}/star")
def star_mail(email_id: int):
    email = get_email_by_id(email_id)
    return update_email_status(email_id, "starred", not email["starred"])

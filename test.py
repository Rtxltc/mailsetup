import json
import os
import sqlite3
from datetime import datetime
import requests
import uvicorn
from fastapi import FastAPI, HTTPException, Request, UploadFile
from dotenv import load_dotenv

load_dotenv()

RESEND_API_KEY = os.getenv("RESEND_API_KEY")
DB_PATH = os.path.join(os.path.dirname(__file__), "mail_data.db")

from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT,
            recipient TEXT,
            subject TEXT,
            html TEXT,
            text TEXT,
            preview TEXT,
            date TEXT,
            read INTEGER DEFAULT 0,
            starred INTEGER DEFAULT 0,
            deleted INTEGER DEFAULT 0,
            has_attachment INTEGER DEFAULT 0,
            raw_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email_id INTEGER NOT NULL,
            filename TEXT,
            path TEXT,
            content_type TEXT,
            size INTEGER,
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
    conn = sqlite3.connect(DB_PATH)
    sender = data.get("from") or data.get("sender") or data.get("from_email") or ""
    recipient = data.get("to") or data.get("recipient") or data.get("to_email") or ""
    subject = data.get("subject") or ""
    html = data.get("html") or data.get("body_html") or ""
    text = data.get("text") or data.get("body_text") or ""
    preview = data.get("preview") or (text[:160] if text else "")
    date_value = data.get("date") or datetime.utcnow().isoformat()
    attachments = attachments or _extract_attachments(data)

    cursor = conn.execute(
        """
        INSERT INTO emails (
            sender, recipient, subject, html, text, preview, date,
            read, starred, deleted, has_attachment, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?, ?)
        """,
        (
            sender,
            recipient,
            subject,
            html,
            text,
            preview,
            date_value,
            1 if attachments else 0,
            json.dumps(data, default=str),
        ),
    )
    email_id = cursor.lastrowid

    for attachment in attachments:
        conn.execute(
            """
            INSERT INTO attachments (email_id, filename, path, content_type, size)
            VALUES (?, ?, ?, ?, ?)
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
                "raw_json": json.loads(row["raw_json"]) if row["raw_json"] else {},
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


def get_saved_incoming_data(include_deleted: bool = False, search_query: str | None = None):
    where_clauses = []
    params = []

    if not include_deleted:
        where_clauses.append("e.deleted = 0")

    if search_query and search_query.strip():
        search_term = f"%{search_query.strip()}%"
        where_clauses.append(
            "(e.sender LIKE ? OR e.recipient LIKE ? OR e.subject LIKE ? OR e.text LIKE ? OR e.preview LIKE ? OR e.raw_json LIKE ?)"
        )
        params.extend([search_term] * 6)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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
    rows = conn.execute(query, params).fetchall()
    conn.close()

    return _build_email_objects(rows)


def get_email_by_id(email_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
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
        WHERE e.id = ?
        ORDER BY a.id ASC
        """,
        (email_id,),
    ).fetchall()
    conn.close()

    emails = _build_email_objects(rows)
    if not emails:
        raise HTTPException(status_code=404, detail="Email not found")
    return emails[0]


def update_email_status(email_id: int, field: str, value: bool):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute(f"UPDATE emails SET {field} = ? WHERE id = ?", (1 if value else 0, email_id))
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

    save_incoming_data(data, attachments=files)
    return {"success": True, "saved": True, "attachments": len(files)}


@app.get("/mail/incoming")
def get_incoming():
    return get_saved_incoming_data()


@app.get("/mail/search")
def search_incoming(q: str):
    return get_saved_incoming_data(search_query=q)


@app.get("/mail/{email_id}")
def get_mail(email_id: int):
    return get_email_by_id(email_id)


@app.delete("/mail/{email_id}")
def delete_mail(email_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("UPDATE emails SET deleted = 1 WHERE id = ?", (email_id,))
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
@app.get("/db-test")
def db_test():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM emails")
    count = cur.fetchone()[0]
    conn.close()
    return {
        "connected": True,
        "emails": count
    }

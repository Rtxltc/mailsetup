import hmac
import hashlib
import json
import os
from datetime import datetime
import requests
import uvicorn
from fastapi import FastAPI, HTTPException, Request
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

# Mailgun-specific config
MAILGUN_API_KEY = os.getenv("MAILGUN_API_KEY")          # private API key, for fetching stored messages
MAILGUN_SIGNING_KEY = os.getenv("MAILGUN_SIGNING_KEY")  # HTTP webhook signing key, for verifying notify calls

from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()


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
            message_id TEXT UNIQUE,
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


# ---------------------------------------------------------------------------
# Mailgun signature verification
# ---------------------------------------------------------------------------

def verify_mailgun_signature(token: str, timestamp: str, signature: str) -> bool:
    if not MAILGUN_SIGNING_KEY or not token or not timestamp or not signature:
        return False
    hmac_digest = hmac.new(
        key=MAILGUN_SIGNING_KEY.encode("utf-8"),
        msg=f"{timestamp}{token}".encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(hmac_digest, signature)


# ---------------------------------------------------------------------------
# Fetching the stored message from Mailgun's Storage API
# ---------------------------------------------------------------------------

def fetch_stored_message(storage_url: str) -> dict:
    """
    GET the parsed message from Mailgun's storage. Returns a dict with fields
    like sender, recipient, subject, body-plain, body-html, stripped-text,
    stripped-html, Message-Id, attachments (list of {filename, content-type, size, url}).
    """
    if not MAILGUN_API_KEY:
        raise RuntimeError("MAILGUN_API_KEY is not configured")

    resp = requests.get(storage_url, auth=("api", MAILGUN_API_KEY), timeout=15)
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch stored message from Mailgun: {resp.status_code} {resp.text}",
        )
    return resp.json()


def download_attachment(url: str) -> bytes:
    resp = requests.get(url, auth=("api", MAILGUN_API_KEY), timeout=30)
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to download attachment from Mailgun: {resp.status_code}",
        )
    return resp.content


def _save_attachments_locally(message: dict, email_id: int, cursor):
    attachments = message.get("attachments") or []
    if isinstance(attachments, str):
        try:
            attachments = json.loads(attachments)
        except json.JSONDecodeError:
            attachments = []

    upload_dir = os.path.join(os.path.dirname(__file__), "uploads")
    os.makedirs(upload_dir, exist_ok=True)

    for item in attachments:
        if not isinstance(item, dict):
            continue
        filename = item.get("name") or item.get("filename") or "attachment"
        safe_name = os.path.basename(filename)
        content_type = item.get("content-type") or item.get("content_type") or ""
        size = item.get("size") or 0
        att_url = item.get("url")

        saved_path = ""
        if att_url:
            try:
                file_bytes = download_attachment(att_url)
                saved_path = os.path.join(
                    upload_dir,
                    f"{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}_{safe_name}",
                )
                with open(saved_path, "wb") as fh:
                    fh.write(file_bytes)
                size = len(file_bytes)
            except HTTPException:
                # keep going even if one attachment fails to download
                saved_path = ""

        cursor.execute(
            """
            INSERT INTO attachments (email_id, filename, path, content_type, size)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (email_id, safe_name, saved_path, content_type, size),
        )

    return len(attachments)


# ---------------------------------------------------------------------------
# Saving a fetched message to the DB
# ---------------------------------------------------------------------------

def save_incoming_message(message: dict):
    conn = get_db_connection()
    cursor = conn.cursor()

    sender = message.get("sender") or message.get("from") or ""
    recipient = message.get("recipient") or message.get("to") or ""
    subject = message.get("subject") or ""
    html = message.get("body-html") or message.get("stripped-html") or ""
    text = message.get("body-plain") or message.get("stripped-text") or ""
    preview = text[:160] if text else ""

    ts = message.get("timestamp")
    if ts:
        try:
            date_value = datetime.utcfromtimestamp(int(float(ts)))
        except (ValueError, TypeError):
            date_value = datetime.utcnow()
    else:
        date_value = datetime.utcnow()

    message_id = message.get("Message-Id") or message.get("message-id") or None

    # Idempotency: skip if we've already stored this message
    if message_id:
        cursor.execute("SELECT id FROM emails WHERE message_id = %s", (message_id,))
        existing = cursor.fetchone()
        if existing:
            conn.close()
            return existing[0]

    cursor.execute(
        """
        INSERT INTO emails (
            sender, recipient, subject, html, text, preview, date,
            read, starred, deleted, has_attachment, message_id, raw_json
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, FALSE, FALSE, FALSE, %s, %s, %s)
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
            bool(message.get("attachments")),
            message_id,
            json.dumps(message, default=str),
        ),
    )
    email_id = cursor.fetchone()[0]

    _save_attachments_locally(message, email_id, cursor)

    conn.commit()
    conn.close()
    return email_id


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
        where_clauses.append("e.deleted = FALSE")

    if search_query and search_query.strip():
        search_term = f"%{search_query.strip()}%"
        where_clauses.append(
            "(e.sender ILIKE %s OR e.recipient ILIKE %s OR e.subject ILIKE %s OR e.text ILIKE %s OR e.preview ILIKE %s OR e.raw_json::text ILIKE %s)"
        )
        params.extend([search_term] * 6)

    conn = get_db_connection()
    query = """
        SELECT
            e.id, e.sender, e.recipient, e.subject, e.html, e.text, e.preview,
            e.date, e.read, e.starred, e.deleted, e.has_attachment, e.raw_json,
            a.id AS attachment_id, a.filename, a.path, a.content_type, a.size
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
            e.id, e.sender, e.recipient, e.subject, e.html, e.text, e.preview,
            e.date, e.read, e.starred, e.deleted, e.has_attachment, e.raw_json,
            a.id AS attachment_id, a.filename, a.path, a.content_type, a.size
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
    rowcount = cursor.rowcount
    conn.close()

    if rowcount == 0:
        raise HTTPException(status_code=404, detail="Email not found")

    return get_email_by_id(email_id)


init_db()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://frontend-mailsetup.vercel.app",
        "https://ms.soulmatrix.in",
        "https://ms.soulmatrix.in/inbox",
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
        raise HTTPException(status_code=response.status_code, detail=response.json())

    save_incoming_message(
        {
            "sender": data.from_email,
            "recipient": data.to,
            "subject": data.subject,
            "body-plain": data.text,
            "body-html": data.html or "",
            "source": "sent",
        }
    )

    return response.json()


@app.post("/mail/incoming")
async def incoming(request: Request):
    """
    Mailgun `store()` + `notify=` webhook handler.

    Mailgun POSTs JSON here that looks like:
    {
      "signature": {"timestamp": "...", "token": "...", "signature": "..."},
      "event-data": {
        "storage": {"url": "https://storage-xxx.mailgun.net/v3/domains/.../messages/..."},
        ...
      }
    }
    We verify the signature, then fetch the full parsed message from the
    storage URL using the Mailgun API key, then save it.
    """
    body = await request.json()

    sig = body.get("signature", {}) or {}
    if not verify_mailgun_signature(
        sig.get("token", ""), sig.get("timestamp", ""), sig.get("signature", "")
    ):
        raise HTTPException(status_code=401, detail="Invalid Mailgun signature")

    event_data = body.get("event-data", {}) or {}
    storage = event_data.get("storage", {}) or {}
    storage_url = storage.get("url")

    if not storage_url:
        raise HTTPException(status_code=400, detail="No storage URL in notify payload")

    message = fetch_stored_message(storage_url)
    email_id = save_incoming_message(message)

    return {"success": True, "saved": True, "email_id": email_id}


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
    return update_email_status(email_id, "deleted", True)


@app.post("/mail/{email_id}/read")
def mark_mail_read(email_id: int):
    return update_email_status(email_id, "read", True)


@app.post("/mail/{email_id}/star")
def star_mail(email_id: int):
    email = get_email_by_id(email_id)
    return update_email_status(email_id, "starred", not email["starred"])


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)

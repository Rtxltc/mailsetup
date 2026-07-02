import os
import requests

from fastapi import FastAPI, HTTPException
from dotenv import load_dotenv

load_dotenv()

RESEND_API_KEY = os.getenv("RESEND_API_KEY")

app = FastAPI()

from pydantic import BaseModel

class EmailRequest(BaseModel):
    from_email: str
    to: str
    subject: str
    text: str


@app.post("/send-email")
async def send_email(data: EmailRequest):
    response = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "from": data.from_email,
            "to": [data.to],
            "subject": data.subject,
            "text": data.text,
        },
    )

    if response.status_code not in (200, 201):
        raise HTTPException(
            status_code=response.status_code,
            detail=response.json(),
        )

    return response.json()

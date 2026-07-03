import os
import requests
import uvicorn
from fastapi import FastAPI, HTTPException
from dotenv import load_dotenv

load_dotenv()

RESEND_API_KEY = os.getenv("RESEND_API_KEY")

from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://frontend-mailsetup.vercel.app",
        
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

    return response.json()

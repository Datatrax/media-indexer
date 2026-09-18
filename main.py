import os
import re
import secrets
from typing import Dict, Any
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaDocument
from fastapi import FastAPI, Request, Query, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import uvicorn
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
SESSION_STRING = os.getenv("SESSION_STRING")
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "cambia_esta_password")

app = FastAPI(title="Media Indexer")
templates = Jinja2Templates(directory="templates")
security = HTTPBasic()

catalog = {"movies": [], "series": [], "music": [], "other": []}

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

def verify_credentials(credentials: HTTPBasicCredentials = Depends(security)):
    correct_user = secrets.compare_digest(credentials.username, ADMIN_USER)
    correct_pass = secrets.compare_digest(credentials.password, ADMIN_PASS)
    if not (correct_user and correct_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales incorrectas",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

def extract_info(filename: str, caption: str = "") -> Dict[str, Any]:
    text = f"{filename} {caption}".lower()
    year_match = re.search(r"(19|20)\d{2}", text)
    year = year_match.group(0) if year_match else None

    se_match = re.search(
        r"(?:s|season|temporada)[\s._-]*(\d{1,2})[\s._-]*(?:e|ep|episode|capítulo|cap)[\s._-]*(\d{1,3})",
        text, re.IGNORECASE
    )
    if not se_match:
        se_match = re.search(r"(\d{1,2})[xX](\d{1,3})", text)

    season = int(se_match.group(1)) if se_match else None
    episode = int(se_match.group(2)) if se_match else None

    quality = None
    for q in ["2160p", "4k", "1080p", "720p", "480p", "360p"]:
        if q in text:
            quality = q.upper()
            break

    clean_title = re.sub(r"\.(mkv|mp4|avi|mov|mp3|flac|m4a|ogg)$", "", filename, flags=re.I)
    clean_title = re.sub(r"[\._]", " ", clean_title)
    clean_title = re.sub(r"\s+", " ", clean_title).strip()

    return {"title": clean_title, "year": year, "season": season, "episode": episode, "quality": quality}

def classify(filename: str, mime: str = "", caption: str = "") -> str:
    text = (filename + " " + caption).lower()
    if any(x in text for x in [".mp3", ".flac", ".m4a", ".ogg", ".wav", "music", "song", "album", "ost"]) or "audio/" in mime:
        return "music"
    if re.search(r"(s\d{1,2}e\d{1,2}|season|temporada|capítulo|episode|cap\.|\d{1,2}x\d{1,3})", text, re.I):
        return "series"
    if any(x in text for x in [".mkv", ".mp4", ".avi", ".mov", "1080p", "720p", "2160p", "4k", "bluray", "web-dl", "webrip"]):
        return "movies"
    return "other"

async def reindex():
    global catalog
    catalog = {"movies": [], "series": [], "music": [], "other": []}
    print("Iniciando re-indexación...")
    count = 0

    async for msg in client.iter_messages(CHANNEL_ID, limit=None):
        if not msg.media or not isinstance(msg.media, MessageMediaDocument):
            continue

        doc = msg.media.document
        filename = "Sin nombre"
        for attr in doc.attributes:
            if hasattr(attr, "file_name") and attr.file_name:
                filename = attr.file_name
                break

        mime = doc.mime_type or ""
        size = doc.size or 0
        caption = msg.message or ""
        info = extract_info(filename, caption)

        item = {
            "id": msg.id,
            "filename": filename,
            "title": info["title"],
            "year": info["year"],
            "season": info["season"],
            "episode": info["episode"],
            "quality": info["quality"],
            "size_mb": round(size / (1024 * 1024), 2),
            "mime": mime,
            "caption": caption[:200],
            "date": msg.date.strftime("%Y-%m-%d") if msg.date else "",
            "link": f"https://t.me/c/{str(CHANNEL_ID)[4:]}/{msg.id}"
        }

        tipo = classify(filename, mime, caption)
        catalog[tipo].append(item)
        count += 1
        if count % 100 == 0:
            print(f"Indexados {count} archivos...")

    for key in catalog:
        catalog[key].sort(key=lambda x: x["date"], reverse=True)

    print(f"✅ Completado: {count} archivos")
    print(f"Películas: {len(catalog['movies'])} | Series: {len(catalog['series'])} | Música: {len(catalog['music'])}")

@app.on_event("startup")
async def startup_event():
    await client.start(bot_token=BOT_TOKEN)
    await reindex()

@app.get("/", response_class=HTMLResponse)
async def home(request: Request, category: str = "all", q: str = "", username: str = Depends(verify_credentials)):
    items = []
    if category == "all":
        for cat in catalog.values():
            items.extend(cat)
    else:
        items = catalog.get(category, [])

    if q:
        q_lower = q.lower()
        items = [i for i in items if q_lower in i["filename"].lower() or q_lower in i["title"].lower() or q_lower in i["caption"].lower()]

    return templates.TemplateResponse("index.html", {
        "request": request,
        "items": items[:300],
        "total": sum(len(v) for v in catalog.values()),
        "counts": {k: len(v) for k, v in catalog.items()},
        "category": category,
        "q": q
    })

@app.get("/api/search")
async def api_search(q: str = Query(...), username: str = Depends(verify_credentials)):
    q = q.lower()
    results = []
    for cat in catalog.values():
        for item in cat:
            if q in item["filename"].lower() or q in item["title"].lower() or q in item["caption"].lower():
                results.append(item)
    return results[:100]

@app.get("/health")
async def health():
    return {"status": "ok", "total_files": sum(len(v) for v in catalog.values())}

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
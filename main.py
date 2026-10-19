import logging
import os
import re
import zipfile
import tarfile
from datetime import datetime, timedelta
import urllib3
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse
# --- [ГЛАВНОЕ ИСПРАВЛЕНИЕ] ИМПОРТ CORS ---
from fastapi.middleware.cors import CORSMiddleware
# -----------------------------------------
from pydantic import BaseModel

from exchangelib import (
    Credentials, Account, Configuration, DELEGATE, NTLM, Build, Version,
    CalendarItem, Body, Mailbox, Attendee, FileAttachment, Folder, Message
)
from exchangelib.protocol import BaseProtocol, NoVerifyHTTPAdapter

# ===========================
# ⚙️ НАСТРОЙКИ
# ===========================
from exchange_service import (
    build_readiness,
    get_vpn_status,
    resolve_exchange_server,
    check_exchange_route,
    account_is_cached,
)
from exchange_storage import (
    open_storage as _open_exchange_storage,
    message_record_from_exchange,
    make_message_ref,
)
from exchange_mail_service import (
    send_message as _send_message_service,
    reply_message as _reply_message_service,
    MailTargetNotFound,
    MailSendError,
)
from exchange_attachment_service import (
    list_attachments_for_message as _list_attachments_service,
    download_attachment as _download_attachment_service,
    AttachmentNotFound,
    AttachmentSourceError,
)
from exchange_rag_service import (
    index_rag_documents as _index_rag_documents_service,
    search_rag_documents as _search_rag_documents_service,
    get_rag_document as _get_rag_document_service,
    RagDocNotFound,
    RagRootUnsafe,
)
EXCHANGE_USER = os.environ.get("EXCHANGE_USER", "DOMAIN\\username")
EXCHANGE_PASS = os.environ.get("EXCHANGE_PASS", "")
EXCHANGE_EMAIL = os.environ.get("EXCHANGE_EMAIL", "user@example.com")
EXCHANGE_SERVER = os.environ.get("EXCHANGE_SERVER", "exchange.example.com")

RAG_DATA_DIR = "rag_data"
MIN_IMAGE_SIZE = 5 * 1024 
IMAGE_EXTENSIONS = ['.jpg', '.jpeg', '.png', '.bmp', '.gif', '.webp', '.tiff']

# 👇 ЖЕЛЕЗОБЕТОННАЯ ТАЙМЗОНА (Стандарт Python)
MSK_TZ = ZoneInfo("Europe/Moscow")

BaseProtocol.HTTP_ADAPTER_CLS = NoVerifyHTTPAdapter
urllib3.disable_warnings()

app = FastAPI(title="Exchange V14 (ZoneInfo Fix)")

# --- CORS ---
CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "http://localhost:3000,http://localhost:8080,http://localhost:5015").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Exchange")

if not os.path.exists(RAG_DATA_DIR):
    os.makedirs(RAG_DATA_DIR)

# === СТАТУС ФОНОВЫХ ЗАДАЧ ===
download_status = {"running": False, "folder": "", "progress": "", "error": None}

# === МОДЕЛИ ===
class CalendarRequest(BaseModel):
    date: str = "today" 

class SearchCalendarRequest(BaseModel):
    query: str
    days: int = 30 

class CalendarRangeRequest(BaseModel):
    """Structured calendar range request for external calendar sync."""
    start: str = None
    end: str = None
    days_back: int = 7
    days_forward: int = 60

class NewEventRequest(BaseModel):
    subject: str
    start_time: str 
    end_time: str   
    body: str = ""
    location: str = ""
    attendees: list[str] = []

class DeleteEventRequest(BaseModel):
    subject: str

class SendWorkEmailRequest(BaseModel):
    to_email: str
    subject: str
    body: str

class ReplyWorkEmailRequest(BaseModel):
    message_id: str
    body: str
    subject: str = None

class DownloadRequest(BaseModel):
    limit_per_folder: int = 100
    folder: str = "archive_inbox"
    download_attachments: bool = True
    extract_archives: bool = True
    recursive: bool = True

class ListFoldersRequest(BaseModel):
    target: str = "archive"

class CheckNewRequest(BaseModel):
    minutes: int = 15

# === W2a: read-only mail API models ===
class ListMessagesRequest(BaseModel):
    folder: str = None               # имя Exchange-папки, None = все
    limit: int = 50                  # max кол-во записей
    offset: int = 0
    received_after: str = None       # ISO-8601
    received_before: str = None
    refresh: bool = False            # если True — сначала синхронизировать кеш из Exchange

class SearchMessagesRequest(BaseModel):
    query: str
    folder: str = None
    limit: int = 50
    offset: int = 0
    received_after: str = None
    received_before: str = None

class GetMessageRequest(BaseModel):
    # Один из двух ниже. message_ref возвращается /list_messages и /search_messages.
    message_ref: str = None
    item_id: str = None              # raw EWS item id (использовать только если клиент его знает)

# === W2b: destructive mail API models ===
class SendMessageRequest(BaseModel):
    """Schema for POST /send_message (MCP-shaped)."""
    to: list[str] = []
    cc: list[str] = []
    bcc: list[str] = []
    subject: str = ""
    body: str = ""

class ReplyMessageRequest(BaseModel):
    """Schema for POST /reply_message (MCP-shaped).
    Prefer `message_ref` (returned by /list_messages, /search_messages).
    `item_id` is kept for legacy clients that already hold the raw EWS id.
    """
    message_ref: str = None
    item_id: str = None
    body: str = ""
    subject: str = None

# === W2c: attachment read/download API models ===
class ListAttachmentsRequest(BaseModel):
    """Schema for POST /list_attachments (MCP-shaped).

    Provide either `message_ref` (preferred, returned by /list_messages) or
    the legacy `item_id` (raw EWS id). At least one is required.
    """
    message_ref: str = None
    item_id: str = None


class DownloadAttachmentRequest(BaseModel):
    """Schema for POST /download_attachment (MCP-shaped).

    Provide either `message_ref` or `item_id` (one is required).
    Pick the attachment by exactly one of: `attachment_ref`, `attachment_id`,
    or `filename`. The target directory defaults to ``rag_data/attachments``.
    """
    message_ref: str = None
    item_id: str = None
    attachment_ref: str = None
    attachment_id: str = None
    filename: str = None
    target_dir: str = "rag_data/attachments"

# === W2d: local-file RAG indexing models ===
class IndexRagDocumentsRequest(BaseModel):
    """Schema for POST /index_rag_documents.

    Walks ``root_dir`` (default ``rag_data``) and upserts every text-like
    file into the SQLite rag_documents index. Operates entirely on local
    files already downloaded by /download_dataset — never touches
    Exchange and never requires VPN. Returns 404 if ``root_dir`` is
    missing/invalid; 200 with structured stats otherwise.
    """
    root_dir: str = "rag_data"
    limit: int = 1000
    max_file_bytes: int = 2_000_000
    extensions: list[str] = []      # empty list → default text-like set
    force: bool = False


class SearchRagDocumentsRequest(BaseModel):
    """Schema for POST /search_rag_documents.

    Substring search over filename + text_excerpt. No VPN/Exchange
    required. Missing query → 400.
    """
    query: str = ""
    limit: int = 20
    root_dir: str = None           # optional filter by resolved root


class GetRagDocumentRequest(BaseModel):
    """Schema for POST /get_rag_document.

    Provide the opaque ``doc_ref`` returned by /index_rag_documents or
    /search_rag_documents. With ``include_text=True`` the response also
    carries a ``text`` field (capped at ``max_chars``). Missing doc_ref
    → 400; unknown doc_ref → 404.
    """
    doc_ref: str = ""
    include_text: bool = False
    max_chars: int = 20000


# === VPN CHECK ===
def is_vpn_active() -> bool:
    """Check if Cisco AnyConnect VPN is active by looking for vpn0 interface with an IP."""
    import subprocess
    try:
        result = subprocess.run(
            ["ip", "-br", "addr", "show", "vpn0"],
            capture_output=True, text=True, timeout=3
        )
        # Example output: "vpn0  UNKNOWN  10.255.0.1/21  global  vpn_zone"
        if result.returncode == 0 and result.stdout.strip():
            # Has an IP assigned
            parts = result.stdout.strip().split()
            if len(parts) >= 3:
                logger.info("VPN active: vpn0=%s", parts[2])
                return True
    except Exception:
        pass
    return False

# === ПОДКЛЮЧЕНИЕ (с кешированием) ===
_cached_account = None
_account_created_at = None
ACCOUNT_TTL = 300  # секунд жизни кеша

def get_account():
    global _cached_account, _account_created_at
    now = datetime.now(MSK_TZ)
    if _cached_account and _account_created_at and (now - _account_created_at).total_seconds() < ACCOUNT_TTL:
        return _cached_account

    # Проверка VPN — не пытаемся авторизоваться без VPN (3 попытки = блокировка!)
    if not is_vpn_active():
        logger.warning("VPN is DOWN — skipping Exchange auth to prevent account lockout")
        return None

    try:
        creds = Credentials(username=EXCHANGE_USER, password=EXCHANGE_PASS)
        config = Configuration(server=EXCHANGE_SERVER, credentials=creds, auth_type=NTLM, version=Version(build=Build(15, 1)))
        _cached_account = Account(primary_smtp_address=EXCHANGE_EMAIL, config=config, autodiscover=False, access_type=DELEGATE)
        _account_created_at = now
        logger.info("Exchange account connected (cached for %ds)", ACCOUNT_TTL)
        return _cached_account
    except Exception as e:
        logger.error(f"Account Error: {e}")
        _cached_account = None
        _account_created_at = None
        return None

def _account_cached_predicate() -> bool:
    """Predicate used by exchange_service.readiness to check the account cache
    without importing exchangelib in the service module."""
    if _cached_account is None or _account_created_at is None:
        return False
    try:
        now = datetime.now(MSK_TZ)
        return (now - _account_created_at).total_seconds() < ACCOUNT_TTL
    except Exception:
        return bool(_cached_account)

# === УТИЛИТЫ ===
def get_archive_inbox(account):
    """Находит папку Inbox в архиве Exchange. Возвращает None при ошибке."""
    try:
        archive_root = account.archive_msg_folder_root
        for child in archive_root.children:
            if child.folder_class == 'IPF.Note' and child.name.lower() in ['inbox', 'входящие']:
                return child
        return account.archive_root
    except Exception as e:
        logger.warning(f"Archive inbox lookup failed: {e}")
        return None

def clean_filename(text):
    if not text: return "NoName"
    safe_text = re.sub(r'[\\/*?:"<>|]', '_', text).replace('\n', '').replace('\r', '')
    return safe_text[:100].strip()

def extract_archive(archive_path, extract_root):
    extracted_files = []
    folder_name = os.path.splitext(os.path.basename(archive_path))[0] + "_EXTRACTED"
    target_dir = os.path.join(extract_root, folder_name)
    if not os.path.exists(target_dir): os.makedirs(target_dir)
    try:
        if zipfile.is_zipfile(archive_path):
            with zipfile.ZipFile(archive_path, 'r') as z: z.extractall(target_dir)
        elif tarfile.is_tarfile(archive_path):
            with tarfile.open(archive_path, 'r') as t: t.extractall(target_dir, filter='data')
        else: return []
        for root, _, files in os.walk(target_dir):
            for file in files:
                if not file.startswith('.'):
                    extracted_files.append(os.path.relpath(os.path.join(root, file), extract_root))
    except Exception as e:
        logger.warning(f"Failed to extract archive {archive_path}: {e}")
    return extracted_files

def process_single_folder(folder, limit, with_attachments, extract_zips):
    total_items = folder.total_count
    logger.info(f"📂 FOLDER: {folder.name} | Items: {total_items}")
    if folder.folder_class != 'IPF.Note' or total_items == 0: return 0
    try:
        qs = folder.all().order_by('-datetime_received')
        if limit > 0: qs = qs[:limit]
        count = 0
        for item in qs:
            if item.item_class != 'IPM.Note': continue
            subject = item.subject or "No Subject"
            received = item.datetime_received or datetime.now(MSK_TZ)
            date_str = received.strftime("%Y-%m-%d_%H-%M")
            base_name = f"{date_str}___{clean_filename(subject)}"
            
            if with_attachments and item.attachments:
                for att in item.attachments:
                    if isinstance(att, FileAttachment):
                        att_name = clean_filename(att.name)
                        ext = os.path.splitext(att_name)[1].lower()
                        size = att.size or 0
                        if ext in IMAGE_EXTENSIONS and size < MIN_IMAGE_SIZE: continue
                        att_path = os.path.join(RAG_DATA_DIR, f"{base_name}___ATT__{att_name}")
                        if not os.path.exists(att_path):
                            with open(att_path, "wb") as f: f.write(att.content)
                            if extract_zips and ext in ['.zip', '.tar', '.gz']:
                                extract_archive(att_path, RAG_DATA_DIR)
            txt_path = os.path.join(RAG_DATA_DIR, f"{base_name}.txt")
            if not os.path.exists(txt_path):
                body = item.text_body or (re.sub('<[^<]+?>', '', str(item.body)) if item.body else "")
                sender_name = item.sender.name if item.sender else 'Unknown'
                sender_email = item.sender.email_address if item.sender else 'Unknown'
                content = f"Subject: {subject}\nFrom: {sender_name} <{sender_email}>\nDate: {received}\nFolder: {folder.name}\n{'-'*50}\n{body}"
                with open(txt_path, "w", encoding="utf-8") as f: f.write(content)
                count += 1
                if count % 100 == 0: logger.info(f"   ⬇️ {folder.name}: {count}...")
        if count > 0: logger.info(f"   ✅ {folder.name}: Downloaded {count}")
        return count
    except Exception as e:
        logger.warning(f"   ⚠️ Error in {folder.name}: {e}")
        return 0

def recursive_walker(folder, limit, with_attachments, extract_zips):
    total = process_single_folder(folder, limit, with_attachments, extract_zips)
    try:
        for child in folder.children:
            total += recursive_walker(child, limit, with_attachments, extract_zips)
    except Exception as e:
        logger.warning(f"Error walking children of {folder.name}: {e}")
    return total

def background_download_task(limit: int, folder_name: str, with_attachments: bool, extract_zips: bool, recursive: bool):
    download_status.update(running=True, folder=folder_name, progress="connecting", error=None)
    account = get_account()
    if not account:
        download_status.update(running=False, progress="failed", error="No connection")
        return
    logger.info(f"🚀 SEARCH: '{folder_name}' (Recurse={recursive}, Limit={limit})")
    target = None
    if folder_name.lower() == "archive_inbox":
        target = get_archive_inbox(account)
        if not target:
            download_status.update(running=False, progress="failed", error="Archive inbox not found")
            logger.error("Archive inbox not found"); return
    elif folder_name.lower() == "inbox": target = account.inbox
    elif folder_name.lower() == "root": target = account.root
    elif folder_name.lower() == "archive_root":
        try: target = account.archive_root
        except Exception as e: logger.warning(f"Archive root unavailable: {e}")
    else:
        for f in account.root.walk():
            if f.name and f.name.lower() == folder_name.lower(): target = f; break
        if not target and hasattr(account, 'archive_root'):
            try:
                for f in account.archive_root.walk():
                    if f.name and f.name.lower() == folder_name.lower(): target = f; break
            except Exception as e:
                logger.warning(f"Error searching archive for '{folder_name}': {e}")
    if not target:
        download_status.update(running=False, progress="failed", error=f"Folder '{folder_name}' not found")
        logger.error(f"Folder '{folder_name}' not found!"); return
    logger.info(f"🎯 TARGET: {target.name}")
    download_status["progress"] = f"downloading from {target.name}"
    if recursive: recursive_walker(target, limit, with_attachments, extract_zips)
    else: process_single_folder(target, limit, with_attachments, extract_zips)
    download_status.update(running=False, progress="done", error=None)
    logger.info(f"🏁 DONE")

def print_tree(folder, indent=0, results=None):
    if results is None:
        results = []
    try:
        line = f"{'  ' * indent}📂 {folder.name} ({folder.total_count})"
        results.append(line)
        for child in folder.children: print_tree(child, indent + 1, results)
    except Exception as e:
        logger.warning(f"Error reading folder tree at {getattr(folder, 'name', '?')}: {e}")

# ===========================
# API
# ===========================

@app.post("/check_archive_new")
def check_new_emails_in_archive(req: CheckNewRequest):
    account = get_account()
    if not account:
        raise HTTPException(status_code=503, detail="Exchange connection failed")
    try:
        target_folder = get_archive_inbox(account)
        if not target_folder:
            raise HTTPException(status_code=503, detail="Archive unavailable")

        now = datetime.now(MSK_TZ)
        time_threshold = now - timedelta(minutes=req.minutes)
        
        new_items = target_folder.filter(datetime_received__gt=time_threshold).order_by('-datetime_received')
        
        found_emails = []
        for item in new_items:
            if item.item_class != 'IPM.Note': continue
            raw_body = item.text_body or ""
            if not raw_body and item.body: raw_body = re.sub('<[^<]+?>', '', str(item.body))
            body_preview = raw_body[:2000].strip()
            
            # Конвертация через astimezone(MSK_TZ)
            item_date = item.datetime_received.astimezone(MSK_TZ) if item.datetime_received else now
            
            found_emails.append({
                "id": item.id,
                "time": item_date.strftime("%H:%M"),
                "sender": item.sender.name if item.sender else "Unknown",
                "sender_email": item.sender.email_address if item.sender else "Unknown",
                "subject": item.subject or "(No Subject)",
                "body": body_preview
            })
        if not found_emails: return {"status": "empty", "count": 0}
        return {"status": "new_mail", "count": len(found_emails), "emails": found_emails}
    except Exception as e: return {"status": "error", "message": str(e)}

@app.post("/reply_work_email")
def reply_exchange_email(req: ReplyWorkEmailRequest):
    """Legacy reply endpoint. Preserved for backward compatibility —
    delegates to the W2b service layer so audit is written and the VPN
    guard is honoured. Response shape on success is unchanged:
        {"status": "success", "message": "Replied"}
    """
    account = get_account()
    if not account:
        raise HTTPException(status_code=503, detail="Exchange connection failed")
    storage = _open_exchange_storage()
    try:
        _reply_message_service(
            account,
            storage,
            message_ref=None,
            item_id=req.message_id,
            body=req.body,
            subject=req.subject,
            inbox_lookup=_legacy_inbox_lookup,
            archive_lookup=_legacy_archive_inbox_lookup,
        )
        return {"status": "success", "message": "Replied"}
    except MailTargetNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except MailSendError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def _legacy_inbox_lookup(account, item_id: str):
    """Match the legacy /reply_work_email behaviour: only try inbox; if it
    raises, return None so the archive fallback runs."""
    try:
        return account.inbox.get(id=item_id)
    except Exception:
        return None


def _legacy_archive_inbox_lookup(account, item_id: str):
    """Legacy archive inbox lookup — same logic as the legacy route."""
    try:
        target_folder = get_archive_inbox(account)
        if target_folder:
            return target_folder.get(id=item_id)
    except Exception:
        return None
    return None


@app.post("/send_work_email")
def send_exchange_email(req: SendWorkEmailRequest):
    """Legacy send endpoint. Preserved for backward compatibility —
    delegates to the W2b service layer. Response shape on success is
    unchanged: {"status": "success", "message": "Sent"}."""
    account = get_account()
    if not account:
        raise HTTPException(status_code=503, detail="Exchange connection failed")
    storage = _open_exchange_storage()
    try:
        _send_message_service(
            account,
            storage,
            to=req.to_email,
            subject=req.subject,
            body=req.body,
        )
        return {"status": "success", "message": "Sent"}
    except MailSendError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/list_folders")
def list_folders_structure(req: ListFoldersRequest):
    account = get_account()
    if not account:
        raise HTTPException(status_code=503, detail="Exchange connection failed")
    results = []
    try:
        root = account.archive_msg_folder_root if req.target == "archive" else account.msg_folder_root
        print_tree(root, 0, results)
        return {"status": "success", "tree": "\n".join(results)}
    except Exception as e: return {"status": "error", "message": str(e)}

@app.post("/download_dataset")
def start_download(req: DownloadRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(background_download_task, req.limit_per_folder, req.folder, req.download_attachments, req.extract_archives, req.recursive)
    return {"status": "started", "message": "Started"}

@app.get("/download_status")
def get_download_status():
    return download_status

# ===========================
# ❤️ / 🟢 HEALTH & READINESS (additive, W1)
# ===========================

@app.get("/health")
def health():
    """Liveness probe. Always returns 200 if the process is up.

    Intentionally cheap: does NOT touch VPN/DNS/Exchange.
    """
    return {"status": "ok", "service": "exchange-connector"}

@app.get("/ready")
def ready():
    """Readiness probe with structured VPN/DNS/route/account fields.

    * 200 when `ready=True`
    * 503 when any readiness step fails

    Never attempts Exchange auth — preserves the vpn0 guard.
    """
    try:
        payload = build_readiness(
            server=EXCHANGE_SERVER,
            is_account_cached=_account_cached_predicate,
        )
    except Exception as e:
        logger.exception("readiness probe failed")
        raise HTTPException(
            status_code=503,
            detail={
                "ready": False,
                "reason": f"internal_error:{type(e).__name__}",
                "service_alive": True,
            },
        )
    status_code = 200 if payload.get("ready") else 503
    return JSONResponse(status_code=status_code, content=payload)

# ===========================
# 📅 CALENDAR (FIXED for Python 3.12)
# ===========================

@app.post("/add_event")
def create_event(req: NewEventRequest):
    account = get_account()
    if not account:
        raise HTTPException(status_code=503, detail="Exchange connection failed")
    try:
        # 1. Parse time
        dt_start = datetime.strptime(req.start_time, "%Y-%m-%d %H:%M")
        dt_end = datetime.strptime(req.end_time, "%Y-%m-%d %H:%M")

        # 2. Localize using standard .replace(tzinfo=...)
        # Это работает в Python 3.12 c ZoneInfo
        start_tz = dt_start.replace(tzinfo=MSK_TZ)
        end_tz = dt_end.replace(tzinfo=MSK_TZ)

        item = CalendarItem(
            account=account, 
            folder=account.calendar, 
            subject=req.subject, 
            start=start_tz, 
            end=end_tz, 
            body=Body(req.body), 
            location=req.location
        )

        if req.attendees:
            clean_attendees = [a.strip() for a in req.attendees if a.strip()]
            item.required_attendees = [Attendee(mailbox=Mailbox(email_address=a)) for a in clean_attendees]
            item.save(send_meeting_invitations='SendToAllAndSaveCopy')
        else:
            item.save(send_meeting_invitations='SendToNone')
            
        return {"status": "success"}
    except Exception as e: return {"status": "error", "message": str(e)}

@app.post("/delete_event")
def delete_event(req: DeleteEventRequest):
    account = get_account()
    if not account:
        raise HTTPException(status_code=503, detail="Exchange connection failed")
    try:
        items = account.calendar.filter(subject=req.subject)
        if items.count() == 0: return {"status": "warning"}
        items.delete(delete_type='MoveToDeletedItems')
        return {"status": "success"}
    except Exception as e: return {"status": "error", "message": str(e)}

@app.post("/get_calendar")
def read_calendar(req: CalendarRequest):
    account = get_account()
    if not account:
        raise HTTPException(status_code=503, detail="Exchange connection failed")
    try:
        now_msk = datetime.now(MSK_TZ)
        
        if req.date in ["today", "сегодня"]:
            target_date = now_msk.date()
        elif req.date in ["tomorrow", "завтра"]:
            target_date = now_msk.date() + timedelta(days=1)
        else:
            try:
                target_date = datetime.strptime(req.date, "%Y-%m-%d").date()
            except ValueError:
                return {"status": "error", "result": f"Invalid date: {req.date}"}

        # 3. Create bounds with timezone
        s = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0, tzinfo=MSK_TZ)
        e = datetime(target_date.year, target_date.month, target_date.day, 23, 59, 59, tzinfo=MSK_TZ)
        
        items = account.calendar.view(start=s, end=e)
        
        res = []
        for i in items:
            local_s = i.start.astimezone(MSK_TZ).strftime('%H:%M')
            local_e = i.end.astimezone(MSK_TZ).strftime('%H:%M')
            res.append(f"• {local_s}-{local_e}: {i.subject}")
            
        return {"result": "\n".join(res) if res else f"No events on {target_date}."}
    except Exception as e: return {"result": str(e)}

def _parse_calendar_bound(value: str, default: datetime) -> datetime:
    """Parse an ISO date/datetime into an aware MSK datetime."""
    if not value:
        return default
    raw = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        dt = datetime.strptime(raw[:10], "%Y-%m-%d")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MSK_TZ)
    return dt.astimezone(MSK_TZ)


@app.post("/calendar_events_json")
def calendar_events_json(req: CalendarRangeRequest):
    """Structured, read-only calendar export used by the Google Calendar bridge.

    Returns occurrences expanded by EWS within the requested range, including
    ``uid`` and ``recurrence_id`` so callers can build an idempotent mapping.
    The legacy text endpoint ``/get_calendar`` is left untouched.
    """
    account = get_account()
    if not account:
        raise HTTPException(status_code=503, detail="Exchange connection failed")

    now_msk = datetime.now(MSK_TZ)
    start = _parse_calendar_bound(
        req.start, now_msk - timedelta(days=max(0, min(req.days_back, 365)))
    )
    end = _parse_calendar_bound(
        req.end, now_msk + timedelta(days=max(1, min(req.days_forward, 365)))
    )
    if end <= start:
        raise HTTPException(status_code=400, detail="end must be after start")
    if (end - start).days > 400:
        raise HTTPException(status_code=400, detail="range too large (max 400 days)")

    try:
        items = account.calendar.view(start=start, end=end)
        events = []
        for i in items:
            st = i.start.astimezone(MSK_TZ)
            en = i.end.astimezone(MSK_TZ)
            uid = getattr(i, "uid", None) or ""
            rid = getattr(i, "recurrence_id", None)
            if hasattr(rid, "isoformat"):
                rid_out = rid.isoformat()
            elif rid:
                rid_out = str(rid)
            else:
                rid_out = None
            sensitivity = str(getattr(i, "sensitivity", "") or "").lower()
            last_mod = getattr(i, "last_modified_time", None)
            events.append({
                "uid": uid,
                "recurrence_id": rid_out,
                "subject": i.subject or "",
                "start": st.isoformat(),
                "end": en.isoformat(),
                "location": i.location or "",
                "organizer": i.organizer.email_address if getattr(i, "organizer", None) else None,
                "is_all_day": bool(getattr(i, "is_all_day", False)),
                "is_cancelled": bool(getattr(i, "is_cancelled", False)),
                "is_private": "private" in sensitivity,
                "last_modified": last_mod.astimezone(MSK_TZ).isoformat() if last_mod else None,
            })
        events.sort(key=lambda e: e["start"])
        return {
            "status": "ok",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "count": len(events),
            "events": events,
        }
    except Exception as e:
        logger.exception("calendar_events_json failed")
        raise HTTPException(status_code=500, detail=f"calendar read failed: {e}")

@app.post("/search_calendar")
def search_calendar_events(req: SearchCalendarRequest):
    account = get_account()
    if not account:
        raise HTTPException(status_code=503, detail="Exchange connection failed")
    try:
        now = datetime.now(MSK_TZ)
        start_search = now - timedelta(days=7)
        end_search = now + timedelta(days=req.days)
        
        items = account.calendar.view(start=start_search, end=end_search)
        
        found = []
        q = req.query.lower()
        
        for i in items:
            subj = i.subject or ""
            loc = i.location or ""
            
            if q in subj.lower() or q in loc.lower():
                start_str = i.start.astimezone(MSK_TZ).strftime('%d.%m %H:%M')
                end_str = i.end.astimezone(MSK_TZ).strftime('%H:%M')
                found.append(f"📅 {start_str}-{end_str} | {subj} ({loc})")
        
        if not found:
            return {"result": "Nothing found."}
            
        return {"result": "\n".join(found)}
        
    except Exception as e: return {"result": f"Error: {str(e)}"}

@app.post("/sync_emails")
def sync_emails_quick(limit: int = 5):
    account = get_account()
    if not account:
        raise HTTPException(status_code=503, detail="Exchange connection failed")
    try:
        items = account.inbox.all().order_by('-datetime_received')[:limit]
        res = []
        for i in items:
            sender_name = i.sender.name if i.sender else '?'
            sender_email = i.sender.email_address if i.sender else '?'
            body = i.text_body[:200] if i.text_body else ''
            res.append(f"FROM: {sender_name} <{sender_email}>\nSUBJ: {i.subject}\n{body}\n---")
        return {"status": "success", "message": "\n".join(res)}
    except Exception as e: return {"status": "error", "message": str(e)}


# ===========================
# 📬 W2a: READ-ONLY MAIL API (metadata cache + structured endpoints)
# ===========================

# Limits — keep in one place so tests can assert against them.
W2A_DEFAULT_LIMIT = 50
W2A_MAX_LIMIT = 500
W2A_REFRESH_LIMIT = 100  # how many items to pull when refresh=True

# Папки, которые /list_messages и /search_messages умеют рефрешить из Exchange
# при refresh=True. Только реальные exchangelib-атрибуты аккаунта.
W2A_REFRESHABLE_FOLDERS = {
    "inbox": lambda acc: acc.inbox,
    "archive_inbox": lambda acc: _get_archive_inbox(acc),  # local helper, see below
}


def _normalize_folder_name(name: str) -> str:
    return (name or "").strip().lower()


def _resolve_exchange_folder(account, folder_name: str):
    """Resolve a folder name to an exchangelib Folder object.

    Returns None if unsupported. Never raises — caller falls back to cache.
    """
    if not account or not folder_name:
        return None
    fn = _normalize_folder_name(folder_name)
    mapper = W2A_REFRESHABLE_FOLDERS.get(fn)
    if mapper is not None:
        try:
            f = mapper(account)
            return f
        except Exception as e:
            logger.warning("w2a: folder '%s' resolve failed: %s", folder_name, e)
            return None
    return None


def _get_archive_inbox(account):
    """Returns archive inbox folder or None (matches behaviour of older endpoints)."""
    try:
        archive_root = account.archive_msg_folder_root
        for child in archive_root.children:
            if child.folder_class == 'IPF.Note' and child.name.lower() in ['inbox', 'входящие']:
                return child
        return account.archive_root
    except Exception as e:
        logger.warning("w2a: archive inbox lookup failed: %s", e)
        return None


def _refresh_cache_from_folder(account, folder_obj, limit: int = W2A_REFRESH_LIMIT) -> int:
    """Pull recent items from an Exchange folder and write metadata to storage.

    Never raises. Returns the number of records written.
    Strictly read-only — does not mark items read, does not move them.
    """
    storage = _open_exchange_storage()
    folder_name = getattr(folder_obj, "name", None) or "unknown"
    written = 0
    try:
        # Strict bound — even if caller asks for huge limit.
        limit = max(1, min(int(limit or W2A_REFRESH_LIMIT), W2A_MAX_LIMIT))
        qs = folder_obj.all().order_by('-datetime_received')[:limit]
        for item in qs:
            try:
                rec = message_record_from_exchange(item, folder_name)
                if rec.get("item_id") is None:
                    continue
                storage.upsert_message(rec)
                written += 1
            except Exception as e:
                # One bad item must not poison the whole batch.
                logger.info("w2a: skip item in %s: %s", folder_name, e)
    except Exception as e:
        logger.warning("w2a: refresh failed for %s: %s", folder_name, e)
    return written


def _unavailable_account_reason() -> str:
    """Return a structured 503-reason string for the read endpoints."""
    if not is_vpn_active():
        return "vpn_down"
    return "exchange_unreachable"


@app.post("/list_messages")
def list_messages_endpoint(req: ListMessagesRequest):
    """List cached message metadata, optionally refreshed from Exchange.

    Strictly READ-ONLY. Never sends, replies to, or modifies messages.
    Honours the VPN guard: returns 503 if VPN is down (no Exchange auth).
    """
    account = get_account()
    if not account:
        raise HTTPException(
            status_code=503,
            detail={
                "status": "error",
                "reason": _unavailable_account_reason(),
                "message": "Exchange account unavailable; check vpn0 / EWS auth",
            },
        )

    # Bound the inputs — SQLite pagination, never trust the request.
    try:
        limit = max(1, min(int(req.limit or W2A_DEFAULT_LIMIT), W2A_MAX_LIMIT))
        offset = max(0, int(req.offset or 0))
    except Exception:
        raise HTTPException(status_code=400, detail={"status": "error", "reason": "bad_pagination"})

    folder_norm = _normalize_folder_name(req.folder) if req.folder else None

    storage = _open_exchange_storage()
    refreshed = 0
    if req.refresh:
        target = _resolve_exchange_folder(account, req.folder) if req.folder else None
        if target is not None:
            refreshed = _refresh_cache_from_folder(account, target, limit=W2A_REFRESH_LIMIT)
        # If folder unknown / refreshable — we still serve from cache. Do NOT
        # raise — clients can fall back to /search.

    messages = storage.list_messages(
        folder=folder_norm,
        limit=limit,
        offset=offset,
        received_after=req.received_after,
        received_before=req.received_before,
    )
    total = storage.count(
        folder=folder_norm,
        received_after=req.received_after,
        received_before=req.received_before,
    )
    return {
        "status": "success",
        "folder": folder_norm,
        "limit": limit,
        "offset": offset,
        "total": total,
        "refreshed": refreshed,
        "messages": messages,
    }


@app.post("/search_messages")
def search_messages_endpoint(req: SearchMessagesRequest):
    """Full-text-ish search (subject/sender/snippet LIKE) over cached metadata.

    Strictly READ-ONLY. Same VPN guard as /list_messages.
    """
    account = get_account()
    if not account:
        raise HTTPException(
            status_code=503,
            detail={
                "status": "error",
                "reason": _unavailable_account_reason(),
                "message": "Exchange account unavailable; check vpn0 / EWS auth",
            },
        )

    q = (req.query or "").strip()
    if not q:
        raise HTTPException(
            status_code=400,
            detail={"status": "error", "reason": "empty_query"},
        )

    try:
        limit = max(1, min(int(req.limit or W2A_DEFAULT_LIMIT), W2A_MAX_LIMIT))
        offset = max(0, int(req.offset or 0))
    except Exception:
        raise HTTPException(status_code=400, detail={"status": "error", "reason": "bad_pagination"})

    folder_norm = _normalize_folder_name(req.folder) if req.folder else None

    storage = _open_exchange_storage()
    messages = storage.search_messages(
        query=q,
        folder=folder_norm,
        limit=limit,
        offset=offset,
        received_after=req.received_after,
        received_before=req.received_before,
    )
    return {
        "status": "success",
        "query": q,
        "folder": folder_norm,
        "limit": limit,
        "offset": offset,
        "count": len(messages),
        "messages": messages,
    }


@app.post("/get_message")
def get_message_endpoint(req: GetMessageRequest):
    """Fetch one cached message by ref or raw item_id.

    Strictly READ-ONLY. Returns 404 if not found, 503 if VPN/Exchange down.
    """
    account = get_account()
    if not account:
        raise HTTPException(
            status_code=503,
            detail={
                "status": "error",
                "reason": _unavailable_account_reason(),
                "message": "Exchange account unavailable; check vpn0 / EWS auth",
            },
        )

    if not req.message_ref and not req.item_id:
        raise HTTPException(
            status_code=400,
            detail={"status": "error", "reason": "no_identifier"},
        )

    storage = _open_exchange_storage()
    try:
        msg = storage.get_message(message_ref=req.message_ref, item_id=req.item_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail={"status": "error", "reason": str(e)})

    if msg is None:
        raise HTTPException(
            status_code=404,
            detail={"status": "not_found", "message_ref": req.message_ref, "item_id": req.item_id},
        )
    return {"status": "success", "message": msg}


# ===========================
# ✉️ W2b: DESTRUCTIVE MAIL API (send / reply via service layer)
# ===========================
#
# Both endpoints honour the VPN guard: 503 if get_account() is None. They
# never construct EWS objects unless account is present. All operations
# write an audit row (success OR error) into storage.audit_log.

@app.post("/send_message")
def send_message_endpoint(req: SendMessageRequest):
    """Send a new email via the W2b service layer.

    Request:
        {"to": [...], "cc": [...], "bcc": [...], "subject": "...", "body": "..."}

    Responses:
        200  {"status": "success", "operation": "send", "audit_id": N, ...}
        400  {"detail": {"status": "error", "reason": "missing_recipient"}}
        503  VPN / account unavailable
        500  EWS send failure (after audit row written)
    """
    account = get_account()
    if not account:
        raise HTTPException(
            status_code=503,
            detail={
                "status": "error",
                "reason": _unavailable_account_reason(),
                "message": "Exchange account unavailable; check vpn0 / EWS auth",
            },
        )
    storage = _open_exchange_storage()
    try:
        result = _send_message_service(
            account,
            storage,
            to=req.to,
            cc=req.cc,
            bcc=req.bcc,
            subject=req.subject,
            body=req.body,
        )
        return result
    except MailSendError as e:
        raise HTTPException(
            status_code=500,
            detail={"status": "error", "operation": "send", "message": str(e)},
        )
    except Exception as e:
        # service-layer guards (missing recipient etc.) raise MailSendError;
        # anything else is unexpected — surface as 500 with audit already
        # written by the service.
        logger.exception("send_message failed")
        raise HTTPException(
            status_code=500,
            detail={"status": "error", "operation": "send", "message": str(e)},
        )


@app.post("/reply_message")
def reply_message_endpoint(req: ReplyMessageRequest):
    """Reply to an existing email via the W2b service layer.

    Request:
        {"message_ref": "...", "item_id": "optional", "body": "...",
         "subject": "optional"}

    Resolution order:
        1. message_ref → resolved through storage cache → raw item_id
        2. item_id     → used as-is (legacy fallback)
        3. inbox lookup → archive lookup → 404 if neither finds the item

    Responses:
        200  {"status": "success", "operation": "reply", "message_ref": ...,
              "subject": "...", "audit_id": N}
        400  {"detail": {"status": "error", "reason": "missing_identifier"}}
        404  {"detail": "..."}              target not in cache or Exchange
        503  VPN / account unavailable
        500  EWS reply failure (after audit row written)
    """
    if not req.message_ref and not req.item_id:
        raise HTTPException(
            status_code=400,
            detail={"status": "error", "reason": "missing_identifier"},
        )

    account = get_account()
    if not account:
        raise HTTPException(
            status_code=503,
            detail={
                "status": "error",
                "reason": _unavailable_account_reason(),
                "message": "Exchange account unavailable; check vpn0 / EWS auth",
            },
        )
    storage = _open_exchange_storage()
    try:
        result = _reply_message_service(
            account,
            storage,
            message_ref=req.message_ref,
            item_id=req.item_id,
            body=req.body,
            subject=req.subject,
            inbox_lookup=_default_reply_inbox_lookup,
            archive_lookup=_default_reply_archive_lookup,
        )
        return result
    except MailTargetNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except MailSendError as e:
        raise HTTPException(
            status_code=500,
            detail={"status": "error", "operation": "reply", "message": str(e)},
        )
    except Exception as e:
        logger.exception("reply_message failed")
        raise HTTPException(
            status_code=500,
            detail={"status": "error", "operation": "reply", "message": str(e)},
        )


def _default_reply_inbox_lookup(account, item_id: str):
    """Default inbox lookup for /reply_message — same shape as the legacy
    route: try inbox.get(id=...) and swallow transport errors so the
    archive fallback can run."""
    try:
        return account.inbox.get(id=item_id)
    except Exception as e:
        logger.info("reply inbox lookup failed: %s", e)
        return None


def _default_reply_archive_lookup(account, item_id: str):
    """Default archive inbox lookup for /reply_message."""
    try:
        target_folder = _get_archive_inbox(account)
        if target_folder:
            return target_folder.get(id=item_id)
    except Exception as e:
        logger.info("reply archive lookup failed: %s", e)
    return None


# ===========================
# 📎 W2c: ATTACHMENT READ/DOWNLOAD API
# ===========================
#
# Both endpoints honour the VPN guard: 503 if get_account() is None.
# They never touch the real Exchange unless the account is present.
# No archive extraction happens here — that stays in /download_dataset.

@app.post("/list_attachments")
def list_attachments_endpoint(req: ListAttachmentsRequest):
    """List attachments for a message identified by message_ref or item_id.

    Strictly READ-ONLY. Honours the VPN guard: 503 if VPN/account down.
    Returns 400 if neither identifier is supplied. Returns 404 if the
    target message is not in cache / Exchange folders.

    Request:
        {"message_ref": "...", "item_id": "optional"}

    Response (200):
        {"status": "success", "operation": "list_attachments",
         "message_ref": "...", "item_id_hash": "...",
         "count": N,
         "attachments": [
             {"attachment_ref": "...", "filename": "...",
              "size": N, "content_type": "..."}, ...
         ]}
    """
    if not req.message_ref and not req.item_id:
        raise HTTPException(
            status_code=400,
            detail={"status": "error", "reason": "missing_identifier"},
        )

    account = get_account()
    if not account:
        raise HTTPException(
            status_code=503,
            detail={
                "status": "error",
                "reason": _unavailable_account_reason(),
                "message": "Exchange account unavailable; check vpn0 / EWS auth",
            },
        )

    storage = _open_exchange_storage()
    try:
        result = _list_attachments_service(
            account,
            storage,
            message_ref=req.message_ref,
            item_id=req.item_id,
        )
        return result
    except AttachmentNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("list_attachments failed")
        raise HTTPException(
            status_code=500,
            detail={"status": "error", "operation": "list_attachments",
                    "message": str(e)},
        )


@app.post("/download_attachment")
def download_attachment_endpoint(req: DownloadAttachmentRequest):
    """Download a single attachment to disk and record its metadata row.

    Honour the VPN guard: 503 if VPN/account down. Never touches
    production rag_data/ — caller may pass any target_dir (default
    ``rag_data/attachments``).

    Request:
        {"message_ref": "...", "item_id": "optional",
         "attachment_ref": "optional",
         "attachment_id": "optional",
         "filename":     "optional",
         "target_dir":   "optional, default rag_data/attachments"}

    Exactly one of attachment_ref / attachment_id / filename must be
    supplied. The filename is sanitised — path traversal sequences are
    stripped before any bytes are written.

    Responses:
        200  {"status": "success", "operation": "download_attachment",
              "message_ref": "...", "attachment_ref": "...",
              "filename": "...", "size": N, "content_type": "...",
              "local_path": "..."}
        400  missing message identifier OR missing attachment selector
        404  target message / attachment not found
        503  VPN / account unavailable (no folder / attachment is touched)
        500  EWS attachment fetch failure
    """
    if not req.message_ref and not req.item_id:
        raise HTTPException(
            status_code=400,
            detail={"status": "error", "reason": "missing_identifier"},
        )
    if not req.attachment_ref and not req.attachment_id and not req.filename:
        raise HTTPException(
            status_code=400,
            detail={"status": "error", "reason": "no_attachment_selector"},
        )

    account = get_account()
    if not account:
        raise HTTPException(
            status_code=503,
            detail={
                "status": "error",
                "reason": _unavailable_account_reason(),
                "message": "Exchange account unavailable; check vpn0 / EWS auth",
            },
        )

    storage = _open_exchange_storage()
    target_dir = (req.target_dir or "rag_data/attachments").strip() or "rag_data/attachments"
    try:
        result = _download_attachment_service(
            account,
            storage,
            message_ref=req.message_ref,
            item_id=req.item_id,
            attachment_ref=req.attachment_ref,
            attachment_id=req.attachment_id,
            filename=req.filename,
            target_dir=target_dir,
        )
        return result
    except AttachmentNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except AttachmentSourceError as e:
        raise HTTPException(
            status_code=500,
            detail={"status": "error", "operation": "download_attachment",
                    "message": str(e)},
        )
    except Exception as e:
        logger.exception("download_attachment failed")
        raise HTTPException(
            status_code=500,
            detail={"status": "error", "operation": "download_attachment",
                    "message": str(e)},
        )


# ===========================
# 🗂️ W2d: LOCAL-FILE RAG INDEXING (no Exchange / no VPN)
# ===========================
#
# These endpoints operate on files already on disk under a configurable
# ``root_dir`` (default ``rag_data/``). They never call
# get_account() / EWS — safe to call from agents / MCP tooling without
# VPN. Path-safety is enforced inside exchange_rag_service.

@app.post("/index_rag_documents")
def index_rag_documents_endpoint(req: IndexRagDocumentsRequest):
    """Walk a local root and upsert text-like files into the SQLite RAG index.

    Request:
        {
          "root_dir":       optional, default "rag_data",
          "limit":          optional, default 1000,
          "max_file_bytes": optional, default 2_000_000,
          "extensions":     optional list, e.g. [".txt", ".md"];
                             empty/missing → service default text set,
          "force":          optional bool, default false
        }

    Responses:
        200  {"status": "success", "root_dir": "...", "indexed": N,
              "skipped": N, "errors": N, "errors_detail": [...],
              "limit": N, "max_file_bytes": N, "extensions": [...],
              "force": bool}
        404  root_dir missing/invalid (cannot resolve / not a directory /
              unsafe path)
        500  unexpected exception (logged; never reaches Exchange)
    """
    storage = _open_exchange_storage()
    try:
        result = _index_rag_documents_service(
            storage,
            root_dir=req.root_dir,
            limit=req.limit,
            max_file_bytes=req.max_file_bytes,
            extensions=list(req.extensions) if req.extensions else None,
            force=bool(req.force),
        )
    except RagRootUnsafe as e:
        raise HTTPException(
            status_code=404,
            detail={"status": "error", "reason": "unsafe_root_dir",
                    "root_dir": req.root_dir, "message": str(e)},
        )
    except Exception as e:
        logger.exception("index_rag_documents failed")
        raise HTTPException(
            status_code=500,
            detail={"status": "error", "operation": "index_rag_documents",
                    "message": str(e)},
        )
    # Service returns status: "error" for missing dirs; expose as 404.
    if result.get("status") == "error":
        raise HTTPException(
            status_code=404,
            detail={
                "status": "error",
                "reason": "root_dir_unavailable",
                "root_dir": result.get("root_dir") or req.root_dir,
                "errors_detail": result.get("errors_detail") or [],
            },
        )
    return result


@app.post("/search_rag_documents")
def search_rag_documents_endpoint(req: SearchRagDocumentsRequest):
    """Substring search over the local RAG index.

    Request:
        {"query": "...", "limit": 20 (optional), "root_dir": "..." (optional)}

    Responses:
        200  {"status": "success", "query": "...", "root_dir": "...",
              "limit": N, "count": N, "documents": [<row>, ...]}
        400  empty_query
        404  bad_root_dir (when caller supplied one and it is unsafe)
    """
    q = (req.query or "").strip()
    if not q:
        raise HTTPException(
            status_code=400,
            detail={"status": "error", "reason": "empty_query"},
        )
    storage = _open_exchange_storage()
    try:
        result = _search_rag_documents_service(
            storage,
            q,
            limit=req.limit,
            root_dir=req.root_dir,
        )
    except Exception as e:
        logger.exception("search_rag_documents failed")
        raise HTTPException(
            status_code=500,
            detail={"status": "error", "operation": "search_rag_documents",
                    "message": str(e)},
        )
    if result.get("status") == "error" and result.get("reason", "").startswith("bad_root_dir"):
        raise HTTPException(
            status_code=404,
            detail={
                "status": "error",
                "reason": "unsafe_root_dir",
                "root_dir": req.root_dir,
                "message": result.get("reason"),
            },
        )
    return result


@app.post("/get_rag_document")
def get_rag_document_endpoint(req: GetRagDocumentRequest):
    """Fetch one RAG document by opaque doc_ref, optionally with text body.

    Request:
        {"doc_ref": "...", "include_text": false, "max_chars": 20000}

    Responses:
        200  {"status": "success", "doc_ref": "...",
              "document": {<row>...},
              "text": "..." (only when include_text=true),
              "text_chars": N,
              "text_truncated": bool}
        400  missing doc_ref
        404  unknown doc_ref OR file resolved outside its root
        500  unexpected exception
    """
    if not (req.doc_ref or "").strip():
        raise HTTPException(
            status_code=400,
            detail={"status": "error", "reason": "missing_doc_ref"},
        )
    storage = _open_exchange_storage()
    try:
        result = _get_rag_document_service(
            storage,
            req.doc_ref.strip(),
            include_text=bool(req.include_text),
            max_chars=req.max_chars,
        )
    except RagDocNotFound:
        raise HTTPException(
            status_code=404,
            detail={"status": "not_found", "doc_ref": req.doc_ref},
        )
    except RagRootUnsafe as e:
        raise HTTPException(
            status_code=404,
            detail={"status": "error", "reason": "unsafe_doc_path",
                    "doc_ref": req.doc_ref, "message": str(e)},
        )
    except Exception as e:
        logger.exception("get_rag_document failed")
        raise HTTPException(
            status_code=500,
            detail={"status": "error", "operation": "get_rag_document",
                    "message": str(e)},
        )
    return result


if __name__ == "__main__":
    import uvicorn
    print("Exchange V14 (Fixed ZoneInfo) started!")
    # Слушаем все интерфейсы, чтобы Docker/LAN видели сервер
    uvicorn.run(app, host="0.0.0.0", port=5015)
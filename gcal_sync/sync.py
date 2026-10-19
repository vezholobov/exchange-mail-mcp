#!/usr/bin/env python3
"""One-way Exchange -> Google Calendar sync.

Source: exchange-mail-mcp REST endpoint /calendar_events_json
Target: a Google Calendar (normally the account's primary calendar), shared
        with a service account that has "Make changes to events" permission.

The sync is idempotent: every created event carries an invisible
extendedProperties marker (sync_source=exchange, sync_key=...). Only marked
events are ever updated or deleted, so manually created entries stay intact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

MSK = ZoneInfo("Europe/Moscow")
SCOPES = ["https://www.googleapis.com/auth/calendar"]
MARKER = "exchange"
DEFAULT_CONFIG = os.path.expanduser("~/.config/exchange-gcal-sync/config.json")

log = logging.getLogger("gcal-sync")


def load_config(path: str) -> dict:
    cfg = {
        "exchange_url": "http://172.17.0.1:5015",
        "calendar_id": "primary",
        "service_account_key": "",
        "days_back": 7,
        "days_forward": 60,
        "state_db": os.path.expanduser("~/.local/state/exchange-gcal-sync/state.sqlite3"),
        "max_deletes": 50,
        "dry_run": False,
    }
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    for key in list(cfg):
        env = "GCAL_SYNC_" + key.upper()
        if env in os.environ:
            cfg[key] = os.environ[env]
    for key in ("days_back", "days_forward", "max_deletes"):
        cfg[key] = int(cfg[key])
    for key in ("dry_run",):
        if isinstance(cfg[key], str):
            cfg[key] = cfg[key].lower() in ("1", "true", "yes", "on")
    return cfg


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(MSK)


def event_key(ev: dict) -> str:
    rid = ev.get("recurrence_id") or ""
    ident = rid or ev["start"]
    return hashlib.sha1(f"{ev.get('uid','')}|{ident}".encode()).hexdigest()[:32]


def event_hash(ev: dict) -> str:
    parts = [
        ev.get("subject", ""),
        ev.get("start", ""),
        ev.get("end", ""),
        ev.get("location", ""),
        str(bool(ev.get("is_all_day"))),
        str(bool(ev.get("is_cancelled"))),
    ]
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def fetch_exchange(cfg: dict) -> list[dict]:
    body = json.dumps(
        {"days_back": cfg["days_back"], "days_forward": cfg["days_forward"]}
    ).encode()
    req = urllib.request.Request(
        cfg["exchange_url"].rstrip("/") + "/calendar_events_json",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.load(resp)
    if data.get("status") != "ok":
        raise RuntimeError(f"exchange returned {data.get('status')}: {data}")
    return data.get("events", [])


def google_event_body(ev: dict, key: str) -> dict:
    body: dict = {
        "summary": ev.get("subject") or "(без темы)",
        "location": ev.get("location") or "",
        "extendedProperties": {
            "private": {
                "sync_source": MARKER,
                "sync_key": key,
                "sync_uid": ev.get("uid", ""),
            }
        },
        "transparency": "opaque",
    }
    if not ev.get("is_private") and ev.get("organizer"):
        body["description"] = f"Организатор: {ev['organizer']}"
    if ev.get("is_all_day"):
        start_date = parse_dt(ev["start"]).date()
        end_date = parse_dt(ev["end"]).date()
        if end_date <= start_date:
            end_date = start_date + timedelta(days=1)
        body["start"] = {"date": start_date.isoformat()}
        body["end"] = {"date": end_date.isoformat()}
    else:
        body["start"] = {"dateTime": ev["start"], "timeZone": "Europe/Moscow"}
        body["end"] = {"dateTime": ev["end"], "timeZone": "Europe/Moscow"}
    return body


class State:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS synced (
                sync_key TEXT PRIMARY KEY,
                google_id TEXT,
                hash TEXT,
                subject TEXT,
                start TEXT,
                updated_at TEXT
            )"""
        )
        self.db.commit()

    def remember(self, key: str, gid: str, h: str, ev: dict) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO synced VALUES (?,?,?,?,?,?)",
            (key, gid, h, ev.get("subject", ""), ev.get("start", ""), datetime.now(MSK).isoformat()),
        )
        self.db.commit()

    def forget(self, key: str) -> None:
        self.db.execute("DELETE FROM synced WHERE sync_key=?", (key,))
        self.db.commit()

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM synced").fetchone()[0]


def calendar_client(cfg: dict):
    key_file = os.path.expanduser(cfg["service_account_key"])
    if not key_file or not os.path.exists(key_file):
        raise SystemExit(f"service account key not found: {key_file}")
    creds = service_account.Credentials.from_service_account_file(key_file, scopes=SCOPES)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def list_marked(service, calendar_id: str, time_min: str, time_max: str) -> dict[str, dict]:
    found: dict[str, dict] = {}
    page = None
    while True:
        resp = service.events().list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            showDeleted=False,
            maxResults=2500,
            privateExtendedProperty=f"sync_source={MARKER}",
            pageToken=page,
        ).execute()
        for item in resp.get("items", []):
            key = (item.get("extendedProperties", {}).get("private", {}) or {}).get("sync_key")
            if key:
                found[key] = item
        page = resp.get("nextPageToken")
        if not page:
            break
    return found


def sync(cfg: dict) -> int:
    events = [e for e in fetch_exchange(cfg) if not e.get("is_cancelled")]
    log.info("exchange returned %d active events", len(events))

    service = calendar_client(cfg)
    now = datetime.now(MSK)
    time_min = (now - timedelta(days=cfg["days_back"])).isoformat()
    time_max = (now + timedelta(days=cfg["days_forward"])).isoformat()

    existing = list_marked(service, cfg["calendar_id"], time_min, time_max)
    log.info("google has %d marked events in window", len(existing))

    state = State(os.path.expanduser(cfg["state_db"]))
    desired = {}
    for ev in events:
        key = event_key(ev)
        desired[key] = ev

    creates = updates = 0
    for key, ev in desired.items():
        body = google_event_body(ev, key)
        h = event_hash(ev)
        current = existing.get(key)
        if current:
            if (current.get("extendedProperties", {}).get("private", {}) or {}).get("sync_hash") == h:
                continue
            updates += 1
            body["extendedProperties"]["private"]["sync_hash"] = h
            if cfg["dry_run"]:
                log.info("[dry-run] update %s %s", ev["start"], ev.get("subject"))
                continue
            try:
                service.events().patch(
                    calendarId=cfg["calendar_id"], eventId=current["id"], body=body
                ).execute()
                state.remember(key, current["id"], h, ev)
            except HttpError as exc:
                log.error("update failed for %s: %s", key, exc)
        else:
            creates += 1
            body["extendedProperties"]["private"]["sync_hash"] = h
            if cfg["dry_run"]:
                log.info("[dry-run] create %s %s", ev["start"], ev.get("subject"))
                continue
            try:
                created = service.events().insert(
                    calendarId=cfg["calendar_id"], body=body
                ).execute()
                state.remember(key, created["id"], h, ev)
            except HttpError as exc:
                log.error("insert failed for %s: %s", key, exc)

    orphans = [k for k in existing if k not in desired]
    deletes = 0
    if orphans and not events:
        log.warning("skipping %d deletions: exchange returned no events", len(orphans))
        orphans = []
    if len(orphans) > cfg["max_deletes"]:
        log.error(
            "refusing to delete %d events (max_deletes=%d); inspect manually",
            len(orphans),
            cfg["max_deletes"],
        )
        orphans = orphans[: cfg["max_deletes"]]
    for key in orphans:
        deletes += 1
        if cfg["dry_run"]:
            log.info("[dry-run] delete %s", key)
            continue
        try:
            service.events().delete(
                calendarId=cfg["calendar_id"], eventId=existing[key]["id"]
            ).execute()
            state.forget(key)
        except HttpError as exc:
            log.error("delete failed for %s: %s", key, exc)

    log.info(
        "done: %d create, %d update, %d delete, %d desired, %d in google",
        creates,
        updates,
        deletes,
        len(desired),
        len(existing),
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    cfg = load_config(args.config)
    if args.dry_run:
        cfg["dry_run"] = True
    try:
        return sync(cfg)
    except urllib.error.URLError as exc:
        log.error("exchange unreachable: %s", exc)
        return 2
    except Exception as exc:  # noqa: BLE001
        log.exception("sync failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

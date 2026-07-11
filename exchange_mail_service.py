"""
exchange_mail_service.py — destructive mail operations (send / reply) as a
testable service layer for the Exchange connector (W2b).

This module wraps the legacy `Message(...).send()` / `Item.reply(...)` paths
that used to live directly inside main.py route handlers. It exists so:

  * The send/reply logic can be exercised from unit tests without booting
    FastAPI or touching the network.
  * Audit logging is centralised — every successful send/reply AND every
    failure produces exactly one row in `audit_log`.
  * The VPN guard upstream in main.py is preserved: this module never
    constructs a `Message` or calls `.send()`/`.reply()` unless the caller
    has already validated an active account.

Public API:
    send_message(account, storage, *, to, subject, body,
                 cc=None, bcc=None,
                 message_factory=None) -> dict
        -> Returns {"status": "success"|"error", "operation": "send",
                    "message_ref": None, "to_recipients": [...], ...}
        -> On success: writes an audit row with status=success.
        -> On error:   writes an audit row with status=error and re-raises.

    reply_message(account, storage, *,
                  message_ref=None, item_id=None,
                  body="", subject=None,
                  inbox_lookup=None, archive_lookup=None) -> dict
        -> Returns {"status": "success"|"error", "operation": "reply",
                    "message_ref": ..., "subject": ..., ...}
        -> On success: writes an audit row with status=success.
        -> On target-not-found: writes an audit row with status=error,
            raises MailTargetNotFound.

    MailTargetNotFound — typed exception for "reply target not in cache or
        Exchange folders" (404 in main.py).
    MailSendError      — typed exception for transport-level failures
        (500 in main.py).

Both helpers write audit rows via `storage.log_audit(...)`. Raw item ids
are NEVER stored — only `hash_item_id(...)` is persisted.

Testability hooks:
    * `send_message` accepts `message_factory` so tests can swap in a fake
      `Message` constructor that does not touch exchangelib.
    * `reply_message` accepts `inbox_lookup` and `archive_lookup` callable
      overrides. Default behaviour tries `account.inbox.get(id=item_id)`
      then a fallback helper. If both fail the caller learns about it via
      the typed MailTargetNotFound exception.

This module deliberately does NOT import FastAPI. The HTTP layer lives in
main.py and converts typed errors / dict returns into JSON.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

logger = logging.getLogger("exchange_mail_service")

# Re-export the storage hash helper for symmetry with main.py imports.
from exchange_storage import hash_item_id  # noqa: E402


# -----------------------------------------------------------------------------
# Typed errors — distinct from arbitrary RuntimeError so main.py can map them
# to the right HTTP status code without inspecting strings.
# -----------------------------------------------------------------------------
class MailTargetNotFound(Exception):
    """Reply target message not in inbox, archive, or storage cache."""

    def __init__(self, identifier: str, reason: str = "not_found"):
        super().__init__(f"reply target not found: {identifier} ({reason})")
        self.identifier = identifier
        self.reason = reason


class MailSendError(Exception):
    """EWS send/reply failed at transport level."""

    def __init__(self, operation: str, original: Optional[BaseException] = None):
        msg = f"{operation} failed: {type(original).__name__ if original else 'unknown'}"
        if original is not None:
            msg = f"{msg}: {original}"
        super().__init__(msg)
        self.operation = operation
        self.original = original


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _normalise_recipients(value: Union[None, str, Sequence[str]]) -> List[str]:
    """Accept either a string, a list of strings, or None; return list[str].

    Each entry is stripped of whitespace; empty strings are dropped. We never
    raise on bad input — callers may pass arbitrary pydantic-driven shapes.
    """
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    else:
        items = list(value)
    return [str(x).strip() for x in items if x is not None and str(x).strip()]


def _default_archive_inbox_lookup(account, item_id: str):
    """Walk account.archive_msg_folder_root and look for a child with
    matching name; return None if not found / archive unavailable.

    Kept as a free function so tests can inject a replacement.
    """
    try:
        archive_root = account.archive_msg_folder_root
    except Exception as e:
        logger.info("archive inbox lookup failed: %s", e)
        return None
    try:
        for child in archive_root.children:
            if (
                getattr(child, "folder_class", None) == "IPF.Note"
                and (child.name or "").lower() in ("inbox", "входящие")
            ):
                try:
                    return child.get(id=item_id)
                except Exception as e:
                    logger.info("archive inbox get(%s) failed: %s", item_id, e)
                    return None
    except Exception as e:
        logger.info("archive inbox children iteration failed: %s", e)
    # Fallback — archive_root is a *folder*; resolve to an item via .get(id=...).
    # Returning the folder itself (legacy behaviour) breaks reply_message,
    # which calls .reply() on the returned object. Resolve and return None if
    # the item is absent.
    try:
        return account.archive_root.get(id=item_id)
    except Exception as e:
        logger.info("archive_root.get(%s) failed: %s", item_id, e)
        return None


def _default_inbox_lookup(account, item_id: str):
    """Default inbox lookup — try account.inbox.get(id=...) and swallow
    everything except "found a real item" so we don't double-report errors.
    """
    try:
        return account.inbox.get(id=item_id)
    except Exception as e:
        logger.info("inbox get(%s) failed: %s", item_id, e)
        return None


# -----------------------------------------------------------------------------
# send_message — outbound new mail
# -----------------------------------------------------------------------------
def send_message(
    account: Any,
    storage: Any,
    *,
    to: Union[str, Sequence[str]],
    subject: str,
    body: str,
    cc: Optional[Union[str, Sequence[str]]] = None,
    bcc: Optional[Union[str, Sequence[str]]] = None,
    message_factory: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """Send a new message via EWS.

    Required:
        account   — active exchangelib Account (caller has validated VPN).
        storage   — ExchangeStorage instance for audit logging.
        to        — list of emails or single email string.
        subject   — message subject.
        body      — message body text.

    Optional:
        cc, bcc   — same shape as `to`.
        message_factory — overrides Message construction. Tests pass a fake.
            Signature: message_factory(account, **kwargs) -> object with .send().

    Returns a structured result dict. On failure, writes an audit row and
    re-raises MailSendError.
    """
    to_list = _normalise_recipients(to)
    cc_list = _normalise_recipients(cc)
    bcc_list = _normalise_recipients(bcc)

    if not to_list:
        # No recipient is a programmer error in normal flow; audit + raise.
        storage.log_audit(
            operation="send_error",
            status="error",
            to_recipients=to_list,
            cc_recipients=cc_list,
            bcc_recipients=bcc_list,
            subject=subject,
            error="missing_recipient",
        )
        raise MailSendError("send")

    # Late import keeps this module exchangelib-importable only when actually
    # running against a real Account (tests inject message_factory).
    if message_factory is None:
        from exchangelib import Message, Body, Mailbox  # type: ignore

        def message_factory(  # noqa: F841 - bound to local namespace
            account, *, subject, body, to_recipients, cc_recipients, bcc_recipients
        ):
            kwargs = dict(
                account=account,
                subject=subject,
                body=Body(body),
                to_recipients=[Mailbox(email_address=x) for x in to_recipients],
            )
            # cc / bcc supported by recent exchangelib Message constructors.
            if cc_recipients:
                kwargs["cc_recipients"] = [Mailbox(email_address=x) for x in cc_recipients]
            if bcc_recipients:
                kwargs["bcc_recipients"] = [Mailbox(email_address=x) for x in bcc_recipients]
            return Message(**kwargs)

    msg = message_factory(
        account,
        subject=subject,
        body=body,
        to_recipients=to_list,
        cc_recipients=cc_list,
        bcc_recipients=bcc_list,
    )

    try:
        msg.send()
    except Exception as e:
        storage.log_audit(
            operation="send_error",
            status="error",
            to_recipients=to_list,
            cc_recipients=cc_list,
            bcc_recipients=bcc_list,
            subject=subject,
            error=f"{type(e).__name__}: {e}",
        )
        logger.warning("send_message failed: %s", e)
        raise MailSendError("send", e)

    audit_id = storage.log_audit(
        operation="send",
        status="success",
        to_recipients=to_list,
        cc_recipients=cc_list,
        bcc_recipients=bcc_list,
        subject=subject,
    )
    logger.info("send_message ok: to=%d cc=%d bcc=%d", len(to_list), len(cc_list), len(bcc_list))
    return {
        "status": "success",
        "operation": "send",
        "audit_id": audit_id,
        "to_recipients": to_list,
        "cc_recipients": cc_list,
        "bcc_recipients": bcc_list,
        "subject": subject,
    }


# -----------------------------------------------------------------------------
# reply_message — reply to existing message
# -----------------------------------------------------------------------------
def reply_message(
    account: Any,
    storage: Any,
    *,
    message_ref: Optional[str] = None,
    item_id: Optional[str] = None,
    body: str = "",
    subject: Optional[str] = None,
    inbox_lookup: Optional[Callable[[Any, str], Any]] = None,
    archive_lookup: Optional[Callable[[Any, str], Any]] = None,
) -> Dict[str, Any]:
    """Reply to an existing message identified by `message_ref` (preferred)
    or raw `item_id` (legacy fallback).

    `message_ref` is resolved via `storage.get_message(message_ref=...)` which
    yields the raw `item_id` from the cache. `item_id` is used as-is when
    `message_ref` is not provided.

    Inbox lookup strategy:
        1. inbox_lookup(account, item_id)  — default: account.inbox.get(id=...)
        2. archive_lookup(account, item_id) — default: walk archive_msg_folder_root
        If both return None, raise MailTargetNotFound → main.py returns 404.

    Subject auto-generation: if `subject` is None and we found the original,
    we prefix "RE: " only if the original subject does not already start
    with "re:" (case-insensitive). Otherwise we keep the original subject.
    Matches the legacy /reply_work_email behaviour.
    """
    raw_item_id: Optional[str] = None
    resolved_message_ref: Optional[str] = message_ref

    if message_ref:
        record = storage.get_message(message_ref=message_ref)
        if record is None:
            storage.log_audit(
                operation="reply_error",
                status="error",
                message_ref=message_ref,
                error="message_ref_not_in_cache",
            )
            raise MailTargetNotFound(message_ref, reason="message_ref_not_in_cache")
        raw_item_id = record.get("item_id")
        if not raw_item_id:
            storage.log_audit(
                operation="reply_error",
                status="error",
                message_ref=message_ref,
                error="cache_row_missing_item_id",
            )
            raise MailTargetNotFound(message_ref, reason="cache_row_missing_item_id")
    elif item_id is not None:
        raw_item_id = item_id
    else:
        # No identifier — caller should have caught this, but be defensive.
        storage.log_audit(
            operation="reply_error",
            status="error",
            error="missing_identifier",
        )
        raise MailTargetNotFound("<none>", reason="missing_identifier")

    inbox_fn = inbox_lookup or _default_inbox_lookup
    archive_fn = archive_lookup or _default_archive_inbox_lookup

    item = inbox_fn(account, raw_item_id)
    if item is None:
        item = archive_fn(account, raw_item_id)

    if item is None:
        storage.log_audit(
            operation="reply_error",
            status="error",
            message_ref=resolved_message_ref,
            item_id=raw_item_id,
            error="target_not_found",
        )
        raise MailTargetNotFound(raw_item_id, reason="target_not_found")

    # Auto-generate subject if missing.
    reply_subject = subject
    if not reply_subject:
        orig = getattr(item, "subject", None) or ""
        if not orig.lower().startswith("re:"):
            reply_subject = f"RE: {orig}"
        else:
            reply_subject = orig

    try:
        item.reply(subject=reply_subject, body=body)
    except Exception as e:
        storage.log_audit(
            operation="reply_error",
            status="error",
            message_ref=resolved_message_ref,
            item_id=raw_item_id,
            subject=reply_subject,
            error=f"{type(e).__name__}: {e}",
        )
        logger.warning("reply_message failed: %s", e)
        raise MailSendError("reply", e)

    audit_id = storage.log_audit(
        operation="reply",
        status="success",
        message_ref=resolved_message_ref,
        item_id=raw_item_id,
        subject=reply_subject,
    )
    logger.info("reply_message ok: item_id_hash=%s", hash_item_id(raw_item_id)[:10])
    return {
        "status": "success",
        "operation": "reply",
        "audit_id": audit_id,
        "message_ref": resolved_message_ref,
        "subject": reply_subject,
    }

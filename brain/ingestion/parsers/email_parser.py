"""Email parser using Python's built-in email module."""

import email
import html
import re
from dataclasses import dataclass
from email.utils import parsedate_to_datetime


@dataclass
class ParsedEmail:
    title: str
    content: str
    author: str | None = None
    source_ref: str | None = None     # Message-ID — unique per email
    metadata: dict | None = None
    created_at: object = None


def _strip_html(text: str) -> str:
    """Minimal HTML → plain text conversion."""
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.S | re.I)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<p[^>]*>", "\n\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _get_body(msg: email.message.Message) -> str:
    """Extract plain text body from a MIME message."""
    body_plain = []
    body_html = []

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = part.get("Content-Disposition", "")
            if "attachment" in cd:
                continue
            charset = part.get_content_charset() or "utf-8"
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            decoded = payload.decode(charset, errors="replace")
            if ct == "text/plain":
                body_plain.append(decoded)
            elif ct == "text/html":
                body_html.append(decoded)
    else:
        charset = msg.get_content_charset() or "utf-8"
        payload = msg.get_payload(decode=True)
        if payload:
            decoded = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                body_html.append(decoded)
            else:
                body_plain.append(decoded)

    if body_plain:
        return "\n".join(body_plain)
    if body_html:
        return _strip_html("\n".join(body_html))
    return ""


def _thread_id(msg: email.message.Message) -> str | None:
    """Derive a stable thread ID from References or In-Reply-To.

    Returns the first (oldest) Message-ID in the chain so all replies
    share the same thread_id in metadata.
    """
    references = msg.get("References", "").split()
    if references:
        return references[0].strip()
    in_reply_to = msg.get("In-Reply-To", "").strip()
    return in_reply_to or None


def parse_email(raw: str) -> ParsedEmail:
    """Parse a raw MIME email string."""
    msg = email.message_from_string(raw)

    subject = msg.get("Subject", "No Subject")
    from_addr = msg.get("From", "")
    to_addr = msg.get("To", "")
    cc_addr = msg.get("Cc", "")
    date_str = msg.get("Date", "")
    message_id = msg.get("Message-ID", "").strip()
    thread = _thread_id(msg)

    created_at = None
    if date_str:
        try:
            created_at = parsedate_to_datetime(date_str)
        except Exception:
            pass

    body = _get_body(msg)

    header_block = f"From: {from_addr}\nTo: {to_addr}"
    if cc_addr:
        header_block += f"\nCc: {cc_addr}"
    header_block += f"\nSubject: {subject}"
    if date_str:
        header_block += f"\nDate: {date_str}"
    content = f"{header_block}\n\n{body}"

    metadata: dict = {
        "from": from_addr,
        "to": to_addr,
        "subject": subject,
        "message_id": message_id,
    }
    if cc_addr:
        metadata["cc"] = cc_addr
    if thread:
        metadata["thread_id"] = thread

    return ParsedEmail(
        title=subject,
        content=content,
        author=from_addr or None,
        source_ref=message_id or None,
        created_at=created_at,
        metadata=metadata,
    )

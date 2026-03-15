"""
Chat history parser.

Supports:
- Claude.ai export format  (array of conversations with `chat_messages`,
                             each message has `sender` and `text`)
- Generic JSON format       (array or single object with `messages`,
                             each message has `role` and `content`)

Each conversation produces one ParsedChat. A file with N conversations
produces N documents in the brain.

Claude export: Settings → Data & Privacy → Export Data → conversations.json
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class ParsedChat:
    title: str
    content: str                    # formatted full conversation
    source_ref: str | None = None   # conversation UUID — used for dedup
    created_at: object = None
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Message formatters
# ---------------------------------------------------------------------------

def _ts(timestamp: str) -> str:
    """Return a short date string from an ISO timestamp, or empty string."""
    try:
        return timestamp[:10]
    except Exception:
        return ""


def _format_claude(messages: list[dict]) -> str:
    """Format Claude-export messages (sender: human|assistant, text: str)."""
    lines = []
    for msg in messages:
        sender = msg.get("sender", "")
        text = (msg.get("text") or "").strip()
        ts = _ts(msg.get("created_at", ""))
        if not text:
            continue
        label = "Human" if sender == "human" else "Assistant"
        header = f"[{label}]" + (f" [{ts}]" if ts else "")
        lines.append(f"{header}\n{text}")
    return "\n\n".join(lines)


def _format_generic(messages: list[dict]) -> str:
    """Format generic messages (role: user|assistant|system, content: str|list)."""
    _labels = {"user": "Human", "assistant": "Assistant", "system": "System"}
    lines = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        # Anthropic API stores content as a list of blocks
        if isinstance(content, list):
            content = " ".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        content = content.strip()
        if not content:
            continue
        ts = _ts(msg.get("timestamp") or msg.get("created_at") or "")
        label = _labels.get(role, role.title())
        header = f"[{label}]" + (f" [{ts}]" if ts else "")
        lines.append(f"{header}\n{content}")
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Conversation parsing
# ---------------------------------------------------------------------------

def _parse_one(conv: dict, index: int) -> ParsedChat:
    """Parse a single conversation dict."""
    if "chat_messages" in conv:
        # Claude.ai export format
        messages = conv["chat_messages"]
        content = _format_claude(messages)
        msg_count = len(messages)
    elif "messages" in conv:
        # Generic format
        messages = conv["messages"]
        content = _format_generic(messages)
        msg_count = len(messages)
    else:
        raise ValueError(
            f"Conversation at index {index} has no 'chat_messages' or 'messages' key."
        )

    title = (conv.get("name") or conv.get("title") or f"Conversation {index + 1}").strip()
    source_ref = conv.get("uuid") or conv.get("id") or None

    created_at = None
    ts = conv.get("created_at") or conv.get("timestamp")
    if ts:
        try:
            created_at = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except Exception:
            pass

    updated_at = conv.get("updated_at")

    metadata: dict = {"message_count": msg_count}
    if source_ref:
        metadata["conversation_id"] = source_ref
    if updated_at:
        metadata["updated_at"] = updated_at

    return ParsedChat(
        title=title,
        content=content,
        source_ref=source_ref,
        created_at=created_at,
        metadata=metadata,
    )


def parse_chat_file(path: str | Path) -> list[ParsedChat]:
    """Parse a chat history JSON file into a list of conversations.

    Returns one ParsedChat per conversation found in the file.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Chat file not found: {path}")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in {path}: {e}") from e

    if isinstance(data, list):
        return [_parse_one(conv, i) for i, conv in enumerate(data) if isinstance(conv, dict)]

    if isinstance(data, dict):
        # Could be a wrapper object: {"conversations": [...]}
        if "conversations" in data:
            convs = data["conversations"]
            return [_parse_one(c, i) for i, c in enumerate(convs) if isinstance(c, dict)]
        return [_parse_one(data, 0)]

    raise ValueError(f"Unexpected JSON structure in {path}: expected array or object.")

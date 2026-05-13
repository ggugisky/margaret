from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+\.md(?:#[^)]+)?)\)")
_BARE_MARKDOWN_PATH_RE = re.compile(
    r"(?<![\w/.-])((?:[~./]|[A-Za-z0-9_-])[\w ./~@()+,-]*?\.md)(?:#[\w.-]+)?"
)
_MAX_DOCUMENT_BYTES = 256 * 1024


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _clean_link_target(target: str) -> str:
    value = target.strip().strip("<>").strip("'\"")
    parsed = urlparse(value)
    if parsed.scheme and parsed.scheme != "file":
        return ""
    if parsed.scheme == "file":
        value = parsed.path
    else:
        value = value.split("#", 1)[0]
    return unquote(value).strip()


def _resolve_markdown_path(
    target: str,
    *,
    workspace_path: str | None,
    allowed_roots: list[str],
) -> Path | None:
    cleaned = _clean_link_target(target)
    if not cleaned:
        return None

    base_dirs = [Path(workspace_path).expanduser()] if workspace_path else []
    candidate = Path(cleaned).expanduser()
    candidates = [candidate] if candidate.is_absolute() else [
        base / candidate for base in base_dirs
    ]
    roots = [Path(root).expanduser().resolve() for root in allowed_roots if root]
    for item in candidates:
        try:
            resolved = item.resolve()
        except OSError:
            continue
        if resolved.suffix.lower() != ".md" or not resolved.is_file():
            continue
        if roots and not any(_is_relative_to(resolved, root) for root in roots):
            continue
        return resolved
    return None


def collect_markdown_documents(
    text: str,
    *,
    workspace_path: str | None,
    allowed_roots: list[str],
) -> list[dict[str, Any]]:
    """Return linked Markdown files as payloads for voice clients.

    Clients cannot open local filesystem links from a phone, so the gateway sends
    small linked Markdown documents inline as companion events.
    """

    seen: set[Path] = set()
    documents: list[dict[str, Any]] = []
    matches: list[tuple[str, str | None]] = []

    for match in _MARKDOWN_LINK_RE.finditer(text):
        matches.append((match.group(2), match.group(1).strip() or None))
    for match in _BARE_MARKDOWN_PATH_RE.finditer(text):
        matches.append((match.group(1), None))

    for target, label in matches:
        path = _resolve_markdown_path(
            target,
            workspace_path=workspace_path,
            allowed_roots=allowed_roots,
        )
        if path is None or path in seen:
            continue
        seen.add(path)
        try:
            size = path.stat().st_size
            if size > _MAX_DOCUMENT_BYTES:
                continue
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        documents.append(
            {
                "id": f"md:{len(documents) + 1}",
                "title": label or path.stem,
                "path": os.fspath(path),
                "filename": path.name,
                "mime_type": "text/markdown",
                "content": content,
                "size_bytes": size,
            }
        )

    return documents

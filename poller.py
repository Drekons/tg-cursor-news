#!/usr/bin/env python3
"""Poll public Cursor/xAI feeds and post new items to a Telegram channel."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

USER_AGENT = "tg-cursor-news/0.1 (feed poller)"
TIMEOUT_SEC = 20
RETRIES = 3
MAX_SEND_PER_RUN = 8
SEND_PAUSE_SEC = 1.0

CHANGELOG_URL = "https://cursor.com/changelog/rss.xml"
XAI_NOTES_MD_URL = "https://docs.x.ai/developers/release-notes.md"
XAI_NOTES_PAGE_URL = "https://docs.x.ai/developers/release-notes"
XAI_NOTES_LINK = "https://docs.x.ai/developers/release-notes"
STATUS_URL = "https://status.x.ai/feed.xml"

SOURCE_CHANGELOG = "changelog"
SOURCE_XAI_NOTES = "xai-notes"
SOURCE_STATUS = "status"

STATUS_SKIP_MARKERS = ("maintenance", "scheduled")
STATUS_INCIDENT_CATS = ("outage", "disruption", "degraded", "incident")


@dataclass(frozen=True)
class Item:
    source: str
    key: str
    title: str
    summary: str
    link: str
    published: datetime | None = None


def local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def strip_html(text: str) -> str:
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def clip_summary(text: str, max_chars: int = 400) -> str:
    text = strip_html(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    for sep in (". ", "? ", "! "):
        idx = cut.rfind(sep)
        if idx >= 80:
            return cut[: idx + 1].strip()
    idx = cut.rfind(" ")
    return (cut[:idx] if idx > 0 else cut).rstrip() + "…"


def normalize_heading(title: str) -> str:
    title = title.replace("\\_", "_")
    title = re.sub(r"[*`]+", "", title)
    title = html.unescape(title)
    return re.sub(r"\s+", " ", title).strip().lower()


def heading_anchor(title: str) -> str:
    slug = normalize_heading(title)
    slug = re.sub(r"[^a-z0-9_]+", "-", slug)
    return slug.strip("-")


def parse_pubdate(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_month_heading(heading: str) -> datetime | None:
    heading = heading.strip()
    now = datetime.now(timezone.utc)
    match = re.fullmatch(r"([A-Za-z]+)\s+(\d{4})", heading)
    if match:
        year = int(match.group(2))
        month_name = match.group(1)
    else:
        match = re.fullmatch(r"([A-Za-z]+)", heading)
        if not match:
            return None
        year = now.year
        month_name = match.group(1)
    try:
        return datetime.strptime(f"{month_name} {year}", "%B %Y").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def rss_items(xml_text: str) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_text)
    out: list[dict[str, Any]] = []
    for el in root.iter():
        if local_tag(el.tag) != "item":
            continue
        rec: dict[str, Any] = {"categories": []}
        for child in el:
            name = local_tag(child.tag)
            value = (child.text or "").strip()
            if name == "category":
                if value:
                    rec["categories"].append(value)
            elif name in {"title", "link", "guid", "description", "pubDate"}:
                rec[name] = value
        out.append(rec)
    return out


def parse_changelog(xml_text: str) -> list[Item]:
    items: list[Item] = []
    for rec in rss_items(xml_text):
        guid = rec.get("guid") or rec.get("link") or ""
        title = rec.get("title") or ""
        if not guid or not title:
            continue
        items.append(
            Item(
                source=SOURCE_CHANGELOG,
                key=guid,
                title=title,
                summary=clip_summary(rec.get("description") or ""),
                link=rec.get("link") or guid,
                published=parse_pubdate(rec.get("pubDate")),
            )
        )
    return items


def parse_xai_notes(md_text: str) -> list[Item]:
    items: list[Item] = []
    month_dt: datetime | None = None
    current: dict[str, Any] | None = None

    def flush() -> None:
        if not current:
            return
        title = current["title"]
        body = "\n".join(current["lines"]).strip()
        if not title:
            return
        items.append(
            Item(
                source=SOURCE_XAI_NOTES,
                key=normalize_heading(title),
                title=re.sub(r"\\_", "_", title).strip(),
                summary=clip_summary(body),
                link=f"{XAI_NOTES_LINK}#{heading_anchor(title)}",
                published=current["published"],
            )
        )

    for raw in md_text.splitlines():
        line = raw.rstrip()
        if line.startswith("### ") and not line.startswith("#### "):
            flush()
            current = {
                "title": line[4:].strip(),
                "lines": [],
                "published": month_dt,
            }
            continue
        if line.startswith("## ") and not line.startswith("### "):
            flush()
            current = None
            month_dt = parse_month_heading(line[3:])
            continue
        if current is not None:
            current["lines"].append(line)
    flush()
    return items


def _status_field(description: str, name: str) -> str:
    match = re.search(rf"{name}:\s*([^<\n]+)", description, re.I)
    return match.group(1).strip() if match else ""


def is_status_maintenance(rec: dict[str, Any]) -> bool:
    cats = [c.lower() for c in rec.get("categories") or []]
    blob = " ".join(
        [
            rec.get("title") or "",
            strip_html(rec.get("description") or ""),
            " ".join(cats),
        ]
    ).lower()
    if any(marker in cat for cat in cats for marker in STATUS_SKIP_MARKERS):
        return True
    status = _status_field(rec.get("description") or "", "Status").lower()
    if any(marker in status for marker in STATUS_SKIP_MARKERS):
        return True
    return "scheduled maintenance" in blob


def is_status_resolved(rec: dict[str, Any]) -> bool:
    cats = [c.lower() for c in rec.get("categories") or []]
    if "resolved" in cats:
        return True
    return _status_field(rec.get("description") or "", "Status").lower() == "resolved"


def is_status_incident(rec: dict[str, Any]) -> bool:
    if is_status_maintenance(rec):
        return False
    cats = [c.lower() for c in rec.get("categories") or []]
    if any(cat in STATUS_INCIDENT_CATS for cat in cats):
        return True
    status = _status_field(rec.get("description") or "", "Status").lower()
    return status in {
        "investigating",
        "identified",
        "monitoring",
        "update",
        "resolved",
        "outage",
        "disruption",
    }


def parse_status(xml_text: str) -> list[Item]:
    """One item per guid; skip maintenance; emit open and/or resolved keys."""
    by_guid: dict[str, dict[str, Any]] = {}
    for rec in rss_items(xml_text):
        guid = rec.get("guid") or ""
        if not guid or guid in by_guid:
            continue
        by_guid[guid] = rec

    items: list[Item] = []
    for guid, rec in by_guid.items():
        if not is_status_incident(rec):
            continue
        title = rec.get("title") or guid
        severity = _status_field(rec.get("description") or "", "Severity")
        summary = clip_summary(rec.get("description") or "")
        if severity and severity.lower() not in summary.lower():
            summary = clip_summary(f"Severity: {severity}. {summary}")
        published = parse_pubdate(rec.get("pubDate"))
        link = rec.get("link") or f"https://status.x.ai/{guid}"
        items.append(
            Item(
                source=SOURCE_STATUS,
                key=f"{guid}:open",
                title=f"Incident: {title}",
                summary=summary,
                link=link,
                published=published,
            )
        )
        if is_status_resolved(rec):
            items.append(
                Item(
                    source=SOURCE_STATUS,
                    key=f"{guid}:resolved",
                    title=f"Resolved: {title}",
                    summary=summary,
                    link=link,
                    published=published,
                )
            )
    return items


def fetch(url: str, *, accept: str | None = None) -> str:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "ru, en;q=0.8",
    }
    if accept:
        headers["Accept"] = accept
    last_error: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
                status = getattr(resp, "status", 200)
                body = resp.read()
                if status >= 500:
                    raise urllib.error.HTTPError(
                        url, status, f"HTTP {status}", resp.headers, None
                    )
                charset = resp.headers.get_content_charset() or "utf-8"
                return body.decode(charset, errors="replace")
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 404 or exc.code < 500:
                raise
            if attempt == RETRIES:
                raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt == RETRIES:
                raise
        time.sleep(2 * attempt)
    raise RuntimeError(f"fetch failed: {url}") from last_error


def fetch_xai_notes() -> str:
    try:
        return fetch(XAI_NOTES_MD_URL, accept="text/markdown")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        return fetch(XAI_NOTES_PAGE_URL, accept="text/markdown")


def empty_state() -> dict[str, Any]:
    return {
        "version": 1,
        "initialized": [],
        "seen": {
            SOURCE_CHANGELOG: [],
            SOURCE_XAI_NOTES: [],
            SOURCE_STATUS: [],
        },
    }


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return empty_state()
    data = json.loads(path.read_text(encoding="utf-8"))
    state = empty_state()
    state["initialized"] = list(data.get("initialized") or [])
    seen = data.get("seen") or {}
    for source in (SOURCE_CHANGELOG, SOURCE_XAI_NOTES, SOURCE_STATUS):
        state["seen"][source] = list(seen.get(source) or [])
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    tmp.replace(path)


def source_seen(state: dict[str, Any], source: str) -> set[str]:
    return set(state["seen"].get(source) or [])


def mark_seen(state: dict[str, Any], source: str, keys: list[str]) -> None:
    seen = list(state["seen"].get(source) or [])
    have = set(seen)
    for key in keys:
        if key not in have:
            seen.append(key)
            have.add(key)
    state["seen"][source] = seen


def select_updates(
    state: dict[str, Any], source: str, items: list[Item]
) -> tuple[list[Item], list[str], bool]:
    """Return (to_send, keys_to_record, bootstrap)."""
    keys = [item.key for item in items]
    if source not in state["initialized"]:
        return [], keys, True
    seen = source_seen(state, source)
    to_send = [item for item in items if item.key not in seen]
    to_send.sort(key=lambda item: item.published or datetime.max.replace(tzinfo=timezone.utc))
    return to_send, [], False


def format_message(item: Item) -> str:
    labels = {
        SOURCE_CHANGELOG: "Cursor Changelog",
        SOURCE_XAI_NOTES: "xAI Release Notes",
        SOURCE_STATUS: "xAI Status",
    }
    parts = [labels.get(item.source, item.source), item.title]
    if item.summary:
        parts.append(item.summary)
    if item.link:
        parts.append(item.link)
    return "\n\n".join(parts)


def send_telegram(token: str, chat_id: str, text: str) -> None:
    payload = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if not body.get("ok"):
        raise RuntimeError(f"telegram sendMessage failed: {body}")


def collect_sources() -> tuple[dict[str, list[Item]], list[str]]:
    fetched: dict[str, list[Item]] = {}
    errors: list[str] = []

    try:
        fetched[SOURCE_CHANGELOG] = parse_changelog(fetch(CHANGELOG_URL))
    except Exception as exc:  # noqa: BLE001 — isolate one source
        errors.append(f"{SOURCE_CHANGELOG}: {exc}")

    try:
        fetched[SOURCE_XAI_NOTES] = parse_xai_notes(fetch_xai_notes())
    except Exception as exc:  # noqa: BLE001
        errors.append(f"{SOURCE_XAI_NOTES}: {exc}")

    try:
        fetched[SOURCE_STATUS] = parse_status(fetch(STATUS_URL))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"{SOURCE_STATUS}: {exc}")

    return fetched, errors


def run(state_path: Path, *, dry_run: bool) -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    can_send = bool(token and chat_id) and not dry_run

    state = load_state(state_path)
    fetched, errors = collect_sources()
    outgoing: list[Item] = []

    for source, items in fetched.items():
        to_send, bootstrap_keys, bootstrap = select_updates(state, source, items)
        if bootstrap:
            mark_seen(state, source, bootstrap_keys)
            if source not in state["initialized"]:
                state["initialized"].append(source)
            print(f"{source}: bootstrap {len(bootstrap_keys)} keys, send 0", file=sys.stderr)
            continue
        outgoing.extend(to_send)

    outgoing.sort(key=lambda item: item.published or datetime.max.replace(tzinfo=timezone.utc))
    limited = outgoing[:MAX_SEND_PER_RUN]
    leftover = len(outgoing) - len(limited)
    sent = 0

    for item in limited:
        text = format_message(item)
        if can_send:
            send_telegram(token, chat_id, text)
            mark_seen(state, item.source, [item.key])
            sent += 1
            time.sleep(SEND_PAUSE_SEC)
        else:
            reason = "dry-run" if dry_run or not (token and chat_id) else "skip"
            print(f"{reason}: {item.source} {item.key}", file=sys.stderr)
            print(text)
            print("---")
            if not token or not chat_id:
                # Don't drop items: retry when secrets appear.
                continue
            # Explicit --dry-run keeps keys unseen so a real run still posts.

    save_state(state_path, state)
    print(
        f"fetched={list(fetched)} send={sent}/{len(limited)} queued={leftover} errors={len(errors)}",
        file=sys.stderr,
    )
    for err in errors:
        print(err, file=sys.stderr)
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state",
        default=os.environ.get("LAST_SEEN_PATH", "last-seen.json"),
        help="path to last-seen JSON (default: last-seen.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print messages, do not call Telegram, do not mark sent keys",
    )
    args = parser.parse_args(argv)
    return run(Path(args.state), dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())

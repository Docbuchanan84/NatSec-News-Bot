from __future__ import annotations

import asyncio
import base64
import imaplib
import json
import logging
import os
import re
import socket
from dataclasses import dataclass
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.policy import default
from email.utils import format_datetime, parsedate_to_datetime, parseaddr
from html.parser import HTMLParser
from typing import Any, Iterable
from urllib.parse import parse_qsl, unquote, urlparse

from app.feed_fetcher import FeedFetchError, FeedFetchResult, clean_html_text
from app.models import EmailSourceRuntime, FeedEntry
from app.normalizer import normalize_article_url, stable_hash

logger = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)
MARKDOWN_LINK_RE = re.compile(r"\[([^\]\n]{2,180})\]\((https?://[^)\s]+)\)")
BRACKET_URL_RE = re.compile(r"(?P<label>[^\n\[][\w\s,:;'.\"()/-]{2,160}?)\s*\[\s*(?P<url>https?://[^\]\s]+)\s*\]")
URLISH_TITLE_RE = re.compile(r"^(?:https?://)?(?:www\.)?[\w.-]+\.[a-z]{2,}(?:[/\?#].*)?$", re.IGNORECASE)
EMBEDDED_URL_RE = re.compile(r"https?://[^\s\"'<>\\]+", re.IGNORECASE)
DATE_HEADING_RE = re.compile(
    r"^(?:mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?),?\s+"
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|"
    r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+\d{1,2},?\s+\d{4}$",
    re.IGNORECASE,
)
MAX_SPLIT_ARTICLES = 12
TRACKING_HOST_FRAGMENTS = (
    "hubspotlinks.com",
    "pardot.",
    "dripemail",
    "list-manage.com",
    "mailchi.mp",
    "sendgrid.net",
    "hs-sites.com",
    "news.scoopnewsgroup.com",
)
UTILITY_HOSTS = {
    "surveys.hotjar.com",
    "fedtalks.upgather.com",
    "defensetalks.upgather.com",
}
UTILITY_PATH_TERMS = (
    "unsubscribe",
    "manage-preferences",
    "email-preferences",
    "subscription-preferences",
    "preferences-center",
    "view-in-browser",
    "webmail",
    "survey",
    "register",
    "registration",
    "fedtalks",
    "defensetalks",
    "fedscoop50",
)


@dataclass(frozen=True)
class EmailLink:
    url: str
    label: str
    context: str


@dataclass(frozen=True)
class ParsedEmail:
    uid: str
    message_id: str | None
    subject: str
    sender: str
    sender_email: str
    list_id: str | None
    body: str | None
    formatted_body: str | None
    canonical_url: str | None
    raw_date: str | None
    links: tuple[EmailLink, ...]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class EmailArticle:
    title: str
    url: str
    summary: str | None
    routing_summary: str | None
    index: int
    guid_suffix: str


class EmailIngestService:
    def __init__(
        self,
        timeout_seconds: int,
        max_messages_per_source: int,
        max_routing_summary_chars: int = 2000,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_messages_per_source = max_messages_per_source
        self.max_routing_summary_chars = max_routing_summary_chars

    async def fetch(self, source: EmailSourceRuntime, since_uid: str | None = None) -> FeedFetchResult:
        parsed = await asyncio.to_thread(self._fetch_sync, source, since_uid)
        entries = tuple(
            entry
            for item in parsed
            for entry in _entries_from_email(
                source,
                item,
                max_routing_summary_chars=self.max_routing_summary_chars,
            )
        )
        high_water = _max_uid((item.uid for item in parsed), since_uid)
        return FeedFetchResult(feed=source, entries=entries, cursor_high_water=high_water)

    def _fetch_sync(self, source: EmailSourceRuntime, since_uid: str | None) -> tuple[ParsedEmail, ...]:
        host = _required_env(source.imap_host_env)
        username = _required_env(source.username_env)
        password = _required_env(source.password_env)
        port = _imap_port(source.imap_port_env)
        timeout = source.fetch_timeout_seconds or self.timeout_seconds
        prior_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(timeout)
        client: imaplib.IMAP4_SSL | None = None
        try:
            client = imaplib.IMAP4_SSL(host, port)
            client.login(username, password)
            status, _data = client.select(source.mailbox, readonly=True)
            if status != "OK":
                raise FeedFetchError(f"IMAP select failed for mailbox {source.mailbox!r}: {status}")
            uids = self._search_uids(client, since_uid)
            messages: list[ParsedEmail] = []
            for uid in uids[-self._message_limit(source) :]:
                raw_message = self._fetch_message(client, uid)
                if raw_message is None:
                    continue
                parsed = parse_email_message(uid, raw_message)
                if not email_matches_source(parsed, source):
                    continue
                messages.append(parsed)
            return tuple(messages)
        except FeedFetchError:
            raise
        except (imaplib.IMAP4.error, OSError, TimeoutError) as exc:
            detail = str(exc) or exc.__class__.__name__
            raise FeedFetchError(f"IMAP request failed: {detail}") from exc
        finally:
            socket.setdefaulttimeout(prior_timeout)
            if client is not None:
                try:
                    client.close()
                except imaplib.IMAP4.error:
                    pass
                try:
                    client.logout()
                except imaplib.IMAP4.error:
                    pass

    def _search_uids(self, client: imaplib.IMAP4_SSL, since_uid: str | None) -> list[str]:
        status, data = client.uid("SEARCH", None, "ALL")
        if status != "OK":
            raise FeedFetchError(f"IMAP search failed: {status}")
        raw_uids = data[0] if data else b""
        if isinstance(raw_uids, str):
            raw_uids = raw_uids.encode("ascii", errors="ignore")
        uids = [value.decode("ascii", errors="ignore") for value in raw_uids.split()]
        minimum_uid = int(since_uid) if since_uid and since_uid.isdigit() else 0
        return sorted((uid for uid in uids if uid.isdigit() and int(uid) > minimum_uid), key=int)

    def _fetch_message(self, client: imaplib.IMAP4_SSL, uid: str) -> bytes | None:
        status, data = client.uid("FETCH", uid, "(BODY.PEEK[])")
        if status != "OK":
            logger.warning("IMAP fetch failed for UID %s: %s", uid, status)
            return None
        for item in data:
            if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
                return item[1]
        return None

    def _message_limit(self, source: EmailSourceRuntime) -> int:
        return source.max_messages_per_poll or self.max_messages_per_source


def parse_email_message(uid: str, raw_message: bytes) -> ParsedEmail:
    message = message_from_bytes(raw_message, policy=default)
    subject = _decode_header_value(message.get("Subject")) or "Untitled email"
    sender = _decode_header_value(message.get("From")) or "Unknown sender"
    _sender_name, sender_email = parseaddr(sender)
    sender_email = sender_email.casefold()
    list_id = _decode_header_value(message.get("List-ID")) or _decode_header_value(message.get("List-Id"))
    message_id = _clean_message_id(_decode_header_value(message.get("Message-ID")))
    raw_date = _message_date(message)
    body, formatted_body, links = _message_content(message)
    canonical_url = _first_content_url(formatted_body or body, links)
    metadata = {
        "source": "email",
        "uid": uid,
        "message_id": message_id or "",
        "from": sender,
        "from_email": sender_email,
        "subject": subject,
        "list_id": list_id or "",
        "canonical_url": canonical_url or "",
        "formatted_body": formatted_body or "",
        "link_count": len(links),
    }
    return ParsedEmail(
        uid=uid,
        message_id=message_id,
        subject=subject,
        sender=sender,
        sender_email=sender_email,
        list_id=list_id,
        body=body,
        formatted_body=formatted_body,
        canonical_url=canonical_url,
        raw_date=raw_date,
        links=links,
        metadata=metadata,
    )


def email_matches_source(parsed: ParsedEmail, source: EmailSourceRuntime) -> bool:
    if source.match_all:
        return True
    sender_text = f"{parsed.sender} {parsed.sender_email}".casefold()
    list_id = (parsed.list_id or "").casefold()
    subject = parsed.subject.casefold()
    if source.from_contains and not any(value in sender_text for value in source.from_contains):
        return False
    if source.list_id_contains and not any(value in list_id for value in source.list_id_contains):
        return False
    if source.subject_contains and not any(value in subject for value in source.subject_contains):
        return False
    return True


def _entries_from_email(
    source: EmailSourceRuntime,
    parsed: ParsedEmail,
    *,
    max_routing_summary_chars: int = 2000,
) -> tuple[FeedEntry, ...]:
    articles = _split_email_articles(parsed, max_routing_summary_chars=max_routing_summary_chars)
    if not articles:
        return (_entry_from_email(source, parsed, max_routing_summary_chars=max_routing_summary_chars),)
    return tuple(
        _entry_from_email(source, parsed, article, max_routing_summary_chars=max_routing_summary_chars)
        for article in articles
    )


def _entry_from_email(
    source: EmailSourceRuntime,
    parsed: ParsedEmail,
    article: EmailArticle | None = None,
    *,
    max_routing_summary_chars: int = 2000,
) -> FeedEntry:
    guid = parsed.message_id or f"imap:{source.feed_key}:{parsed.uid}"
    title = parsed.subject
    url = _clean_article_url(parsed.canonical_url)
    summary = _display_summary(parsed.formatted_body or parsed.body, title, url)
    routing_summary = _routing_summary(parsed.formatted_body or parsed.body, limit=max_routing_summary_chars)
    split_metadata: dict[str, Any] = {}
    if article is not None:
        guid = f"{guid}#{article.guid_suffix}"
        title = article.title
        url = _clean_article_url(article.url)
        summary = article.summary
        routing_summary = article.routing_summary
        split_metadata = {
            "split": True,
            "article_index": article.index,
            "article_title": article.title,
            "parent_subject": parsed.subject,
        }
    low_signal = _is_low_signal_email(title, summary, url, parsed.metadata)
    metadata = {
        **parsed.metadata,
        "mailbox": source.mailbox,
        "source_key": source.feed_key,
        "display_summary": summary or "",
        "routing_summary": routing_summary or summary or "",
        "email_low_signal": low_signal,
        **split_metadata,
    }
    return FeedEntry(
        feed_key=source.feed_key,
        feed_name=source.display_name,
        raw_guid=guid,
        raw_title=title,
        raw_url=url,
        summary=summary,
        image_url=None,
        image_source=None,
        raw_published_at=parsed.raw_date,
        parsed=metadata,
        source_id=source.source_id,
        source_class=source.source_class,
        rich_metadata=metadata,
        routing_tags=source.routing_tags,
    )


def _message_content(message: EmailMessage | Message) -> tuple[str | None, str | None, tuple[EmailLink, ...]]:
    text_parts: list[str] = []
    html_parts: list[str] = []
    for part in message.walk() if message.is_multipart() else [message]:
        content_disposition = str(part.get("Content-Disposition") or "").casefold()
        if "attachment" in content_disposition:
            continue
        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html"}:
            continue
        payload = _part_text(part)
        if not payload:
            continue
        if content_type == "text/plain":
            text_parts.append(payload)
        else:
            html_parts.append(payload)
    html_content = "\n\n".join(html_parts)
    html_markdown, html_links = _html_to_markdown(html_content) if html_content else (None, ())
    if text_parts:
        plain = _clean_body("\n\n".join(text_parts))
        if html_markdown:
            return plain, html_markdown, html_links
        formatted = _format_plain_text_markdown(plain)
        return plain, formatted, _links_from_markdown(formatted)
    if html_content:
        plain = clean_html_text(html_content)
        return plain, html_markdown, html_links
    return None, None, ()


def _part_text(part: EmailMessage | Message) -> str | None:
    try:
        if isinstance(part, EmailMessage):
            content = part.get_content()
            return str(content) if content is not None else None
        payload = part.get_payload(decode=True)
    except (LookupError, UnicodeDecodeError):
        payload = None
    if isinstance(payload, bytes):
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")
    if isinstance(payload, str):
        return payload
    return None


def _clean_body(value: str) -> str | None:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines()]
    cleaned = "\n".join(line for line in lines if line and not _is_email_boilerplate_line(line)).strip()
    return cleaned or None


def _first_content_url(value: str | None, links: tuple[EmailLink, ...]) -> str | None:
    for link in links:
        cleaned = _clean_email_link(link)
        if cleaned and _is_content_link(cleaned.label, cleaned.url, cleaned.context):
            return cleaned.url
    if not value:
        return None
    match = URL_RE.search(value)
    if not match:
        return None
    return _candidate_content_url(match.group(0).rstrip(").,;]"))


class _EmailMarkdownParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[EmailLink] = []
        self._skip_depth = 0
        self._link_href: str | None = None
        self._link_parts: list[str] = []
        self._list_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag in {"script", "style", "figure", "figcaption", "head", "meta"}:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        attr_map = {name.casefold(): value for name, value in attrs if value}
        if tag == "a":
            href = (attr_map.get("href") or "").strip()
            self._link_href = href if href.startswith(("http://", "https://")) else None
            self._link_parts = []
            return
        if tag in {"p", "div", "section", "article", "tr", "table"}:
            self._newline()
        elif tag in {"h1", "h2", "h3", "h4"}:
            self._newline()
            self.parts.append("**")
        elif tag == "br":
            self._newline()
        elif tag in {"ul", "ol"}:
            self._list_stack.append(tag)
            self._newline()
        elif tag == "li":
            self._newline()
            marker = "1. " if self._list_stack and self._list_stack[-1] == "ol" else "- "
            self.parts.append(marker)
        elif tag == "blockquote":
            self._newline()
            self.parts.append("> ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in {"script", "style", "figure", "figcaption", "head", "meta"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag == "a":
            label = _clean_inline_text("".join(self._link_parts))
            if self._link_href:
                label = label or _url_label(self._link_href)
                markdown = f"[{_escape_link_label(label)}]({self._link_href})"
                context = _clean_inline_text("".join(self.parts[-12:]) + " " + label)
                self.parts.append(markdown)
                self.links.append(EmailLink(url=self._link_href, label=label, context=context))
            elif label:
                self.parts.append(label)
            self._link_href = None
            self._link_parts = []
            return
        if tag in {"h1", "h2", "h3", "h4"}:
            self.parts.append("**")
            self._newline()
        elif tag in {"p", "div", "section", "article", "li", "tr", "table", "blockquote"}:
            self._newline()
        elif tag in {"ul", "ol"}:
            if self._list_stack:
                self._list_stack.pop()
            self._newline()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._link_href is not None:
            self._link_parts.append(data)
        else:
            self.parts.append(data)

    def _newline(self) -> None:
        if self.parts and self.parts[-1] != "\n":
            self.parts.append("\n")


def _html_to_markdown(value: str) -> tuple[str | None, tuple[EmailLink, ...]]:
    parser = _EmailMarkdownParser()
    parser.feed(value)
    formatted = _clean_markdown_body("".join(parser.parts))
    links = tuple(
        cleaned
        for link in parser.links
        if _is_content_link(link.label, link.url, link.context)
        for cleaned in (_clean_email_link(link),)
        if cleaned is not None
    )
    return formatted, links


def _format_plain_text_markdown(value: str | None) -> str | None:
    if not value:
        return None
    formatted = BRACKET_URL_RE.sub(lambda match: _plain_bracket_link(match.group("label"), match.group("url")), value)
    formatted = URL_RE.sub(_plain_url_link, formatted)
    return _clean_markdown_body(formatted)


def _plain_bracket_link(label: str, url: str) -> str:
    cleaned_label = _clean_inline_text(label).rstrip(" .:-")
    if _is_generic_link_label(cleaned_label):
        cleaned_label = _url_label(url)
    return f"[{_escape_link_label(cleaned_label)}]({url})"


def _plain_url_link(match: re.Match[str]) -> str:
    raw = match.group(0)
    start = match.start()
    if start >= 2 and match.string[start - 2 : start] == "](":
        return raw
    url = raw.rstrip(").,;]")
    trailing = raw[len(url) :]
    return f"[{_url_label(url)}]({url}){trailing}"


def _links_from_markdown(value: str | None) -> tuple[EmailLink, ...]:
    if not value:
        return ()
    links: list[EmailLink] = []
    for match in MARKDOWN_LINK_RE.finditer(value):
        context_start = max(0, match.start() - 180)
        context_end = min(len(value), match.end() + 180)
        links.append(
            EmailLink(
                url=match.group(2),
                label=match.group(1),
                context=_clean_inline_text(value[context_start:context_end]),
            )
        )
    return tuple(
        cleaned
        for link in links
        if _is_content_link(link.label, link.url, link.context)
        for cleaned in (_clean_email_link(link),)
        if cleaned is not None
    )


def _clean_markdown_body(value: str) -> str | None:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    for raw_line in value.splitlines():
        line = re.sub(r"[ \t]+", " ", raw_line).strip()
        if not line or _is_email_boilerplate_line(line):
            continue
        line = _strip_low_value_markdown_links(line).strip()
        if not line or _is_email_boilerplate_line(line):
            continue
        if _line_contains_only_boilerplate_link(line):
            continue
        line = _clean_content_markdown_links(line)
        line = re.sub(r"\s+([,.;:!?])", r"\1", line)
        lines.append(line)
    cleaned = _squash_duplicate_lines(lines)
    return "\n".join(cleaned).strip() or None


def _squash_duplicate_lines(lines: list[str]) -> list[str]:
    output: list[str] = []
    previous = ""
    for line in lines:
        if line == previous:
            continue
        output.append(line)
        previous = line
    return output


def _split_email_articles(
    parsed: ParsedEmail,
    *,
    max_routing_summary_chars: int = 2000,
) -> tuple[EmailArticle, ...]:
    formatted = parsed.formatted_body or parsed.body or ""
    candidates: list[EmailArticle] = []
    seen_urls: set[str] = set()
    for link in parsed.links:
        if not _is_content_link(link.label, link.url, link.context):
            continue
        title = _article_title_for_link(link, formatted)
        if not title:
            continue
        cleaned_url = _clean_article_url(link.url)
        if not cleaned_url:
            continue
        url_key = _url_dedupe_key(cleaned_url)
        if url_key in seen_urls:
            continue
        seen_urls.add(url_key)
        index = len(candidates) + 1
        summary = _article_summary_for_link(title, link, formatted)
        routing_summary = _article_routing_summary_for_link(title, link, formatted, limit=max_routing_summary_chars)
        candidates.append(
            EmailArticle(
                title=title,
                url=cleaned_url,
                summary=summary,
                routing_summary=routing_summary,
                index=index,
                guid_suffix=stable_hash(f"{cleaned_url}|{title}", 16),
            )
        )
        if len(candidates) >= MAX_SPLIT_ARTICLES:
            break
    if len(candidates) < 2:
        return ()
    return tuple(candidates)


def _article_title_for_link(link: EmailLink, formatted: str) -> str | None:
    label = _clean_inline_text(link.label)
    if _is_meaningful_article_title(label):
        return label
    lines = [line for line in formatted.splitlines() if line.strip()]
    match_index = _line_index_for_url(link.url, lines)
    if match_index is None:
        return None
    prior_lines = list(reversed(lines[max(0, match_index - 4) : match_index]))
    for prefer_heading in (True, False):
        for line in prior_lines:
            if prefer_heading and "**" not in line:
                continue
            candidate = _clean_article_title_line(line)
            if _is_meaningful_article_title(candidate):
                return candidate
    return None


def _article_summary_for_link(title: str, link: EmailLink, formatted: str) -> str | None:
    lines = _article_block_lines(link.url, formatted)
    useful: list[str] = []
    for line in lines:
        cleaned = line.strip()
        if not cleaned or _is_email_boilerplate_line(cleaned):
            continue
        if _line_contains_only_boilerplate_link(cleaned):
            continue
        if _same_text(_clean_article_title_line(cleaned), title):
            continue
        if MARKDOWN_LINK_RE.fullmatch(cleaned) and link.url not in cleaned:
            continue
        useful.append(cleaned)
        if len(useful) >= 4:
            break
    if not useful:
        return f"[Read article]({link.url})"
    summary = "\n".join(useful)
    if link.url not in summary:
        summary = f"{summary}\n[Read article]({link.url})"
    return _truncate_summary(summary, 900)


def _article_routing_summary_for_link(
    title: str,
    link: EmailLink,
    formatted: str,
    *,
    limit: int = 2000,
) -> str | None:
    lines = _article_block_lines(link.url, formatted)
    useful: list[str] = []
    for line in lines:
        cleaned = line.strip()
        if not cleaned or _is_email_boilerplate_line(cleaned):
            continue
        if _line_contains_only_boilerplate_link(cleaned):
            continue
        if _same_text(_clean_article_title_line(cleaned), title):
            continue
        useful.append(MARKDOWN_LINK_RE.sub(lambda match: match.group(1), cleaned))
        if len(useful) >= 10:
            break
    if not useful:
        return None
    return _truncate_summary("\n".join(useful), limit)


def _display_summary(value: str | None, title: str, url: str | None) -> str | None:
    if not value:
        return None
    useful: list[str] = []
    for line in value.splitlines():
        cleaned = line.strip()
        if not cleaned or _is_email_boilerplate_line(cleaned):
            continue
        if _line_contains_only_boilerplate_link(cleaned):
            continue
        if _same_text(_clean_article_title_line(cleaned), title):
            continue
        useful.append(cleaned)
        if len(useful) >= 5:
            break
    if not useful:
        return f"[Read article]({url})" if url else None
    summary = "\n".join(useful)
    if url and url not in summary:
        summary = f"{summary}\n[Read article]({url})"
    return _truncate_summary(summary, 900)


def _routing_summary(value: str | None, *, limit: int = 2000) -> str | None:
    if not value:
        return None
    lines: list[str] = []
    for line in value.splitlines():
        cleaned = line.strip()
        if not cleaned or _is_email_boilerplate_line(cleaned):
            continue
        if _line_contains_only_boilerplate_link(cleaned):
            continue
        lines.append(MARKDOWN_LINK_RE.sub(lambda match: match.group(1), cleaned))
        if len(lines) >= 14:
            break
    return _truncate_summary("\n".join(lines), limit)


def _truncate_summary(value: str, limit: int) -> str:
    cleaned = value.strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "..."


def _is_low_signal_email(title: str, summary: str | None, url: str | None, metadata: dict[str, Any]) -> bool:
    text = f"{title}\n{summary or ''}\n{metadata.get('subject', '')}".casefold()
    if not url and len(text) < 220:
        return True
    low_signal_terms = (
        "welcome to",
        "confirm your",
        "confirmation required",
        "verify your",
        "email preferences",
        "manage your subscription",
        "webinar",
        "register now",
        "save your seat",
        "course",
        "training",
        "sponsor",
        "advertise",
        "weekly video recap",
        "read in browser",
        "vulnerability summary",
    )
    if any(term in text for term in low_signal_terms):
        return True
    if "cve-" in text and text.count("cve-") >= 4:
        return True
    return False


def _article_block_lines(url: str, formatted: str) -> list[str]:
    lines = [line for line in formatted.splitlines() if line.strip()]
    match_index = _line_index_for_url(url, lines)
    if match_index is None:
        return []
    start = max(0, match_index - 2)
    for index in range(match_index - 1, max(-1, match_index - 5), -1):
        if "**" in lines[index] or _is_meaningful_article_title(_clean_article_title_line(lines[index])):
            start = index
            break
    end = min(len(lines), match_index + 3)
    for index in range(match_index + 1, min(len(lines), match_index + 6)):
        if "**" in lines[index]:
            end = index
            break
    return lines[start:end]


def _nearby_lines(url: str, formatted: str, radius: int = 3) -> list[str]:
    lines = [line for line in formatted.splitlines() if line.strip()]
    match_index = _line_index_for_url(url, lines)
    if match_index is None:
        return []
    start = max(0, match_index - radius)
    end = min(len(lines), match_index + radius + 1)
    return lines[start:end]


def _line_index_for_url(url: str, lines: list[str]) -> int | None:
    for index, line in enumerate(lines):
        if url in line:
            return index
    return None


def _is_meaningful_article_title(value: str) -> bool:
    if not value or _is_generic_link_label(value) or _is_email_boilerplate_line(value):
        return False
    if DATE_HEADING_RE.fullmatch(value.strip()):
        return False
    if _looks_like_url_title(value):
        return False
    if len(value) < 12 or len(value) > 180:
        return False
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'-]*", value)
    return len(words) >= 3


def _is_content_link(label: str, url: str, context: str = "") -> bool:
    return not _is_boilerplate_link(label, url, context) and _candidate_content_url(url) is not None


def _is_tracking_or_utility_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.casefold()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.casefold()
    query = parsed.query.casefold()
    if path in {"", "/"}:
        return True
    if host in UTILITY_HOSTS:
        return True
    if any(fragment in host for fragment in TRACKING_HOST_FRAGMENTS):
        return True
    url_text = f"{path}?{query}"
    return any(term in url_text for term in UTILITY_PATH_TERMS)


def _is_boilerplate_link(label: str, url: str, context: str = "") -> bool:
    del context
    text = f"{label} {url}".casefold()
    boilerplate_terms = (
        "unsubscribe",
        "manage preference",
        "email preference",
        "subscription preference",
        "privacy policy",
        "terms of use",
        "view in browser",
        "view this email",
        "update profile",
        "forward to a friend",
        "facebook",
        "twitter",
        "linkedin",
        "instagram",
        "youtube",
        "confirm sign-up",
        "confirm signup",
        "confirmation required",
        "advertise with us",
        "sponsored by",
        "sponsor content",
        "take our survey",
        "register now",
        "save your seat",
    )
    return any(term in text for term in boilerplate_terms)


def _is_email_boilerplate_line(value: str) -> bool:
    text = value.casefold()
    if len(text) > 220 and "unsubscribe" in text:
        return True
    if len(text) < 220 and any(
        term in text
        for term in (
            "unsubscribe",
            "manage preferences",
            "privacy policy",
            "take our survey",
            "sponsored by",
            "advertise with us",
        )
    ):
        return True
    boilerplate_prefixes = (
        "unsubscribe",
        "manage preferences",
        "update your preferences",
        "privacy policy",
        "view this email",
        "view in browser",
        "this email was sent to",
        "you are receiving this email",
        "to stop receiving",
        "follow us on",
        "copyright ",
        "advertisement",
        "sponsored by",
        "presented by",
        "register now",
        "save your seat",
    )
    return any(text.startswith(prefix) for prefix in boilerplate_prefixes)


def _line_contains_only_boilerplate_link(value: str) -> bool:
    cleaned = value.strip()
    match = MARKDOWN_LINK_RE.fullmatch(cleaned)
    return bool(match and not _is_content_link(match.group(1), match.group(2), cleaned))


def _strip_low_value_markdown_links(value: str) -> str:
    def replacement(match: re.Match[str]) -> str:
        label = match.group(1)
        url = match.group(2)
        if _is_content_link(label, url, value):
            return match.group(0)
        return ""

    return MARKDOWN_LINK_RE.sub(replacement, value)


def _clean_content_markdown_links(value: str) -> str:
    def replacement(match: re.Match[str]) -> str:
        label = match.group(1)
        url = match.group(2)
        cleaned_url = _candidate_content_url(url)
        if not cleaned_url:
            return ""
        return f"[{label}]({cleaned_url})"

    return MARKDOWN_LINK_RE.sub(replacement, value)


def _clean_article_title_line(value: str) -> str:
    value = MARKDOWN_LINK_RE.sub(lambda match: match.group(1), value)
    value = URL_RE.sub("", value)
    value = value.replace("**", "")
    return _clean_inline_text(value).strip(" -|:")


def _is_generic_link_label(value: str) -> bool:
    normalized = _clean_inline_text(value).casefold().strip(" .:-")
    return normalized in {
        "read more",
        "read article",
        "full story",
        "learn more",
        "more",
        "here",
        "link",
        "online",
        "click here",
        "continue reading",
        "watch",
        "open",
        "confirm",
        "sign up",
        "register",
        "register now",
        "visit website",
    }


def _looks_like_url_title(value: str) -> bool:
    cleaned = _clean_inline_text(value).strip(" .:-")
    if not cleaned:
        return False
    if URL_RE.fullmatch(cleaned):
        return True
    if URLISH_TITLE_RE.fullmatch(cleaned):
        return True
    return bool(re.search(r"\b[\w.-]+\.(?:com|org|net|gov|mil|io|co|news)/\S+", cleaned, re.IGNORECASE))


def _clean_inline_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def _escape_link_label(value: str) -> str:
    return _clean_inline_text(value).replace("[", "(").replace("]", ")")


def _clean_email_link(link: EmailLink) -> EmailLink | None:
    cleaned_url = _candidate_content_url(link.url)
    if not cleaned_url:
        return None
    return EmailLink(url=cleaned_url, label=link.label, context=link.context)


def _clean_article_url(url: str | None) -> str | None:
    if not url:
        return None
    return normalize_article_url(url) or url


def _candidate_content_url(url: str | None) -> str | None:
    unwrapped = _unwrap_tracking_url(url) or url
    cleaned = _clean_article_url(unwrapped)
    if not cleaned or _is_tracking_or_utility_url(cleaned):
        return None
    return cleaned


def _unwrap_tracking_url(url: str | None, *, depth: int = 0) -> str | None:
    if not url or depth > 2:
        return None
    parsed = urlparse(url)
    host = parsed.netloc.casefold()
    if host.startswith("www."):
        host = host[4:]
    if not any(fragment in host for fragment in TRACKING_HOST_FRAGMENTS):
        return None

    for _name, value in parse_qsl(parsed.query, keep_blank_values=False):
        for candidate in _embedded_url_candidates(value):
            cleaned = _clean_article_url(_unwrap_tracking_url(candidate, depth=depth + 1) or candidate)
            if cleaned and not _is_tracking_or_utility_url(cleaned):
                return cleaned

    for segment in parsed.path.split("/"):
        for candidate in _embedded_url_candidates(segment):
            cleaned = _clean_article_url(_unwrap_tracking_url(candidate, depth=depth + 1) or candidate)
            if cleaned and not _is_tracking_or_utility_url(cleaned):
                return cleaned
        decoded = _decode_base64_json(segment)
        if decoded is not None:
            for candidate in _urls_from_json(decoded):
                cleaned = _clean_article_url(_unwrap_tracking_url(candidate, depth=depth + 1) or candidate)
                if cleaned and not _is_tracking_or_utility_url(cleaned):
                    return cleaned
    return None


def _embedded_url_candidates(value: str) -> tuple[str, ...]:
    decoded_values = {value}
    previous = value
    for _ in range(3):
        current = unquote(previous)
        if current == previous:
            break
        decoded_values.add(current)
        previous = current
    candidates: list[str] = []
    for item in decoded_values:
        normalized = item.replace("\\/", "/")
        for match in EMBEDDED_URL_RE.finditer(normalized):
            candidates.append(match.group(0).rstrip(").,;]}"))
        decoded = _decode_base64_json(item)
        if decoded is not None:
            candidates.extend(_urls_from_json(decoded))
    return tuple(candidates)


def _decode_base64_json(value: str) -> object | None:
    segments = [value]
    if "." in value:
        segments.extend(part for part in value.split(".") if part)
    for segment in segments:
        cleaned = re.sub(r"[^A-Za-z0-9_-]", "", segment)
        if len(cleaned) < 16:
            continue
        padded = cleaned + ("=" * (-len(cleaned) % 4))
        try:
            decoded = base64.urlsafe_b64decode(padded).decode("utf-8", errors="ignore")
            return json.loads(decoded)
        except (ValueError, json.JSONDecodeError):
            continue
    return None


def _urls_from_json(value: object) -> tuple[str, ...]:
    found: list[str] = []
    if isinstance(value, dict):
        for item in value.values():
            found.extend(_urls_from_json(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_urls_from_json(item))
    elif isinstance(value, str):
        found.extend(_embedded_url_candidates(value))
    return tuple(found)


def _url_label(url: str) -> str:
    parsed = urlparse(url)
    label = parsed.netloc.lower()
    if label.startswith("www."):
        label = label[4:]
    path = parsed.path.rstrip("/")
    if path and path != "/":
        label = f"{label}{path}"
    if len(label) > 70:
        label = label[:67].rstrip("/-_") + "..."
    return label or "link"


def _url_dedupe_key(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"


def _same_text(left: str, right: str) -> bool:
    return _clean_inline_text(left).casefold() == _clean_inline_text(right).casefold()


def _message_date(message: EmailMessage | Message) -> str | None:
    raw_date = _decode_header_value(message.get("Date"))
    if not raw_date:
        return None
    try:
        return format_datetime(parsedate_to_datetime(raw_date))
    except (TypeError, ValueError, IndexError, OverflowError):
        return raw_date


def _decode_header_value(value: object) -> str | None:
    if value is None:
        return None
    try:
        return str(make_header(decode_header(str(value)))).strip()
    except (LookupError, UnicodeDecodeError, ValueError):
        return str(value).strip()


def _clean_message_id(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip()
    if cleaned.startswith("<") and cleaned.endswith(">"):
        cleaned = cleaned[1:-1].strip()
    return cleaned or None


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise FeedFetchError(f"{name} is not set")
    return value


def _imap_port(name: str) -> int:
    value = os.environ.get(name, "").strip()
    if not value:
        return 993
    if not value.isdigit():
        raise FeedFetchError(f"{name} must be an integer IMAP port")
    return int(value)


def _max_uid(uids: Iterable[str], fallback: str | None) -> str | None:
    parsed: list[int] = []
    for uid in uids:
        if isinstance(uid, str) and uid.isdigit():
            parsed.append(int(uid))
    if parsed:
        return str(max(parsed))
    return fallback

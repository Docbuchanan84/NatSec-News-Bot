from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_REPO = Path(__file__).resolve().parents[1]
DEFAULT_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "natsec-x-draft.txt"
MAX_BODY_BYTES = 2_000_000
PROMPT_VERSION = "natsec-x-draft-v1"
PRICING_VERSION = "2026-07-08"
WEB_SEARCH_COST_USD = 0.01
METRICS_LOCK = threading.Lock()


@dataclass(frozen=True)
class DraftProfile:
    name: str
    model: str
    reasoning_effort: str
    search_context_size: str
    max_tool_calls: int
    max_image_results: int
    input_usd_per_million: float
    cached_input_usd_per_million: float
    output_usd_per_million: float


@dataclass(frozen=True)
class WorkerResult:
    result: str
    meta: dict[str, Any]


def build_profiles() -> dict[str, DraftProfile]:
    return {
        "fast": DraftProfile(
            name="fast",
            model=os.environ.get("OPENAI_DRAFT_FAST_MODEL", "gpt-5.4-mini-2026-03-17"),
            reasoning_effort="low",
            search_context_size="low",
            max_tool_calls=3,
            max_image_results=2,
            input_usd_per_million=0.75,
            cached_input_usd_per_million=0.075,
            output_usd_per_million=4.50,
        ),
        "quality": DraftProfile(
            name="quality",
            model=os.environ.get("OPENAI_DRAFT_QUALITY_MODEL", "gpt-5.5-2026-04-23"),
            reasoning_effort="low",
            search_context_size="medium",
            max_tool_calls=5,
            max_image_results=4,
            input_usd_per_million=5.00,
            cached_input_usd_per_million=0.50,
            output_usd_per_million=30.00,
        ),
    }


DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["tweet", "evidence", "media", "notes"],
    "properties": {
        "tweet": {"type": "string", "minLength": 1, "maxLength": 500},
        "evidence": {
            "type": "array",
            "minItems": 1,
            "maxItems": 6,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["source", "url", "verifies"],
                "properties": {
                    "source": {"type": "string"},
                    "url": {"type": "string"},
                    "verifies": {"type": "string"},
                },
            },
        },
        "media": {
            "type": "object",
            "additionalProperties": False,
            "required": ["recommended", "alt_text", "other_options"],
            "properties": {
                "recommended": {
                    "anyOf": [
                        {"type": "null"},
                        {"$ref": "#/$defs/mediaOption"},
                    ]
                },
                "alt_text": {"type": ["string", "null"]},
                "other_options": {
                    "type": "array",
                    "maxItems": 3,
                    "items": {"$ref": "#/$defs/mediaOption"},
                },
            },
        },
        "notes": {
            "type": "array",
            "maxItems": 6,
            "items": {"type": "string"},
        },
    },
    "$defs": {
        "mediaOption": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "type",
                "attribution",
                "rights_label",
                "media_url",
                "source_url",
            ],
            "properties": {
                "type": {"type": "string", "enum": ["image", "video"]},
                "attribution": {"type": "string"},
                "rights_label": {
                    "type": "string",
                    "enum": [
                        "safe",
                        "likely usable with attribution",
                        "preview only",
                        "avoid reposting",
                    ],
                },
                "media_url": {"type": ["string", "null"]},
                "source_url": {"type": ["string", "null"]},
            },
        }
    },
}


class DraftWorker(BaseHTTPRequestHandler):
    server: "DraftServer"

    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_error(404)
            return
        self.send_json(self.server.health_payload())

    def do_POST(self) -> None:
        if self.path not in {"/draft", "/importance-review"}:
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0 or length > MAX_BODY_BYTES:
            self.send_error(413, "invalid body size")
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            mode = "importance" if self.path == "/importance-review" else "draft"
            result = self.server.run(payload, mode=mode)
        except Exception as exc:
            self.send_json({"ok": False, "error": safe_error(exc)}, status=500)
            return
        self.send_json({"ok": True, "result": result.result, "meta": result.meta})

    def log_message(self, format: str, *args: object) -> None:
        if not self.server.quiet:
            super().log_message(format, *args)

    def send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class DraftServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        handler: type[DraftWorker],
        *,
        codex: str,
        repo: Path,
        codex_timeout: int,
        openai_timeout: int,
        quiet: bool,
    ) -> None:
        super().__init__(server_address, handler)
        self.codex = codex
        self.repo = repo
        self.codex_timeout = codex_timeout
        self.openai_timeout = openai_timeout
        self.quiet = quiet
        self.backend = normalized_backend(os.environ.get("DRAFT_BACKEND", "openai"))
        self.profiles = build_profiles()
        self.prompt_path = Path(os.environ.get("OPENAI_DRAFT_PROMPT_PATH", DEFAULT_PROMPT_PATH))
        self.metrics_path = Path(
            os.environ.get("DRAFT_METRICS_PATH", self.repo / "logs" / "openai-draft-metrics.jsonl")
        )

    def health_payload(self) -> dict[str, Any]:
        return {
            "ok": True,
            "service": "rss-draft-worker",
            "draft_backend": self.backend,
            "draft_profiles": sorted(self.profiles),
            "importance_backend": "codex",
            "openai_configured": bool(os.environ.get("OPENAI_API_KEY")),
            "openai_sdk_available": importlib.util.find_spec("openai") is not None,
            "prompt_version": PROMPT_VERSION,
        }

    def run(self, payload: dict[str, Any], *, mode: str) -> WorkerResult:
        if mode == "importance":
            return self.run_codex(payload, mode="importance")
        if self.backend == "codex":
            return self.run_codex(payload, mode="draft")
        return self.run_openai(payload)

    def run_openai(self, payload: dict[str, Any]) -> WorkerResult:
        request_id = uuid.uuid4().hex
        started = time.monotonic()
        request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
        profile_name = str(request.get("profile") or "fast").strip().casefold()
        profile = self.profiles.get(profile_name)
        if profile is None:
            raise ValueError(f"unknown draft profile: {profile_name}")
        article = payload.get("article") if isinstance(payload.get("article"), dict) else {}
        article_id = article.get("id")
        tweet_only = bool(request.get("tweet_only"))
        metric = base_metric(
            request_id=request_id,
            article_id=article_id,
            profile=profile,
            backend="openai",
        )
        response_dump: dict[str, Any] | None = None
        try:
            api_key = os.environ.get("OPENAI_API_KEY", "").strip()
            if not api_key:
                raise RuntimeError("OPENAI_API_KEY is not configured")
            instructions = self.prompt_path.read_text(encoding="utf-8").strip()
            if not instructions:
                raise RuntimeError("draft prompt is empty")

            from openai import OpenAI

            client = OpenAI(
                api_key=api_key,
                timeout=self.openai_timeout,
                max_retries=1,
            )
            response = client.responses.create(
                model=profile.model,
                instructions=instructions,
                input=build_openai_input(payload),
                reasoning={"effort": profile.reasoning_effort},
                tools=[
                    {
                        "type": "web_search",
                        "search_context_size": profile.search_context_size,
                        "search_content_types": ["text", "image"],
                        "image_settings": {
                            "max_results": profile.max_image_results,
                            "caption": True,
                        },
                    }
                ],
                tool_choice="required",
                max_tool_calls=profile.max_tool_calls,
                include=[
                    "web_search_call.action.sources",
                    "web_search_call.results",
                ],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "natsec_x_draft",
                        "strict": True,
                        "schema": DRAFT_SCHEMA,
                    },
                    "verbosity": "low",
                },
                max_output_tokens=3000,
                prompt_cache_key=PROMPT_VERSION,
                store=False,
                metadata={
                    "workflow": "natsec_x_draft",
                    "profile": profile.name,
                    "article_id": str(article_id or "unknown"),
                    "request_id": request_id,
                },
            )
            response_dump = response.model_dump(mode="json")
            parsed = json.loads(response.output_text)
            sources = response_sources(response_dump)
            media_urls = response_media_urls(response_dump)
            validate_draft(parsed, payload, sources=sources, media_urls=media_urls)

            elapsed_ms = int((time.monotonic() - started) * 1000)
            usage = usage_values(response_dump.get("usage"))
            web_search_calls = count_web_search_calls(response_dump)
            estimated_cost = estimate_cost(profile, usage, web_search_calls)
            metric.update(
                {
                    "status": "ok",
                    "elapsed_ms": elapsed_ms,
                    "response_id": response_dump.get("id"),
                    **usage,
                    "web_search_calls": web_search_calls,
                    "estimated_cost_usd": estimated_cost,
                }
            )
            append_metric(self.metrics_path, metric)
            meta = public_meta(metric)
            result = render_draft(
                parsed,
                tweet_only=tweet_only,
                metrics_line=render_metrics_line(profile, metric),
            )
            return WorkerResult(result=result, meta=meta)
        except Exception as exc:
            if response_dump is not None:
                usage = usage_values(response_dump.get("usage"))
                web_search_calls = count_web_search_calls(response_dump)
                metric.update(
                    {
                        "response_id": response_dump.get("id"),
                        **usage,
                        "web_search_calls": web_search_calls,
                        "estimated_cost_usd": estimate_cost(profile, usage, web_search_calls),
                    }
                )
            metric.update(
                {
                    "status": "error",
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                    "error_category": error_category(exc),
                }
            )
            append_metric(self.metrics_path, metric)
            raise RuntimeError(
                f"OpenAI draft failed ({metric['error_category']}): {safe_error(exc)}"
            ) from exc

    def run_codex(self, payload: dict[str, Any], *, mode: str) -> WorkerResult:
        started = time.monotonic()
        prompt = build_importance_prompt(payload) if mode == "importance" else build_codex_prompt(payload)
        with tempfile.TemporaryDirectory(prefix="natsec-codex-draft-") as temp_dir:
            output_path = Path(temp_dir) / "draft.md"
            command = [
                self.codex,
                "exec",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--cd",
                str(self.repo),
                "-c",
                'model_reasoning_effort="medium"',
                "-o",
                str(output_path),
                "-",
            ]
            env = os.environ.copy()
            env.setdefault("CODEX_HOME", str(Path.home() / ".codex"))
            completed = subprocess.run(
                command,
                input=prompt,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=self.codex_timeout,
                env=env,
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "").strip()
                raise RuntimeError(f"codex exec failed with code {completed.returncode}: {detail[:1800]}")
            if output_path.exists():
                result = output_path.read_text(encoding="utf-8").strip()
            else:
                result = completed.stdout.strip()
            if not result:
                raise RuntimeError("codex exec returned an empty draft")
        return WorkerResult(
            result=result,
            meta={
                "backend": "codex",
                "mode": mode,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            },
        )


def build_openai_input(payload: dict[str, Any]) -> str:
    compact_payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    tweet_only = bool((payload.get("request") or {}).get("tweet_only"))
    mode = "Return a researched tweet only; still populate the structured evidence and media fields." if tweet_only else (
        "Return a complete researched draft pack."
    )
    return f"{mode}\n<rss_bot_payload>\n{compact_payload}\n</rss_bot_payload>"


def build_codex_prompt(payload: dict[str, Any]) -> str:
    compact_payload = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    return (
        "Use $natsec-x-draft to process this Discord bot draft request.\n"
        "Return Draft Pack Mode output only unless the payload request explicitly asks for tweet-only output.\n"
        "Keep the response concise enough to post back into Discord. Include media options with attribution, rights labels, and alt text.\n"
        "Use the provided bot payload as local context, then verify current sources on the internet before drafting.\n\n"
        "<rss_bot_payload>\n"
        f"{compact_payload}\n"
        "</rss_bot_payload>\n"
    )


def build_importance_prompt(payload: dict[str, Any]) -> str:
    compact_payload = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    return (
        "You are reviewing a local RSS bot importance watchlist for national-security news.\n"
        "Use current global, defense, and NatSec context, plus the provided recent bot articles.\n"
        "Return ONLY valid JSON. Do not include markdown or prose outside the JSON object.\n"
        "Schema: {\"suggestions\":[{\"action\":\"add|update|disable\",\"term\":\"short word or phrase\","
        "\"weight\":integer_between_-50_and_50,\"category\":\"short_label\",\"expires_at\":\"ISO-8601 UTC timestamp\","
        "\"notes\":\"short operator note\",\"rationale\":\"why this term matters now\"}]}.\n"
        "Prefer short-lived specific entities, places, operations, weapons, ships, leaders, and crisis labels.\n"
        "Do not suggest generic permanent terms unless the payload clearly proves an existing watch term should be disabled.\n"
        "Use positive weights for breaking/high-value terms and negative weights for recurring low-value/noise terms.\n"
        "Set expirations for additions and updates, usually 24-72 hours. Keep suggestions sparse and high-confidence.\n\n"
        "<rss_bot_importance_payload>\n"
        f"{compact_payload}\n"
        "</rss_bot_importance_payload>\n"
    )


def validate_draft(
    draft: dict[str, Any],
    payload: dict[str, Any],
    *,
    sources: set[str],
    media_urls: set[str],
) -> None:
    if not isinstance(draft, dict):
        raise ValueError("structured draft is not an object")
    tweet = " ".join(str(draft.get("tweet") or "").split())
    if not tweet:
        raise ValueError("structured draft has no tweet")
    if len(tweet) > 500:
        raise ValueError(f"tweet exceeds 500 characters ({len(tweet)})")
    draft["tweet"] = tweet

    allowed_sources = {normalize_url(url) for url in payload_source_urls(payload) | sources}
    evidence = draft.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("structured draft has no evidence")
    for item in evidence:
        if not isinstance(item, dict):
            raise ValueError("evidence item is not an object")
        url = normalize_url(str(item.get("url") or ""))
        if not url or url not in allowed_sources:
            allowed_hosts = sorted({urlsplit(candidate).netloc for candidate in allowed_sources if candidate})
            raise ValueError(
                f"evidence URL was not present in payload or web-search sources: {url or '[missing]'} "
                f"(allowed hosts: {', '.join(allowed_hosts[:12]) or 'none'})"
            )

    allowed_media = {normalize_url(url) for url in payload_media_urls(payload) | media_urls}
    media = draft.get("media")
    if not isinstance(media, dict):
        raise ValueError("structured draft has no media object")
    options = []
    if isinstance(media.get("recommended"), dict):
        options.append(media["recommended"])
    if isinstance(media.get("other_options"), list):
        options.extend(item for item in media["other_options"] if isinstance(item, dict))
    for option in options:
        media_url = option.get("media_url")
        if media_url and normalize_url(str(media_url)) not in allowed_media:
            raise ValueError("media URL was not present in payload or image-search results")
        source_url = option.get("source_url")
        if source_url and normalize_url(str(source_url)) not in allowed_sources:
            raise ValueError("media source URL was not present in payload or web-search sources")


def render_draft(draft: dict[str, Any], *, tweet_only: bool, metrics_line: str) -> str:
    if tweet_only:
        return str(draft["tweet"]).strip()
    evidence_lines = [
        f"- {item['source']}: {item['url']} — {item['verifies']}"
        for item in draft["evidence"]
    ]
    media = draft["media"]
    media_lines: list[str] = []
    recommended = media.get("recommended")
    if isinstance(recommended, dict):
        media_lines.append("- Recommended: " + render_media_option(recommended))
    else:
        media_lines.append("- Recommended: No supportable reusable media located.")
    if media.get("alt_text"):
        media_lines.append(f"- Alt text: {media['alt_text']}")
    other_options = media.get("other_options") or []
    if other_options:
        media_lines.append(
            "- Other options: " + "; ".join(render_media_option(item) for item in other_options)
        )
    note_lines = [f"- {note}" for note in draft.get("notes") or []]
    note_lines.append(f"- {metrics_line}")
    return "\n\n".join(
        [
            f"Tweet:\n{draft['tweet']}",
            "Evidence:\n" + "\n".join(evidence_lines),
            "Media:\n" + "\n".join(media_lines),
            "Notes:\n" + "\n".join(note_lines),
        ]
    )


def render_media_option(option: dict[str, Any]) -> str:
    url = option.get("media_url") or option.get("source_url") or "no URL"
    return (
        f"{option.get('type', 'image')}, {option.get('attribution', 'Attribution required')}, "
        f"{option.get('rights_label', 'avoid reposting')}, {url}"
    )


def response_sources(response: dict[str, Any]) -> set[str]:
    urls: set[str] = set()
    for item in response.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "web_search_call":
            urls.update(urls_in_object(item))
        if item.get("type") == "message":
            for content in item.get("content") or []:
                if not isinstance(content, dict):
                    continue
                for annotation in content.get("annotations") or []:
                    if isinstance(annotation, dict):
                        url = annotation.get("url") or (annotation.get("url_citation") or {}).get("url")
                        if is_http_url(url):
                            urls.add(str(url))
    return urls


def response_media_urls(response: dict[str, Any]) -> set[str]:
    urls: set[str] = set()
    for item in response.get("output") or []:
        if isinstance(item, dict) and item.get("type") == "web_search_call":
            collect_media_urls(item, urls)
    return urls


def urls_in_object(value: Any) -> set[str]:
    output: set[str] = set()
    if isinstance(value, dict):
        for child in value.values():
            output.update(urls_in_object(child))
    elif isinstance(value, list):
        for child in value:
            output.update(urls_in_object(child))
    elif is_http_url(value):
        output.add(str(value))
    return output


def collect_media_urls(value: Any, output: set[str], *, parent_key: str = "") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            collect_media_urls(child, output, parent_key=str(key).casefold())
    elif isinstance(value, list):
        for child in value:
            collect_media_urls(child, output, parent_key=parent_key)
    elif is_http_url(value) and any(token in parent_key for token in ("image", "thumbnail", "media")):
        output.add(str(value))


def payload_source_urls(payload: dict[str, Any]) -> set[str]:
    urls: set[str] = set()
    article = payload.get("article")
    if isinstance(article, dict) and is_http_url(article.get("url")):
        urls.add(str(article["url"]))
    for related in payload.get("related_articles") or []:
        if isinstance(related, dict) and is_http_url(related.get("url")):
            urls.add(str(related["url"]))
    for option in payload.get("media_options") or []:
        if not isinstance(option, dict):
            continue
        for key in ("source_url", "media_url"):
            if is_http_url(option.get(key)):
                urls.add(str(option[key]))
    return urls


def payload_media_urls(payload: dict[str, Any]) -> set[str]:
    urls: set[str] = set()
    article = payload.get("article")
    if isinstance(article, dict):
        for key in ("image_url", "video_url"):
            if is_http_url(article.get(key)):
                urls.add(str(article[key]))
    for option in payload.get("media_options") or []:
        if isinstance(option, dict) and is_http_url(option.get("media_url")):
            urls.add(str(option["media_url"]))
    return urls


def count_web_search_calls(response: dict[str, Any]) -> int:
    return sum(
        1
        for item in response.get("output") or []
        if isinstance(item, dict) and item.get("type") == "web_search_call"
    )


def usage_values(raw: Any) -> dict[str, int]:
    usage = raw if isinstance(raw, dict) else {}
    input_details = usage.get("input_tokens_details") if isinstance(usage.get("input_tokens_details"), dict) else {}
    output_details = usage.get("output_tokens_details") if isinstance(usage.get("output_tokens_details"), dict) else {}
    return {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "cached_input_tokens": int(input_details.get("cached_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "reasoning_tokens": int(output_details.get("reasoning_tokens") or 0),
    }


def estimate_cost(profile: DraftProfile, usage: dict[str, int], web_search_calls: int) -> float:
    cached = min(usage["cached_input_tokens"], usage["input_tokens"])
    uncached = max(0, usage["input_tokens"] - cached)
    model_cost = (
        uncached * profile.input_usd_per_million
        + cached * profile.cached_input_usd_per_million
        + usage["output_tokens"] * profile.output_usd_per_million
    ) / 1_000_000
    return round(model_cost + web_search_calls * WEB_SEARCH_COST_USD, 6)


def base_metric(
    *,
    request_id: str,
    article_id: Any,
    profile: DraftProfile,
    backend: str,
) -> dict[str, Any]:
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "request_id": request_id,
        "article_id": article_id,
        "backend": backend,
        "profile": profile.name,
        "model": profile.model,
        "reasoning_effort": profile.reasoning_effort,
        "prompt_version": PROMPT_VERSION,
        "pricing_version": PRICING_VERSION,
    }


def append_metric(path: Path, metric: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(metric, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    with METRICS_LOCK:
        with path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")


def public_meta(metric: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "request_id",
        "backend",
        "profile",
        "model",
        "elapsed_ms",
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "web_search_calls",
        "estimated_cost_usd",
    )
    return {key: metric.get(key) for key in keys}


def render_metrics_line(profile: DraftProfile, metric: dict[str, Any]) -> str:
    total_tokens = int(metric.get("input_tokens") or 0) + int(metric.get("output_tokens") or 0)
    elapsed = float(metric.get("elapsed_ms") or 0) / 1000
    return (
        f"Run: {profile.name} · {profile.model} · {elapsed:.1f}s · "
        f"{metric.get('web_search_calls', 0)} searches · {total_tokens:,} tokens · "
        f"est. ${float(metric.get('estimated_cost_usd') or 0):.4f}"
    )


def normalize_url(value: str) -> str:
    if not is_http_url(value):
        return ""
    split = urlsplit(value.strip())
    filtered_query = urlencode(
        [
            (key, val)
            for key, val in parse_qsl(split.query, keep_blank_values=True)
            if not key.casefold().startswith("utm_")
        ]
    )
    path = split.path.rstrip("/") or "/"
    return urlunsplit((split.scheme.casefold(), split.netloc.casefold(), path, filtered_query, ""))


def is_http_url(value: Any) -> bool:
    return isinstance(value, str) and value.strip().casefold().startswith(("http://", "https://"))


def normalized_backend(value: str) -> str:
    backend = value.strip().casefold()
    if backend not in {"openai", "codex"}:
        raise ValueError(f"unsupported DRAFT_BACKEND: {backend}")
    return backend


def safe_error(exc: Exception) -> str:
    text = " ".join(str(exc).split())
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if api_key:
        text = text.replace(api_key, "[redacted]")
    return text[:1800]


def error_category(exc: Exception) -> str:
    name = type(exc).__name__.casefold()
    text = str(exc).casefold()
    if "authentication" in name or "401" in text or "api key" in text:
        return "authentication"
    if "ratelimit" in name or "429" in text:
        return "rate_limit"
    if "timeout" in name or "timed out" in text:
        return "timeout"
    if "connection" in name:
        return "connection"
    if "json" in name or "structured" in text or "evidence url" in text or "media url" in text:
        return "validation"
    return "api_error"


def load_worker_env(repo: Path) -> None:
    path = repo / ".env.openai"
    if not path.exists():
        return
    try:
        from dotenv import dotenv_values
    except ImportError:
        return
    for key, value in dotenv_values(path).items():
        if key and value is not None:
            os.environ.setdefault(key, value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Host-side draft worker for NatSec News Discord requests.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--codex", default=os.environ.get("CODEX_CLI") or resolve_codex_command())
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args()


def resolve_codex_command() -> str:
    for candidate in ("codex.cmd", "codex.exe", "codex"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return "codex"


def main() -> int:
    args = parse_args()
    repo = args.repo.resolve()
    load_worker_env(repo)
    server = DraftServer(
        (args.host, args.port),
        DraftWorker,
        codex=args.codex,
        repo=repo,
        codex_timeout=int(os.environ.get("CODEX_DRAFT_TIMEOUT_SECONDS", "900")),
        openai_timeout=int(os.environ.get("OPENAI_DRAFT_TIMEOUT_SECONDS", "90")),
        quiet=args.quiet,
    )
    print(
        f"Draft worker listening on http://{args.host}:{args.port}/draft "
        f"(draft={server.backend}, importance=codex)",
        flush=True,
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

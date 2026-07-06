from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_REPO = Path(__file__).resolve().parents[1]
MAX_BODY_BYTES = 2_000_000


class DraftWorker(BaseHTTPRequestHandler):
    server: "DraftServer"

    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_error(404)
            return
        self.send_json({"ok": True})

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
            result = self.server.run_codex(payload, mode="importance" if self.path == "/importance-review" else "draft")
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=500)
            return
        self.send_json({"ok": True, "result": result})

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
    def __init__(self, server_address: tuple[str, int], handler, *, codex: str, repo: Path, timeout: int, quiet: bool) -> None:
        super().__init__(server_address, handler)
        self.codex = codex
        self.repo = repo
        self.timeout = timeout
        self.quiet = quiet

    def run_codex(self, payload: dict[str, Any], *, mode: str = "draft") -> str:
        prompt = build_importance_prompt(payload) if mode == "importance" else build_prompt(payload)
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
                timeout=self.timeout,
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
            return result


def build_prompt(payload: dict[str, Any]) -> str:
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Host-side Codex worker for NatSec News Discord draft requests.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--codex", default=os.environ.get("CODEX_CLI") or resolve_codex_command())
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("CODEX_DRAFT_TIMEOUT_SECONDS", "900")))
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
    server = DraftServer((args.host, args.port), DraftWorker, codex=args.codex, repo=args.repo, timeout=args.timeout, quiet=args.quiet)
    print(
        f"Codex draft worker listening on http://{args.host}:{args.port}/draft and /importance-review",
        flush=True,
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

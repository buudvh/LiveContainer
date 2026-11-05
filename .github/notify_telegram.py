#!/usr/bin/env python3
"""VieStore: post CI build results to a Telegram group.

Copy of buudvh/VieStore scripts/ci/notify_telegram.py, with NOTIFY_* overrides so the
same script serves the LiveContainer+VieStore build.

Usage:
    python3 .github/notify_telegram.py success <ipa_path>
    python3 .github/notify_telegram.py failure

Environment:
    TELEGRAM_BOT_TOKEN   bot token from @BotFather (GitHub secret)
    TELEGRAM_CHAT_ID     target group chat ID, e.g. -1001234567890 (GitHub secret)
    TELEGRAM_THREAD_ID   optional topic ID for groups with Topics enabled (GitHub variable)
    NOTIFY_TITLE         message title (default "VieStore <RELEASE_NAME>")
    NOTIFY_ASSET_NAME    release asset the download link points to (default SideStore.ipa)
    NOTIFY_RELEASE_TAG   release tag for the links (default CHANNEL, then "nightly")
    MARKETING_VERSION, SHORT_COMMIT, CHANNEL, RELEASE_NAME   optional, set earlier in the workflow
    GITHUB_SERVER_URL, GITHUB_REPOSITORY, GITHUB_RUN_ID, GITHUB_REF_NAME, GITHUB_SHA   set by Actions

Never exits non-zero for delivery problems: a failed notification must not fail the build.
"""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

API_BASE = "https://api.telegram.org"
# Bot API limit for files sent by the bot itself (sendDocument with multipart upload).
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_CAPTION_CHARS = 1024
TIMEOUT_SECONDS = 300


def env(name, default=""):
    return os.environ.get(name, default).strip()


def commit_subject():
    try:
        return subprocess.run(
            ["git", "log", "-1", "--format=%s"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def links():
    server = env("GITHUB_SERVER_URL", "https://github.com")
    repo = env("GITHUB_REPOSITORY")
    run_url = f"{server}/{repo}/actions/runs/{env('GITHUB_RUN_ID')}"
    tag = env("NOTIFY_RELEASE_TAG") or env("CHANNEL", "nightly")
    release_url = f"{server}/{repo}/releases/tag/{tag}"
    ipa_url = f"{server}/{repo}/releases/download/{tag}/{urllib.parse.quote(asset_name())}"
    return run_url, release_url, ipa_url


def asset_name():
    return env("NOTIFY_ASSET_NAME") or "SideStore.ipa"


def build_text(succeeded):
    run_url, release_url, ipa_url = links()
    title = env("NOTIFY_TITLE") or f"VieStore {env('RELEASE_NAME', 'Nightly')}"
    version = env("MARKETING_VERSION")
    commit = env("SHORT_COMMIT") or env("GITHUB_SHA")[:7]
    subject = commit_subject()

    status = "✅ build OK" if succeeded else "❌ build FAILED"
    lines = [f"{title} — {status}"]
    if version:
        lines.append(f"Version: {version}")
    lines.append(f"Branch: {env('GITHUB_REF_NAME')} @ {commit}")
    if subject:
        lines.append(f"Commit: {subject}")
    if succeeded:
        lines.append(f"IPA: {ipa_url}")
        lines.append(f"Release: {release_url}")
    lines.append(f"Run: {run_url}")

    # Plain text (no parse_mode) so commit subjects never need escaping.
    text = "\n".join(lines)
    return text[:MAX_CAPTION_CHARS]


def common_fields():
    fields = {"chat_id": env("TELEGRAM_CHAT_ID")}
    thread_id = env("TELEGRAM_THREAD_ID")
    if thread_id:
        fields["message_thread_id"] = thread_id
    return fields


def post(method, body, content_type):
    url = f"{API_BASE}/bot{env('TELEGRAM_BOT_TOKEN')}/{method}"
    request = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": content_type})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as error:
        # Telegram explains the problem in the body (wrong chat ID, bot not in group, ...).
        detail = error.read().decode("utf-8", "replace")
        print(f"Telegram {method} failed: HTTP {error.code} {detail}", file=sys.stderr)
        return False
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        print(f"Telegram {method} failed: {error}", file=sys.stderr)
        return False

    if not payload.get("ok"):
        print(f"Telegram {method} returned an error: {payload}", file=sys.stderr)
        return False
    print(f"Telegram {method} delivered.", file=sys.stderr)
    return True


def send_message(text):
    fields = common_fields()
    fields["text"] = text
    fields["disable_web_page_preview"] = "true"
    body = urllib.parse.urlencode(fields).encode("utf-8")
    return post("sendMessage", body, "application/x-www-form-urlencoded")


def send_document(path, caption):
    boundary = f"----VieStore{uuid.uuid4().hex}"
    parts = []
    fields = common_fields()
    fields["caption"] = caption
    for key, value in fields.items():
        parts.append(
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"{key}\"\r\n\r\n"
            f"{value}\r\n".encode("utf-8")
        )
    parts.append(
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"document\"; filename=\"{path.name}\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n".encode("utf-8")
    )
    parts.append(path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return post("sendDocument", b"".join(parts), f"multipart/form-data; boundary={boundary}")


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("success", "failure"):
        print(__doc__, file=sys.stderr)
        return

    if not env("TELEGRAM_BOT_TOKEN") or not env("TELEGRAM_CHAT_ID"):
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; skipping Telegram notification.", file=sys.stderr)
        return

    succeeded = sys.argv[1] == "success"
    text = build_text(succeeded)

    if not succeeded:
        send_message(text)
        return

    ipa = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(asset_name())
    if not ipa.is_file():
        print(f"{ipa} not found; sending the message without the file.", file=sys.stderr)
        send_message(text)
        return

    size = ipa.stat().st_size
    if size > MAX_UPLOAD_BYTES:
        print(f"{ipa} is {size} bytes (> {MAX_UPLOAD_BYTES}); sending the link only.", file=sys.stderr)
        send_message(text)
        return

    if not send_document(ipa, text):
        # e.g. upload timeout: still tell the group, with the release link.
        send_message(text)


if __name__ == "__main__":
    main()

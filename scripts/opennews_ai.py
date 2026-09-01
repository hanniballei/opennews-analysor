"""Shared DeepSeek client and state helpers for OpenNews enrichment jobs."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar


DEFAULT_API_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_BATCH_SIZE = 20
DEFAULT_MAX_BATCH_CHARACTERS = 30_000
DEFAULT_MAX_TOKENS = 32_768
DEFAULT_REQUEST_ATTEMPTS = 3

Candidate = TypeVar("Candidate")
Result = TypeVar("Result")


class TruncatedResponseError(RuntimeError):
    pass


class RetryableDeepSeekError(RuntimeError):
    pass


@dataclass(frozen=True)
class AiSettings:
    api_key: str
    api_url: str
    model: str
    data_dir: Path


def load_settings(args: argparse.Namespace) -> AiSettings:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is required")
    api_base_url = os.environ.get(
        "DEEPSEEK_API_BASE_URL",
        args.api_base_url,
    ).rstrip("/")
    model = os.environ.get("DEEPSEEK_MODEL", args.model).strip()
    if not model:
        raise RuntimeError("DEEPSEEK_MODEL must not be empty")
    data_dir = Path(os.environ.get("OPENNEWS_DATA_DIR", args.data_dir)).expanduser()
    return AiSettings(
        api_key=api_key,
        api_url=f"{api_base_url}/chat/completions",
        model=model,
        data_dir=data_dir,
    )


def iso_utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def canonical_utc_timestamp(value: Any, source: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"Missing UTC timestamp in {source}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(f"Invalid UTC timestamp in {source}: {value}") from exc
    if parsed.tzinfo is None:
        raise RuntimeError(f"UTC timestamp in {source} has no timezone: {value}")
    canonical = (
        parsed.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    if value != canonical:
        raise RuntimeError(f"Non-canonical UTC timestamp in {source}: {value}")
    return canonical


def clean_text(value: str) -> str:
    text = html.unescape(value)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temporary_path = Path(handle.name)
    os.replace(temporary_path, path)


def load_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise RuntimeError(f"Expected a JSON object in {path}")
    return payload


def positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def chunk_by_text(
    candidates: list[Candidate],
    text: Callable[[Candidate], str],
    batch_size: int,
    max_batch_characters: int,
) -> Iterator[list[Candidate]]:
    batch: list[Candidate] = []
    character_count = 0
    for candidate in candidates:
        candidate_characters = len(text(candidate))
        if batch and (
            len(batch) >= batch_size
            or character_count + candidate_characters > max_batch_characters
        ):
            yield batch
            batch = []
            character_count = 0
        batch.append(candidate)
        character_count += candidate_characters
    if batch:
        yield batch


def chat_request(
    model: str,
    system_prompt: str,
    items: list[dict[str, Any]],
    max_tokens: int,
) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps({"items": items}, ensure_ascii=False),
            },
        ],
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "stream": False,
    }


def post_json(
    url: str,
    api_key: str,
    body: dict[str, Any],
    timeout: int,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "opennews-enricher/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        error_type = (
            RetryableDeepSeekError
            if exc.code == 429 or exc.code >= 500
            else RuntimeError
        )
        raise error_type(
            f"DeepSeek API returned HTTP {exc.code}: {response_body}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RetryableDeepSeekError(f"DeepSeek API request failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RetryableDeepSeekError("DeepSeek API returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RetryableDeepSeekError("DeepSeek API returned a non-object response")
    return payload


def response_content(response: dict[str, Any]) -> str:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("DeepSeek response did not contain choices")
    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        raise RuntimeError("DeepSeek response contained an invalid choice")
    if first_choice.get("finish_reason") == "length":
        raise TruncatedResponseError("DeepSeek response reached the max_tokens limit")
    message = first_choice.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise RuntimeError("DeepSeek response did not contain message content")
    content = message["content"].strip()
    if content.startswith("```json") and content.endswith("```"):
        content = content[7:-3].strip()
    elif content.startswith("```") and content.endswith("```"):
        content = content[3:-3].strip()
    return content


def request_with_retries(
    api_url: str,
    api_key: str,
    body: dict[str, Any],
    timeout: int,
    parse: Callable[[dict[str, Any]], Result],
    post: Callable[[str, str, dict[str, Any], int], dict[str, Any]] = post_json,
    max_attempts: int = DEFAULT_REQUEST_ATTEMPTS,
) -> Result:
    last_error: RuntimeError | None = None
    for _ in range(max_attempts):
        try:
            response = post(api_url, api_key, body, timeout)
        except RetryableDeepSeekError as exc:
            last_error = exc
            continue
        try:
            return parse(response)
        except TruncatedResponseError:
            raise
        except RuntimeError as exc:
            last_error = exc
    raise RuntimeError(
        f"DeepSeek request failed after {max_attempts} attempts: {last_error}"
    ) from last_error


def split_truncated_batches(
    candidates: list[Candidate],
    request: Callable[[list[Candidate]], Result],
) -> Iterator[tuple[list[Candidate], Result]]:
    try:
        result = request(candidates)
    except TruncatedResponseError:
        if len(candidates) == 1:
            raise
        midpoint = len(candidates) // 2
        yield from split_truncated_batches(candidates[:midpoint], request)
        yield from split_truncated_batches(candidates[midpoint:], request)
        return
    yield candidates, result

"""Explicit Chat Completions configuration and credential-safe HTTP transport.

The transport returns bytes. Call receipts must persist those bytes before parsing
model content; no redirect, protocol probing or automatic retry is performed.
"""
from __future__ import annotations

import base64
import json
import math
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


class ApiError(RuntimeError):
    """A safe error whose message never contains provider error bodies or credentials."""


@dataclass(frozen=True)
class ProviderConfig:
    base_url: str
    model: str
    api_key_env: str = "VIDEO_NOTES_API_KEY"
    api_key: str | None = field(default=None, repr=False, compare=False)
    token_parameter: str = "max_completion_tokens"
    json_mode: bool = True
    supports_images: bool = True
    timeout_seconds: float = 90
    max_response_bytes: int = 2_000_000
    max_image_bytes: int = 5_000_000
    max_request_bytes: int = 16_000_000

    def __post_init__(self):
        if not isinstance(self.base_url, str) or any(ord(c) < 33 for c in self.base_url) or "\\" in self.base_url:
            raise ValueError("base_url must be an HTTP(S) base URL without credentials or query parameters")
        parts = urlsplit(self.base_url)
        try:
            port = parts.port
        except ValueError:
            raise ValueError("Invalid base_url port") from None
        if (parts.scheme not in ("https", "http") or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.query or parts.fragment):
            raise ValueError("base_url must be HTTP(S), without credentials, query parameters or fragments")
        if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("Plain HTTP is only supported for a local loopback model endpoint")
        if not isinstance(self.model, str) or not self.model.strip() or len(self.model) > 200 or any(ord(c) < 32 for c in self.model):
            raise ValueError("A nonempty model identifier is required")
        if not isinstance(self.api_key_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.api_key_env):
            raise ValueError("api_key_env must name an environment variable")
        if self.api_key is not None and (not isinstance(self.api_key, str) or not self.api_key.strip()
                                        or any(ord(c) < 32 for c in self.api_key)):
            raise ValueError("API key must be nonempty text without control characters")
        if self.token_parameter not in ("max_tokens", "max_completion_tokens"):
            raise ValueError("token_parameter must be max_tokens or max_completion_tokens")
        if not isinstance(self.json_mode, bool) or not isinstance(self.supports_images, bool):
            raise ValueError("json_mode and supports_images must be booleans")
        if (isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float))
                or not math.isfinite(self.timeout_seconds) or not 0 < self.timeout_seconds <= 600):
            raise ValueError("timeout_seconds must be between 0 and 600")
        for field_name in ("max_response_bytes", "max_image_bytes", "max_request_bytes"):
            _positive(getattr(self, field_name), field_name)
        if self.max_response_bytes > 20_000_000:
            raise ValueError("max_response_bytes cannot exceed 20000000")

    @property
    def endpoint(self):
        return self.base_url.rstrip("/") + "/chat/completions"

    def public(self):
        return {"protocol": "chat_completions", "endpoint": self.endpoint, "model": self.model,
                "token_parameter": self.token_parameter, "json_mode": self.json_mode,
                "supports_images": self.supports_images,
                "max_image_bytes": self.max_image_bytes, "max_request_bytes": self.max_request_bytes}

    def credential(self):
        key = self.api_key if self.api_key is not None else os.environ.get(self.api_key_env)
        if not isinstance(key, str) or not key.strip() or any(ord(c) < 32 for c in key):
            raise ApiError("Model API credential is missing or invalid; set the configured environment variable")
        return key


def _positive(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")


@dataclass(frozen=True)
class ApiBudget:
    """Dispatch limits, not a promise about a provider's invoice.

Repeated prompts/images and unsuccessful attempts consume these reservations.
Actual token/cost totals are taken from responses, independently of these limits.
"""
    max_calls: int = 3
    max_input_chars: int = 80_000
    max_image_presentations: int = 16
    output_tokens_per_call: int = 4096
    max_reserved_output_tokens: int = 12_288

    def __post_init__(self):
        for name in self.__dataclass_fields__:
            _positive(getattr(self, name), name)
        if self.output_tokens_per_call > self.max_reserved_output_tokens:
            raise ValueError("Per-call output tokens exceed the total reserved output budget")

    def public(self):
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


def build_request(config, *, system, text, images=(), output_tokens=4096):
    """Include the actual JPEG bytes; never silently downgrade a visual request."""
    if not isinstance(system, str) or not system or not isinstance(text, str) or not text:
        raise ValueError("Model input requires nonempty system and user text")
    _positive(output_tokens, "output_tokens")
    if images and not config.supports_images:
        raise ApiError("The configured model does not support image inputs; choose a vision-capable model")
    content = [{"type": "text", "text": text}]
    # UTF-8 content and base64 lengths are lower bounds on the final JSON body.
    # Reject obvious oversize before reading or encoding image bytes.
    input_bytes = len(system.encode("utf-8")) + len(text.encode("utf-8"))
    for image in images:
        path = Path(image)
        size = path.stat().st_size
        if size > config.max_image_bytes:
            raise ApiError("Image input exceeded max_image_bytes; no request was sent")
        input_bytes += 4 * ((size + 2) // 3)
        if input_bytes > config.max_request_bytes:
            raise ApiError("Model input exceeded max_request_bytes; no request was sent")
    for image in images:
        with Path(image).open("rb") as handle:
            picture = handle.read(config.max_image_bytes + 1)
        if len(picture) > config.max_image_bytes:
            raise ApiError("Image input exceeded max_image_bytes; no request was sent")
        if not picture.startswith(b"\xff\xd8\xff"):
            raise ValueError("Model image inputs must be actual JPEG files")
        content.append({"type": "image_url", "image_url": {
            "url": "data:image/jpeg;base64," + base64.b64encode(picture).decode("ascii")}})
    request = {"model": config.model, "messages": [{"role": "system", "content": system},
                                                {"role": "user", "content": content}],
               config.token_parameter: output_tokens, "stream": False}
    if config.json_mode:
        request["response_format"] = {"type": "json_object"}
    serialize_request(config, request)
    return request


def serialize_request(config, request):
    """Bound encoded image and JSON bytes at both construction and transport."""
    allowed = {"model", "messages", "stream", config.token_parameter, "response_format"}
    if not isinstance(request, dict) or set(request) - allowed:
        raise ValueError("Unsupported Chat Completions request fields")
    total = 0
    for message in request.get("messages", []):
        content = message.get("content", [])
        if isinstance(content, str):
            total += len(content.encode("utf-8"))
            continue
        for item in content:
            if item.get("type") == "text":
                total += len(item.get("text", "").encode("utf-8"))
            elif item.get("type") == "image_url":
                value = item.get("image_url", {}).get("url", "")
                prefix = "data:image/jpeg;base64,"
                if not value.startswith(prefix):
                    raise ValueError("Only embedded JPEG images are supported")
                encoded_length = len(value) - len(prefix)
                padding = 2 if value.endswith("==") else 1 if value.endswith("=") else 0
                if encoded_length % 4:
                    raise ValueError("Invalid base64 JPEG input")
                decoded_length = encoded_length // 4 * 3 - padding
                if decoded_length > config.max_image_bytes:
                    raise ApiError("Image input exceeded max_image_bytes; no request was sent")
                # Length is checked before allocation; validate exact decoded size
                # in calls.request_manifest as well.
                total += len(value)
            else:
                raise ValueError("Unsupported API input type")
            if total > config.max_request_bytes:
                raise ApiError("Model input exceeded max_request_bytes; no request was sent")
    if total > config.max_request_bytes:
        raise ApiError("Model input exceeded max_request_bytes; no request was sent")
    data = json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(data) > config.max_request_bytes:
        raise ApiError("Serialized model request exceeded max_request_bytes; no request was sent")
    return data


@dataclass(frozen=True)
class TransportResponse:
    status_code: int
    body: bytes = field(repr=False)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def send(config, request):
    """Send once. Network ambiguity raises a safe error; only callers save receipts."""
    key = config.credential()
    data = serialize_request(config, request)
    req = urllib.request.Request(config.endpoint, data=data, method="POST",
                                 headers={"Authorization": "Bearer " + key,
                                          "Content-Type": "application/json", "Accept": "application/json"})
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(req, timeout=config.timeout_seconds) as response:
            body = response.read(config.max_response_bytes + 1)
            status = response.status
    except urllib.error.HTTPError as error:
        # Even a redirect or failed HTTP response consumes an attempt. Preserve a
        # bounded body for private receipts, but never send authentication onward.
        try:
            body = error.read(config.max_response_bytes + 1)
        finally:
            error.close()
        status = error.code
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise ApiError("Model request outcome is unknown after a transport error; automatic resend is blocked") from None
    if len(body) > config.max_response_bytes:
        raise ApiError("Model response exceeded the configured size limit; outcome is unknown and automatic resend is blocked")
    return TransportResponse(status, body)


def redact_bytes(body, secret):
    # Decode for storage even when a provider returned invalid JSON. Never retain
    # an echoed authentication value in a private receipt or exception.
    text = body.decode("utf-8", errors="replace")
    def clean(value, depth=0):
        if isinstance(value, str):
            value = value.replace(secret, "[REDACTED]")
            if depth < 4 and value.lstrip().startswith(("{", "[")):
                try:
                    nested = json.loads(value)
                except (ValueError, RecursionError):
                    pass
                else:
                    return json.dumps(clean(nested, depth + 1), ensure_ascii=False)
            return value
        if isinstance(value, list):
            return [clean(item, depth) for item in value]
        if isinstance(value, dict):
            return {str(key).replace(secret, "[REDACTED]"): clean(item, depth) for key, item in value.items()}
        return value
    try:
        value = clean(json.loads(text))
    except (ValueError, RecursionError):
        return text.replace(secret, "[REDACTED]").encode("utf-8")
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def parse_envelope(response):
    try:
        value = json.loads(response.body, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
    except (ValueError, UnicodeError, RecursionError):
        raise ApiError("Model endpoint returned invalid JSON; its response was saved and will not be resent automatically") from None
    if not isinstance(value, dict):
        raise ApiError("Model response envelope must be a JSON object")
    return value


def model_content(envelope, status_code=200):
    if not 200 <= status_code < 300:
        raise ApiError(f"Model endpoint returned HTTP {status_code}; the attempt was recorded without an automatic retry")
    choices = envelope.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise ApiError("Expected one Chat Completions choice")
    choice = choices[0]
    if choice.get("finish_reason") != "stop":
        raise ApiError("Model completion did not finish normally; no paid repair or retry was performed")
    message = choice.get("message")
    if not isinstance(message, dict) or message.get("refusal"):
        raise ApiError("Model completion was refused or has no message")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ApiError("Model completion has no text content")
    try:
        result = json.loads(content, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
    except (ValueError, RecursionError):
        raise ApiError("Model content is not a JSON object; no paid repair was performed") from None
    if not isinstance(result, dict):
        raise ApiError("Model content must be a JSON object")
    return result

"""Durable API attempts: reserve before dispatch, persist before interpretation."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from contextlib import nullcontext
from pathlib import Path

from . import api
from .run import load, now, open_run, run_lock
from .usage import record_api_receipt


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def durable_save(path, value):
    """Replace and fsync both file and directory before a network dispatch."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode() + b"\n"
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data); handle.flush(); os.fsync(handle.fileno())
    temp.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def request_manifest(request, config):
    """A private input inventory without raw media, prompt text or authorization."""
    encoded = api.serialize_request(config, request)
    chars, images = 0, []
    texts = []
    for message in request["messages"]:
        content = message["content"]
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for item in content:
            if item["type"] == "text":
                value = item["text"]
                chars += len(value)
                texts.append({"role": message["role"], "chars": len(value),
                              "sha256": hashlib.sha256(value.encode()).hexdigest()})
            elif item["type"] == "image_url":
                url = item["image_url"]["url"]
                if not url.startswith("data:image/jpeg;base64,"):
                    raise ValueError("Only embedded JPEG images are supported")
                picture = base64.b64decode(url.split(",", 1)[1], validate=True)
                if len(picture) > config.max_image_bytes:
                    raise api.ApiError("Image input exceeded max_image_bytes; no request was sent")
                if not picture.startswith(b"\xff\xd8\xff"):
                    raise ValueError("Invalid JPEG request input")
                images.append({"sha256": hashlib.sha256(picture).hexdigest(), "bytes": len(picture)})
            else:
                raise ValueError("Unsupported API input type")
    return {"input_chars": chars, "image_presentations": len(images), "texts": texts, "images": images,
            "request_bytes": len(encoded)}


def _inputs(value):
    """Allow only local evidence identifiers and finite timestamps in receipts."""
    if value is None:
        return {"pack_id": None, "frame_ids": [], "segment_ids": [], "material_hashes": {}}
    from .notes import identifier
    result = {}
    for key in ("pack_id",):
        result[key] = identifier(value[key], key) if value.get(key) is not None else None
    for key in ("frame_ids", "segment_ids"):
        if not isinstance(value.get(key, []), list):
            raise ValueError("API evidence IDs must be lists")
        result[key] = [identifier(item, key) for item in value.get(key, [])]
    hashes = value.get("material_hashes", {})
    if not isinstance(hashes, dict):
        raise ValueError("API material hashes must be an object")
    import re
    result["material_hashes"] = {}
    for key, sha in hashes.items():
        identifier(key, "material ID")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise ValueError("API material hashes must be SHA256 strings")
        result["material_hashes"][key] = sha
    return result


def _restore(directory, marker, response, prices):
    envelope = None
    try:
        envelope = api.parse_envelope(response)
    except api.ApiError:
        pass
    receipt = record_api_receipt(directory, call_id=marker["call_id"], stage=marker["stage"],
        response=envelope, prices=prices, request_hash=marker["request_hash"], inputs=marker["inputs"],
        http_status=response.status_code)
    _, run = open_run(directory)
    if run.get("api_pending") == marker["call_id"]:
        run.pop("api_pending", None)
        durable_save(directory / "run.json", run)
    if envelope is None and not 200 <= response.status_code < 300:
        api.model_content({}, response.status_code)
    if envelope is None:
        api.parse_envelope(response)  # raise the safe error after the unknown usage was imported
    return envelope, receipt


def invoke(run_dir, config, request, *, stage, budget=None, inputs=None, prices=None, transport=None, _locked=False):
    """Execute/cache a single request. A pending attempt blocks every new dispatch.

    The engine holds the run lock once and uses the private _locked handoff; callers
    of this standalone function get the same one-writer protection automatically.
    """
    directory = Path(run_dir).resolve()
    budget = budget or api.ApiBudget()
    transport = transport or api.send
    with nullcontext() if _locked else run_lock(directory):
        return _invoke(directory, config, request, stage, budget, inputs, prices, transport)


def _invoke(directory, config, request, stage, budget, inputs, prices, transport):
    from .notes import identifier
    identifier(stage, "API stage")
    _, run = open_run(directory)
    if run.get("status") == "finished":
        raise ValueError("Run is finished; API calls cannot change frozen usage")
    key = config.credential()  # validate before creating a possible-charge marker
    manifest = request_manifest(request, config)
    inventory = _inputs(inputs)
    if request.get("model") != config.model:
        raise ValueError("Request model must match ProviderConfig")
    output = request.get(config.token_parameter)
    if isinstance(output, bool) or not isinstance(output, int) or not 0 < output <= budget.output_tokens_per_call:
        raise ValueError("Request output limit exceeds the per-call API budget")
    if request.get("stream") is not False or request.get("tools") or request.get("tool_choice"):
        raise ValueError("API calls require non-streaming text/image completion without tools")
    if manifest["image_presentations"] and not config.supports_images:
        raise api.ApiError("The configured model does not support image inputs")
    if prices is not None:
        from .usage import price_usage
        # Validate a supplied table before paying; the actual response model may
        # still differ, in which case its price remains unknown.
        price_usage({"model": config.model}, {"input_tokens": 0, "cached_input_tokens": 0,
                                              "output_tokens": 0}, prices)
    identity = {"provider": config.public(), "request": request, "inputs": inventory, "stage": stage}
    hashed = digest(identity)
    folder = directory / "api"; folder.mkdir(exist_ok=True, mode=0o700)
    settings = folder / "budget.json"
    if settings.exists():
        if load(settings) != budget.public():
            raise ValueError("This API run has a frozen budget; use a new run to change it")
    else:
        durable_save(settings, budget.public())
    markers = [load(path) for path in sorted(folder.glob("*/attempt.json"))]
    for row in markers:
        artifact = folder / row["request_hash"] / "response.json"
        if row.get("status") != "received" and artifact.is_file():
            # Response persistence can precede the marker update during a crash.
            saved = load(artifact)
            if (saved.get("call_id") != row["call_id"] or saved.get("request_hash") != row["request_hash"]
                    or not isinstance(saved.get("body"), str) or not isinstance(saved.get("http_status"), int)):
                raise api.ApiError("Interrupted API response has invalid receipt identity; automatic resend is blocked")
            row.update(status="received", received_at=now(), response_sha256=digest(saved))
            durable_save(artifact.parent / "attempt.json", row)
    cached = next((row for row in markers if row["request_hash"] == hashed), None)
    pending = [row for row in markers if row.get("status") != "received"]
    if pending and (cached is None or cached.get("status") != "received"):
        raise api.ApiError("An API request has an unknown outcome; automatic resend is blocked. Inspect its attempt receipt before recovery")
    if run.get("api_pending") and not any(row["call_id"] == run["api_pending"] for row in markers):
        raise api.ApiError("An interrupted API dispatch has no complete receipt; automatic resend is blocked")
    if cached is not None:
        artifact = load(folder / hashed / "response.json")
        if (not cached.get("response_sha256") or digest(artifact) != cached["response_sha256"]
                or artifact.get("call_id") != cached["call_id"] or artifact.get("request_hash") != hashed):
            raise api.ApiError("Saved API response integrity check failed; no request was resent")
        if (not isinstance(artifact.get("body"), str) or not isinstance(artifact.get("http_status"), int)
                or len(artifact["body"].encode()) > config.max_response_bytes):
            raise api.ApiError("Saved API response is invalid or exceeds the configured response limit")
        response = api.TransportResponse(artifact["http_status"], artifact["body"].encode())
        envelope, receipt = _restore(directory, cached, response, prices)
        return {"call_id": cached["call_id"], "request_hash": hashed, "cached": True,
                "envelope": envelope, "content": api.model_content(envelope, response.status_code), "receipt": receipt}
    if pending or run.get("api_pending"):
        raise api.ApiError("An API request has an unknown outcome; a new dispatch is blocked")
    if len(markers) + 1 > budget.max_calls:
        raise api.ApiError("API call budget exhausted; no request was sent")
    totals = {key: sum(row["reservation"][key] for row in markers) for key in
              ("input_chars", "image_presentations", "output_tokens")}
    reservation = {"input_chars": manifest["input_chars"], "image_presentations": manifest["image_presentations"], "output_tokens": output}
    limits = {"input_chars": budget.max_input_chars, "image_presentations": budget.max_image_presentations,
              "output_tokens": budget.max_reserved_output_tokens}
    if any(totals[name] + reservation[name] > limits[name] for name in limits):
        raise api.ApiError("API input/output presentation budget exhausted; no request was sent")
    call_id = str(uuid.uuid4())
    marker = {"schema_version": 1, "call_id": call_id, "stage": stage, "request_hash": hashed,
              "started_at": now(), "status": "dispatching", "provider": config.public(),
              "inputs": inventory, "request_manifest": manifest, "reservation": reservation}
    # A process crash even between these files must not let native commands alter
    # this run or let a later invocation buy the same unknown request again.
    run["api_pending"] = call_id
    durable_save(directory / "run.json", run)
    path = folder / hashed
    durable_save(path / "attempt.json", marker)
    try:
        response = transport(config, request)
    except Exception:
        # Never retain a transport's untrusted exception text. Timeout/reset after
        # acceptance is indistinguishable from an uncharged failure.
        raise api.ApiError("Model request outcome is unknown after a transport error; automatic resend is blocked") from None
    if (not isinstance(response, api.TransportResponse) or isinstance(response.status_code, bool)
            or not isinstance(response.status_code, int) or not 100 <= response.status_code <= 599
            or not isinstance(response.body, bytes) or len(response.body) > config.max_response_bytes):
        raise api.ApiError("Model transport returned an invalid or oversized response; outcome is unknown and resend is blocked")
    safe_body = api.redact_bytes(response.body, key)
    artifact = {"schema_version": 1, "call_id": call_id, "request_hash": hashed,
        "http_status": response.status_code, "body": safe_body.decode(), "received_at": now(),
        "redacted": safe_body != response.body}
    durable_save(path / "response.json", artifact)
    marker.update(status="received", received_at=now(), response_sha256=digest(artifact))
    durable_save(path / "attempt.json", marker)
    response = api.TransportResponse(response.status_code, safe_body)
    envelope, receipt = _restore(directory, marker, response, prices)
    return {"call_id": call_id, "request_hash": hashed, "cached": False, "envelope": envelope,
            "content": api.model_content(envelope, response.status_code), "receipt": receipt}

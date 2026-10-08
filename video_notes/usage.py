"""Record measured API usage, optional native counters, and honest missing values."""
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .run import ensure_open, load, now, open_run, save, text_estimate


def counter_snapshot(path):
    if not path or not Path(path).is_file():
        return None
    last = None
    with Path(path).open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - 2_000_000))
        for line in handle:
            try:
                row = json.loads(line)
                payload = row.get("payload", {})
                if row.get("type") != "event_msg" or payload.get("type") != "token_count":
                    continue
                counters = payload.get("info", {}).get("total_token_usage")
                if counters is not None:
                    last = {"timestamp": row.get("timestamp"), "counters": counters, "path": str(Path(path).resolve())}
            except (ValueError, TypeError, AttributeError):
                continue
    return last


def native_delta(before, after):
    if not before or not after or before["path"] != after["path"]:
        return None
    keys = ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens")
    if any(key not in before["counters"] or key not in after["counters"] for key in keys):
        return None
    values = {key: after["counters"][key] - before["counters"][key] for key in keys}
    if any(not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in values.values()):
        return None
    if before.get("timestamp") == after.get("timestamp"):
        return None  # Counters have not refreshed; zero is not evidence of zero usage.
    return {**values, "source": "codex_session_cumulative_counter_difference",
            "scope": "thread interval from prepare to finish; final reply after finish excluded",
            "before_at": before.get("timestamp"), "after_at": after.get("timestamp")}


def api_usage(response):
    usage = response.get("usage") or {}
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
    cached = (usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
    if input_tokens is None or output_tokens is None:
        raise ValueError("Response has no actual input/output token counters")
    if any(not isinstance(n, int) or isinstance(n, bool) or n < 0 for n in (input_tokens, output_tokens, cached)) or cached > input_tokens:
        raise ValueError("Invalid API token counters")
    return {"input_tokens": input_tokens, "cached_input_tokens": cached,
            "output_tokens": output_tokens, "total_tokens": input_tokens + output_tokens}


def price_usage(response, usage, prices):
    if not prices:
        return None
    if response.get("model") != prices.get("model"):
        raise ValueError("Price table must match the API response model exactly")
    if prices.get("currency") != "USD" or not prices.get("source_url") or not prices.get("checked_at"):
        raise ValueError("Prices need USD, source_url, checked_at and an exact model")
    rates = [Decimal(str(prices[k])) for k in ("input_per_million", "cached_input_per_million", "output_per_million")]
    if any(not r.is_finite() or r < 0 for r in rates):
        raise ValueError("Invalid token prices")
    regular = usage["input_tokens"] - usage["cached_input_tokens"]
    cost = (regular * rates[0] + usage["cached_input_tokens"] * rates[1] + usage["output_tokens"] * rates[2]) / Decimal(1_000_000)
    return {"usd": str(cost), "basis": "actual_tokens_times_supplied_rates_not_invoice",
            "model": prices["model"], "pricing_source": prices["source_url"], "pricing_checked_at": prices["checked_at"]}


def add_api(args):
    directory, run = open_run(args.run); ensure_open(run)
    response = load(args.response)
    identifier = response.get("id")
    if not identifier:
        raise ValueError("An API response ID is required for deduplication")
    if any(call["id"] == identifier for call in run["api_calls"]):
        print(json.dumps({"cached": True, "id": identifier})); return
    usage = api_usage(response)
    prices = load(args.prices) if args.prices else None
    run["api_calls"].append({"id": identifier, "stage": args.stage, "model": response.get("model"),
                              "tokens": usage, "cost": price_usage(response, usage, prices)})
    save(directory / "run.json", run)
    print(json.dumps(run["api_calls"][-1], ensure_ascii=False))



def measured_api_usage(response):
    """Import available counters without manufacturing cached-token information."""
    if not isinstance(response, dict) or not isinstance(response.get("usage"), dict):
        return None
    reported = response["usage"]
    incoming = reported.get("input_tokens", reported.get("prompt_tokens"))
    outgoing = reported.get("output_tokens", reported.get("completion_tokens"))
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in (incoming, outgoing)):
        return None
    details = reported.get("input_tokens_details", reported.get("prompt_tokens_details"))
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    if cached is not None and (isinstance(cached, bool) or not isinstance(cached, int) or not 0 <= cached <= incoming):
        return None
    total = reported.get("total_tokens", incoming + outgoing)
    if isinstance(total, bool) or not isinstance(total, int) or total != incoming + outgoing:
        return None
    return {"input_tokens": incoming, "cached_input_tokens": cached,
            "output_tokens": outgoing, "total_tokens": total}


def record_api_receipt(directory, *, call_id, stage, response, request_hash, inputs,
                       prices=None, http_status=None):
    """Idempotently record one local dispatch, even if provider counters/ID are absent.

    Only receipt-derived provider fields count as observed usage. This function
    runs under the caller's run lock and never records model content or credentials.
    """
    directory, run = open_run(directory)
    if run.get("status") == "finished":
        raise ValueError("Cannot import API usage after a run is finished")
    response = response if isinstance(response, dict) else {}
    tokens = measured_api_usage(response)
    model = response.get("model") if isinstance(response.get("model"), str) else None
    provider_id = response.get("id") if isinstance(response.get("id"), str) else None
    cost = None
    if (prices and model == prices.get("model") and tokens is not None
            and tokens.get("cached_input_tokens") is not None):
        cost = price_usage(response, tokens, prices)
    receipt = {"id": "local:" + call_id, "call_id": call_id, "stage": stage,
               "provider_response_id": provider_id, "model": model, "tokens": tokens, "cost": cost,
               "request_hash": request_hash, "http_status": http_status,
               "reading_method": "api_material_submission_and_response",
               "inputs": inputs}
    previous = next((row for row in run["api_calls"] if row.get("call_id") == call_id), None)
    if previous is not None:
        return previous
    run["api_calls"].append(receipt)
    save(directory / "run.json", run)
    return receipt


def api_totals(calls):
    keys = ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens")
    known = [row["tokens"] for row in calls if row.get("tokens") is not None]
    subtotal = {key: sum(value[key] for value in known if value.get(key) is not None)
                if any(value.get(key) is not None for value in known) else None for key in keys} if known else None
    actual = None
    if calls and len(known) == len(calls):
        actual = {key: sum(value[key] for value in known) if all(value.get(key) is not None for value in known) else None for key in keys}
    costs = [Decimal(row["cost"]["usd"]) for row in calls if row.get("cost")]
    return {"tokens": actual, "tokens_known_subtotal": subtotal,
            "unknown_token_calls": len(calls) - len(known),
            "cost": sum(costs, Decimal(0)) if calls and len(costs) == len(calls) else None,
            "cost_known_subtotal": sum(costs, Decimal(0)) if costs else None,
            "unknown_cost_calls": len(calls) - len(costs)}


def ledger_append(path, report):
    path = Path(path).expanduser(); path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        for line in handle:
            try:
                if json.loads(line).get("run_id") == report["run_id"]:
                    return
            except ValueError:
                continue
        handle.seek(0, 2)
        handle.write(json.dumps(report, ensure_ascii=False) + "\n"); handle.flush()
        os.fsync(handle.fileno())


def finish(args):
    directory, run = open_run(args.run)
    ledger = args.ledger or run.get("ledger") or str(Path.home() / ".local/share/video-study-notes/usage.jsonl")
    if run.get("status") == "finished":
        report = load(directory / "usage.json")
        ledger_append(ledger, report)
        print(json.dumps({"cached": True, "usage_file": str(directory / "usage.json")})); return
    ensure_open(run)
    read_ids = set(args.read_packs.split(",")) if args.read_packs else set()
    if read_ids - {p["id"] for p in run["packs"]}:
        raise ValueError("Unknown reading pack ID")
    for pack in run["packs"]:
        pack["claimed_read"] = pack["id"] in read_ids
    if args.status == "complete":
        has_visuals = any(p["claimed_read"] and p["images"] for p in run["packs"])
        has_transcript = any(p["claimed_read"] and p["kind"] == "overview" and p["text_chars"] for p in run["packs"])
        if run["remaining_range"] or run["transcript_status"] != "ready" or not has_visuals or not has_transcript:
            raise ValueError("Complete needs the full requested interval, a read transcript and read visuals; use partial/visual_only/extraction_only otherwise")
    delta = native_delta(run.get("native_counter_before"), counter_snapshot(run.get("session_log")))
    native_actual = load(args.native_usage) if args.native_usage else None
    if native_actual:
        if not native_actual.get("source") or not native_actual.get("scope"):
            raise ValueError("Native usage must identify measured source and scope")
        delta = {**api_usage({"usage": native_actual}), "source": native_actual["source"], "scope": native_actual["scope"]}
    measured = api_totals(run["api_calls"])
    cost, tokens_api = measured["cost"], measured["tokens"]
    text = "\n".join((directory / "packs" / f"{p['id']}.txt").read_text(encoding="utf-8") for p in run["packs"] if p["claimed_read"])
    elapsed = (datetime.fromisoformat(now()) - datetime.fromisoformat(run["started_at"])).total_seconds()
    processed_seconds = run["processed_range"][1] - run["processed_range"][0]
    source = load(directory / "source.json")
    report = {"schema_version": 1, "run_id": run["run_id"], "recorded_at": now(), "run_directory": str(directory),
              "video": {"title": source["metadata"].get("title"), "url": source["source"].get("canonical_url"),
                        "page": source.get("selection", {}).get("page"), "duration_seconds": run["video_duration_seconds"],
                        "processed_range": run["processed_range"], "processed_minutes": round(processed_seconds / 60, 3),
                        "remaining_range": run["remaining_range"]},
              "elapsed_seconds": round(elapsed, 3), "preset": run["preset"], "result_status": args.status,
              "content_source": run["content_source"], "transcript_status": run["transcript_status"],
              "transcript_coverage_seconds": run["transcript_coverage_seconds"],
              "materials": {"overview_frames_extracted": sum(f["kind"] == "overview" for f in run["frames"]),
                            "detail_frames_extracted": sum(f["kind"] == "detail" for f in run["frames"]),
                            "candidate_frames_scanned": run.get("sampling", {}).get("candidate_frames", 0),
                            "read_batches_reserved": len(run["packs"]), "read_batches_claimed": len(read_ids),
                            "image_presentations_claimed": sum(p["images"] for p in run["packs"] if p["claimed_read"]),
                            "text_chars_claimed": sum(p["text_chars"] for p in run["packs"] if p["claimed_read"])},
              "tokens": {"native_actual": delta, "api_actual": tokens_api,
                         "api_known_subtotal": measured["tokens_known_subtotal"],
                         "api_unknown_token_calls": measured["unknown_token_calls"],
                         "material_text_estimate": text_estimate(text), "vision_estimate": None,
                         "estimate_scope": "material text only, CJK chars + other UTF8 bytes/4; excludes vision, reasoning, tools and repeated conversation context"},
              "cost": {"api_usd": str(cost) if cost is not None else None,
                       "api_known_subtotal_usd": str(measured["cost_known_subtotal"]) if measured["cost_known_subtotal"] is not None else None,
                       "api_unknown_cost_calls": measured["unknown_cost_calls"],
                       "codex_subscription_usd": None, "basis": "no per-run USD conversion for subscription quota"},
              "api_calls": run["api_calls"], "quota_before": load(args.quota_before) if args.quota_before else None,
              "quota_after": load(args.quota_after) if args.quota_after else None}
    if delta and processed_seconds:
        report["native_tokens_per_processed_minute"] = round(delta["total_tokens"] * 60 / processed_seconds, 3)
    save(directory / "usage.json", report)
    ledger_append(ledger, report)
    run["status"] = "finished"; run["finished_at"] = now(); save(directory / "run.json", run)
    print(json.dumps({"usage_file": str(directory / "usage.json"), "ledger": str(Path(ledger).expanduser()),
                      "native_actual": delta, "api_usd": report["cost"]["api_usd"], "processed_minutes": report["video"]["processed_minutes"]}, ensure_ascii=False))


def summary(args):
    records = []
    path = Path(args.ledger).expanduser()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            records.append(json.loads(line))
    print(json.dumps([{"run_id": r["run_id"], "title": r["video"]["title"], "minutes": r["video"]["processed_minutes"],
                       "native_tokens": (r["tokens"]["native_actual"] or {}).get("total_tokens"),
                       "api_tokens": (r["tokens"]["api_actual"] or {}).get("total_tokens"), "api_usd": r["cost"]["api_usd"],
                       "text_estimate": r["tokens"]["material_text_estimate"],
                       "images_claimed": r["materials"]["image_presentations_claimed"], "status": r["result_status"]}
                      for r in records[-args.last:]], ensure_ascii=False, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__); sub = parser.add_subparsers(dest="command", required=True)
    ledger = str(Path.home() / ".local/share/video-study-notes/usage.jsonl")
    p = sub.add_parser("add-api"); p.add_argument("--run", required=True); p.add_argument("--response", required=True)
    p.add_argument("--prices"); p.add_argument("--stage", default="vision"); p.set_defaults(func=add_api)
    p = sub.add_parser("finish"); p.add_argument("--run", required=True); p.add_argument("--read-packs", default="")
    p.add_argument("--ledger"); p.add_argument("--native-usage"); p.add_argument("--quota-before"); p.add_argument("--quota-after")
    p.add_argument("--status", choices=("complete", "partial", "failed", "extraction_only", "visual_only"), required=True); p.set_defaults(func=finish)
    p = sub.add_parser("summary"); p.add_argument("--ledger", default=ledger); p.add_argument("--last", type=int, default=20); p.set_defaults(func=summary)
    args = parser.parse_args(argv)
    try:
        args.func(args); return 0
    except (ValueError, OSError, KeyError, TypeError, InvalidOperation) as exc:
        print(json.dumps({"ok": False, "message": str(exc)}, ensure_ascii=False), file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())

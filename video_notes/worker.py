"""One isolated engine execution; API credentials arrive only through stdin."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from . import api, calls, engine
from .locking import file_lock
from .product import credential, identifier, make_artifacts, safe_failure
from .run import load, now


def execute(root, jid, attempt, key, *, analyze=engine.analyze, rednote_access_url=None, rednote_skill=None):
    root, jid, attempt = Path(root).resolve(), identifier(jid), identifier(attempt)
    folder = root / "jobs" / jid
    if folder.is_symlink() or not folder.resolve().is_relative_to(root / "jobs"):
        return 1
    receipt = folder / (attempt + ".json")
    try:
        # Survives supervisor death. A new supervisor cannot execute another job
        # while this child is making model calls or preparing its materials.
        with file_lock(root / ".execution.lock", blocking=False):
            record = load(folder / "job.json")
            if record.get("attempt") != attempt:
                return 1  # superseded launch: it must not write current job state
            calls.durable_save(root / ".execution-active.json", {"job": jid, "attempt": attempt})
            spec = record["spec"]
            key = credential(key)
            calls.durable_save(receipt, {"status": "running", "started_at": now()})
            provider = api.ProviderConfig(**spec["provider"], api_key=key)
            settings = dict(spec["budget"])
            settings["max_image_presentations"] = settings.pop("max_images")
            budget = api.ApiBudget(**settings)
            result = analyze(spec["url"], run_dir=folder / "run", provider=provider, budget=budget,
                             start=spec["start"], end=spec["end"], preset=spec["preset"], focus=spec["focus"],
                             language=spec["language"], allow_asr=spec["allow_asr"], strategy=spec["strategy"],
                             outputs=("html", "md"), ledger=root / "usage.jsonl",
                             rednote_access_url=rednote_access_url, rednote_skill=rednote_skill)
            status = result.get("status")
            if status in ("complete", "partial", "visual_only"):
                make_artifacts(folder / "run")
                status, message = "completed", "笔记已生成。请查看处理区间、抽样覆盖与用量记录。"
                recoverable = False
            else:
                status = "outcome_unknown" if status == "outcome_unknown" else "failed"
                message = safe_failure(status, result.get("reason", ""))
                # Confirmed but unusable model responses are immutable cache
                # entries. Changing a key cannot repair them by replaying.
                recoverable = (not any((folder / "run/api").glob("*/attempt.json"))
                               or (folder / "run/api/draft.json").is_file())
                if status == "failed" and not recoverable:
                    message += " 本次已确认的响应不可在同一任务中重购；修改配置后可明确创建新任务。"
            calls.durable_save(receipt, {"status": status, "message": message, "recoverable": recoverable, "finished_at": now()})
            return 0 if status == "completed" else 1
    except BlockingIOError:
        calls.durable_save(receipt, {"status": "interrupted", "message": "另一个执行进程仍在运行。本次没有发出模型请求。"})
    except Exception:
        calls.durable_save(receipt, {"status": "failed", "message": "执行未完成，已有材料与记录保留。请检查提取环境或明确继续。"})
    finally:
        key = None
        rednote_access_url = None
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--attempt", required=True)
    args = parser.parse_args(argv)
    # All worker-created media/receipts are private to this OS user by default.
    if os.name != "nt":
        os.umask(0o077)
    try:
        payload = sys.stdin.buffer.read(16_001)
        if len(payload) > 16_000:
            return 1
        value = json.loads(payload)
        payload = b""
        key = value.pop("api_key")
        access_url = value.pop("rednote_access_url", None)
        skill = value.pop("rednote_skill", None)
        if value or (access_url is not None and not isinstance(access_url, str)) or (skill is not None and not isinstance(skill, str)):
            return 1
        value.clear()
        return execute(args.data_dir, args.job, args.attempt, key, rednote_access_url=access_url, rednote_skill=skill)
    except (ValueError, KeyError, TypeError, OSError):
        return 1
    finally:
        key = None
        access_url = None


if __name__ == "__main__":
    raise SystemExit(main())

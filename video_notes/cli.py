"""Portable command dispatch; domain modules own their arguments."""
import sys


def dispatch(argv):
    command = argv[0] if argv else "--help"
    if command in ("--help", "-h"):
        print("usage: video_notes.py COMMAND [options]\n\n"
              "Commands: analyze, analyze-prepared, prepare, frames, transcribe, read, finish, note, export, publish, continue, add-api, summary\n"
              "Use COMMAND --help for its options. analyze runs a configured vision API; read is the native Codex workflow.")
        return 0
    if command in ("analyze", "analyze-prepared"):
        from .engine import main
        return main(argv[1:])
    if command in ("note", "export"):
        from .notes import main
        return main(argv)
    if command == "publish":
        from .delivery.feishu import main
        return main(argv[1:])
    if command == "read":
        from .reading import main
        return main(argv[1:])
    if command in ("finish", "add-api", "summary"):
        from .usage import main
        return main(argv)
    from .materials import main
    return main(argv)


def main(argv=None):
    from .run import run_lock, activity
    import json
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv[0] if argv else "--help"
    if command in ("analyze", "analyze-prepared"):
        return dispatch(argv)  # engine owns one lock across acquisition and API calls
    directory = None
    for flag in (("--out",) if command in ("prepare", "continue") else ("--run",)):
        if flag in argv and argv.index(flag) + 1 < len(argv):
            directory = argv[argv.index(flag) + 1]
            break
    try:
        if directory and "--help" not in argv:
            with run_lock(directory), activity(directory, command):
                result = dispatch(argv)
                if result:
                    raise RuntimeError("Command failed; cached materials remain available")
                return 0
        return dispatch(argv)
    except (ValueError, RuntimeError, OSError, KeyError, TypeError) as error:
        print(json.dumps({"ok": False, "message": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1

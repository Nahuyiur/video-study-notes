"""Portable command dispatch; domain modules own their arguments."""
import sys


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv[0] if argv else "--help"
    if command == "read":
        from .reading import main
        return main(argv[1:])
    if command in ("finish", "add-api", "summary"):
        from .usage import main
        return main(argv)
    if command == "html":
        from .delivery.html import main
        return main(argv[1:])
    from .materials import main
    return main(argv)

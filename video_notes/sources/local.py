"""Local video metadata without platform dependencies."""
import hashlib
from pathlib import Path

from ..contracts import source_record


def resolve(video):
    from ..materials import probe
    path = Path(video).resolve()
    identity = hashlib.sha256((str(path) + str(path.stat().st_size) + str(path.stat().st_mtime_ns)).encode()).hexdigest()
    return source_record("local", identity, "1", path.stem, probe(path))

"""Small isolated helper for durable position snapshots."""
import json
import os
import tempfile
from pathlib import Path


def write_jsonl_snapshot(path, records):
    """Atomically replace a JSONL snapshot only after its complete temp file is synced."""
    target = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=target.parent,
                prefix=f".{target.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            for record in records:
                stream.write(json.dumps(record, default=str) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        return True
    except (OSError, TypeError, ValueError):
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        return False


def read_jsonl_snapshot(path, expected_mode):
    """Read a mode-scoped snapshot; malformed or mismatched records fail closed."""
    target = Path(path)
    if not target.exists():
        return []
    try:
        records = []
        with target.open(encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                if not isinstance(record, dict) or record.get("mode") != expected_mode:
                    return None
                records.append(record)
        return records
    except (OSError, json.JSONDecodeError):
        return None

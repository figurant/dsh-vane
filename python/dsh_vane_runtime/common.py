import hashlib
import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from uuid import UUID


class RuntimeFailure(Exception):
    def __init__(self, code, message, retryable=False, details=None):
        super().__init__(message)
        self.code, self.retryable, self.details = code, retryable, details


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def contained(root, path):
    root, path = Path(root).resolve(), Path(path).resolve()
    if not path.is_relative_to(root):
        raise RuntimeFailure("PATH_OUTSIDE_ROOT", "Resolved path is outside its configured root.")
    return path


def relative_file(root, value):
    if not isinstance(value, str) or not value or "\\" in value:
        raise RuntimeFailure("INVALID_PATH", "Expected a relative POSIX file path.")
    p = PurePosixPath(value)
    if p.is_absolute() or ".." in p.parts or str(p) != value:
        raise RuntimeFailure("INVALID_PATH", "Artifact/package path must be normalized and relative.")
    target = contained(root, Path(root) / value)
    if not target.is_file():
        raise RuntimeFailure("FILE_NOT_FOUND", "Referenced ordinary file does not exist.")
    return target


def uuid_value(value):
    try:
        if str(UUID(value)) != value:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise RuntimeFailure("INVALID_ARGUMENT", "Expected a canonical UUID.") from None
    return value


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise RuntimeFailure("INVALID_IDENTIFIER", "Use an SQL identifier containing letters, digits and underscores.")
    return '"' + value + '"'


def qualified(value):
    parts = value.split(".")
    if len(parts) not in (1, 2):
        raise RuntimeFailure("INVALID_IDENTIFIER", "Expected table or schema.table.")
    return ".".join(identifier(p) for p in parts)


def json_value(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, bytes):
        return {"encoding": "hex", "value": value.hex()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    return str(value)


def redact(value):
    text = value if isinstance(value, str) else canonical(json_value(value))
    for key, secret in os.environ.items():
        if len(secret) >= 5 and re.search(r"KEY|TOKEN|SECRET|PASSWORD|DSN|CREDENTIAL", key, re.I):
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)(password|api_key|token)\s*=\s*[^\s;&]+", r"\1=[REDACTED]", text)
    return text


def schema_path(name):
    local = Path(__file__).parent / "contracts" / name
    return local if local.exists() else Path(__file__).resolve().parents[2] / "contracts" / name


def validate_schema(value, path, code="CONTRACT_INVALID"):
    import jsonschema
    try:
        jsonschema.Draft202012Validator(json.loads(Path(path).read_text())).validate(value)
    except jsonschema.ValidationError as e:
        raise RuntimeFailure(code, f"Schema validation failed at {'/'.join(map(str, e.path))}: {e.validator}") from None


def write_json(path, value):
    Path(path).write_text(canonical(value) + "\n", encoding="utf-8")

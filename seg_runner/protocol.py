"""Wire format and file publishing shared by the runner and its client.

Messages are one JSON object per line. Every message carries
`protocol_version`; a peer that sees another version stops rather than
guessing. The child writes nothing else to its protocol stream -- library
output goes to stderr (`runner.main` rebinds fd 1 before any engine import).

Messages, child -> parent:
    hello   {engine, identity, device}
    result  {task_id, record}        sent only after the record is published
    error   {task_id, message}
    done    {}
Messages, parent -> child:
    task    {task_id, input, params, out_dir}
    shutdown {}
"""
import json
import os

PROTOCOL_VERSION = 1

# Terminal task states. Exactly one is assigned per task and never replaced.
OK = "ok"
FAILED = "failed"
CANCELLED = "cancelled"
TERMINAL = (OK, FAILED, CANCELLED)


class ProtocolError(RuntimeError):
    """A line that is not a message of this protocol version."""


def encode(kind, **fields):
    msg = {"protocol_version": PROTOCOL_VERSION, "type": kind}
    msg.update(fields)
    return json.dumps(msg, separators=(",", ":")) + "\n"


def decode(line):
    try:
        msg = json.loads(line)
    except ValueError as exc:
        raise ProtocolError(f"not a protocol message: {line[:200]!r}") from exc
    if not isinstance(msg, dict) or "type" not in msg:
        raise ProtocolError(f"not a protocol message: {line[:200]!r}")
    if msg.get("protocol_version") != PROTOCOL_VERSION:
        raise ProtocolError(f"protocol version {msg.get('protocol_version')!r}, "
                            f"expected {PROTOCOL_VERSION}")
    return msg


def _replace_into(path, write):
    tmp = f"{path}.tmp{os.getpid()}"
    try:
        with open(tmp, "wb") as f:
            write(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def publish_array(path, array):
    """Write an .npy file so that `path` either does not exist or is complete."""
    import numpy as np
    _replace_into(path, lambda f: np.save(f, array))


def publish_json(path, data):
    payload = json.dumps(data, indent=1, ensure_ascii=False).encode("utf-8")
    _replace_into(path, lambda f: f.write(payload))

"""stdin control loop and separate fd 3 protocol; connection belongs to one worker."""
import json
import os
import queue
import sys
import threading

from .common import RuntimeFailure, json_value, redact

PROTOCOL = "dsh-vane-ipc/v1"


def main():
    max_frame = int(os.environ.get("DSH_VANE_MAX_FRAME", "1048576"))
    output = os.fdopen(3, "w", buffering=1, encoding="utf-8", closefd=False)
    lock, state_lock = threading.Lock(), threading.RLock()
    jobs = queue.Queue()
    state = {"runtime": None, "cancelled": set(), "completed": set(), "current": None}

    def respond(frame, data=None, error=None):
        result = {"protocol": PROTOCOL, "request_id": frame["request_id"], "workspace_id": frame["workspace_id"], "ok": error is None}
        if error is None:
            result["data"] = json_value(data)
        else:
            result["error"] = {"code": getattr(error, "code", "RUNTIME_ERROR"), "message": redact(str(error))[:1500], "retryable": getattr(error, "retryable", False)}
            if getattr(error, "details", None) is not None:
                result["error"]["details"] = error.details
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode()) > max_frame:
            result.pop("data", None)
            result.update(ok=False, error={"code": "IPC_FRAME_LIMIT", "message": "Runtime response exceeds frame limit.", "retryable": False})
            encoded = json.dumps(result)
        with lock:
            output.write(encoded + "\n")

    def worker():
        from .runtime import Runtime
        while True:
            frame = jobs.get()
            if frame is None:
                break
            action, data = frame["action"], frame.get("data", {})
            try:
                if action == "hello":
                    if state["runtime"] is not None:
                        raise RuntimeFailure("IPC_PROTOCOL_ERROR", "Runtime already initialized.")
                    runtime = Runtime(data, frame["workspace_id"])
                    with state_lock:
                        state["runtime"] = runtime
                    respond(frame, runtime.capabilities())
                    continue
                runtime = state["runtime"]
                if runtime is None or frame["workspace_id"] != runtime.workspace_id:
                    raise RuntimeFailure("IPC_PROTOCOL_ERROR", "Handshake or workspace identity missing.")
                if action == "close":
                    runtime.close()
                    respond(frame, {"closed": True})
                    os._exit(0)
                operation_id = frame.get("operation_id")
                if not operation_id:
                    raise RuntimeFailure("IPC_PROTOCOL_ERROR", "Operation ID is required.")
                with state_lock:
                    state["current"] = operation_id
                def check():
                    with state_lock:
                        if operation_id in state["cancelled"]:
                            raise RuntimeFailure("CANCELLED", "Operation cancelled.")
                # Publication and cancellation share the finalization lock.
                def completed():
                    state["completed"].add(operation_id)
                result = runtime.run(action, data, operation_id, check, state_lock, completed)
                respond(frame, result)
            except BaseException as error:
                respond(frame, error=error)
            finally:
                with state_lock:
                    state["current"] = None
                jobs.task_done()
        if state["runtime"] is not None:
            state["runtime"].close()

    thread = threading.Thread(target=worker, daemon=True, name="vane-execution")
    thread.start()
    while True:
        line = sys.stdin.buffer.readline(max_frame + 2)
        if not line:
            break
        if len(line) > max_frame + 1 or not line.endswith(b"\n"):
            os._exit(2)
        try:
            frame = json.loads(line)
            if frame.get("protocol") != PROTOCOL or not isinstance(frame.get("request_id"), str) or not isinstance(frame.get("workspace_id"), str):
                raise ValueError("Malformed envelope")
        except (ValueError, TypeError, AttributeError):
            os._exit(2)
        if frame.get("action") == "cancel":
            operation_id = frame.get("data", {}).get("operation_id")
            with state_lock:
                accepted = operation_id not in state["completed"]
                if accepted:
                    state["cancelled"].add(operation_id)
                    runtime = state["runtime"]
                    if runtime and state["current"] == operation_id:
                        runtime.interrupt()
            respond(frame, {"cancel_requested": accepted, "accepted": accepted})
        else:
            jobs.put(frame)
    # Parent EOF: do not let uninterruptible UDFs retain orphan processes.
    jobs.put(None)
    thread.join(timeout=1)


if __name__ == "__main__":
    main()

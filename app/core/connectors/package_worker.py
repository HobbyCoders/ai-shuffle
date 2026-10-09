"""Connector worker protocol v1. Launched with an isolated plugin interpreter."""
import asyncio
import dataclasses
import importlib
import importlib.util
import json
import logging
import os
import sys
import threading
import types
import uuid
from pathlib import Path

OUTPUT = sys.stdout
sys.stdout = sys.stderr  # third-party prints must never corrupt the protocol
_WRITE_LOCK = threading.Lock()
_REPLIES = {}
_REPLY_LOCK = threading.Lock()
MAX_FRAME = 2_000_000
METHODS = {"connect", "disconnect", "send_message", "send_permission_prompt", "send_notification",
           "send_typing_indicator", "send_ack_reaction", "mark_as_read", "execute_action", "execute_tool",
           "get_action_availability", "request_action_approval", "handle_webhook_payload", "handle_webhook_activity"}


def emit(value):
    line = json.dumps(value, default=lambda o: dataclasses.asdict(o) if dataclasses.is_dataclass(o) else str(o))
    if len(line.encode()) > MAX_FRAME:
        raise ValueError("Connector response too large")
    with _WRITE_LOCK:
        OUTPUT.write(line + "\n")
        OUTPUT.flush()


def host_call(method, args):
    key = str(uuid.uuid4())
    event, result = threading.Event(), []
    with _REPLY_LOCK:
        _REPLIES[key] = (event, result)
    try:
        emit({"host_id": key, "method": method, "args": args})
        if not event.wait(180):
            raise TimeoutError("Host callback timed out")
        if result[0].get("error"):
            raise RuntimeError("Host rejected connector callback")
        return result[0].get("result")
    finally:
        with _REPLY_LOCK:
            _REPLIES.pop(key, None)


def sdk(package_path):
    # Do not import app's __init__, database client, or server configuration.
    for name in ("app", "app.core", "app.core.connectors", "app.db"):
        module = types.ModuleType(name)
        module.__path__ = [str(package_path)] if name == "app.core.connectors" else []
        sys.modules[name] = module
    base_path = Path(__file__).with_name("base.py")
    spec = importlib.util.spec_from_file_location("app.core.connectors.base", base_path)
    base = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = base
    spec.loader.exec_module(base)
    # Standard library only; the Signal, iMessage and BlueBubbles connectors import it.
    detached_path = Path(__file__).parent.parent / "detached_tasks.py"
    spec = importlib.util.spec_from_file_location("app.core.detached_tasks", detached_path)
    detached = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = detached
    spec.loader.exec_module(detached)
    process_tree = types.ModuleType("app.core.process_tree")

    def child_env(extra=None):
        # The host already limited this worker's environment (worker_environment).
        env = dict(os.environ)
        env.update(extra or {})
        if os.name == "nt":
            env.setdefault("PYTHONIOENCODING", "utf-8")
            env.setdefault("PYTHONUTF8", "1")
        return env

    process_tree.child_env = child_env
    sys.modules[process_tree.__name__] = process_tree
    db = types.ModuleType("app.db.database")
    db.update_connector_status = lambda *args, **kwargs: host_call("status", [list(args), kwargs])
    sys.modules[db.__name__] = db
    sys.modules["app.db"].database = db
    async_db = types.ModuleType("app.db.async_database")
    async def run_sync(fn, *args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)
    async_db.run_sync = run_sync
    sys.modules[async_db.__name__] = async_db
    return base


async def serve(cls, base):
    loop = asyncio.get_running_loop()
    instance = None
    tasks = set()
    async def callback(method, *args):
        return await asyncio.to_thread(host_call, method, args)
    async def dispatch(request):
        nonlocal instance
        key = request["id"]
        try:
            method, args, kwargs = request["method"], request.get("args", []), request.get("kwargs", {})
            if method == "init":
                instance = cls(**kwargs,
                    on_incoming_message=lambda message: callback("incoming", dataclasses.asdict(message)),
                    on_permission_response=lambda *a: callback("permission", *a),
                    on_trigger=lambda *a: callback("trigger", *a))
                result = True
            elif instance is not None and method in METHODS:
                conversion = {"send_message": (0, "message", base.OutgoingMessage),
                              "send_permission_prompt": (1, "prompt", base.PermissionPrompt),
                              "request_action_approval": (1, "action", base.ActionDef)}.get(method)
                if conversion:
                    index, name, model = conversion
                    if len(args) > index:
                        args[index] = model(**args[index])
                    elif name in kwargs:
                        kwargs[name] = model(**kwargs[name])
                result = await getattr(instance, method)(*args, **kwargs)
            else:
                raise ValueError("Unsupported connector request")
            emit({"id": key, "result": result,
                  "status": instance.status.value,
                  "instructions": instance.get_context_instructions("")})
        except Exception as exc:
            emit({"id": key, "error": f"Connector operation failed ({type(exc).__name__}). Check its configuration."})
    def schedule(value):
        task = asyncio.create_task(dispatch(value))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    stopped = asyncio.Event()
    def read():
        try:
            while True:
                line = sys.stdin.buffer.readline(MAX_FRAME + 1)
                if not line or len(line) > MAX_FRAME:
                    break
                value = json.loads(line)
                if "host_id" in value:
                    with _REPLY_LOCK:
                        pending = _REPLIES.get(value["host_id"])
                        if pending:
                            pending[1].append(value)
                            pending[0].set()
                else:
                    loop.call_soon_threadsafe(schedule, value)
        finally:
            loop.call_soon_threadsafe(stopped.set)
    threading.Thread(target=read, daemon=True).start()
    await stopped.wait()
    if instance:
        try:
            await asyncio.wait_for(instance.disconnect(), 5)
        except Exception:
            pass
    for task in tasks:
        task.cancel()


if __name__ == "__main__":
    path, module_name, class_name = sys.argv[1:4]
    base = sdk(Path(path))
    cls = getattr(importlib.import_module("app.core.connectors." + module_name), class_name)
    if not issubclass(cls, base.BaseConnector):
        raise TypeError("Invalid connector interface")
    if len(sys.argv) > 4 and sys.argv[4] == "validate":
        # Constructor and descriptor compatibility without opening any network.
        cls(connector_id="validation", connector_type="validation", config={}, on_incoming_message=None, on_permission_response=None)
        emit({"sdk": 1})
    else:
        logging.disable(logging.CRITICAL)
        asyncio.run(serve(cls, base))

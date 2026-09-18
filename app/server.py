"""System Cleanup Tool - Ingress Web 服务。"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid

from flask import Flask, jsonify, request, send_from_directory

import cleaner
from cleaner import CleanupItems, _sup, human_size

OPTIONS_FILE = os.environ.get("OPTIONS_FILE", "/data/options.json")
APP_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, static_folder=None)

# ---------------------------------------------------------------------------
# 全局任务状态
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()
_task: dict = {"running": False, "current": None, "results": [], "total_freed": 0,
               "started_at": None, "finished_at": None, "error": None}


def load_options() -> dict:
    defaults = {
        "purge_keep_days": 7,
        "journal_vacuum_size_mb": 100,
    }
    try:
        with open(OPTIONS_FILE, encoding="utf-8") as fh:
            defaults.update(json.load(fh))
    except (OSError, ValueError):
        pass
    return defaults


def probe_items(options: dict) -> list[dict]:
    """探测每个清理项的当前大小。单项失败不影响整体。"""
    items = []
    for item_id, spec in CleanupItems.REGISTRY.items():
        size = -1
        error = None
        try:
            size = spec["size_fn"]()
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
        if size < 0 and not error:
            error = "无法访问宿主机资源 (权限不足)"
        items.append({
            "id": item_id,
            "name": spec["name"],
            "checked": item_id in CleanupItems.DEFAULT_CHECKED,
            "size": size,
            "size_text": human_size(size) if size >= 0 else "未知",
            "param": spec["param_desc"](options),
            "danger": spec["danger"],
            "error": error,
        })
    return items


def get_disk_info() -> dict:
    """数据磁盘用量 (/data 所在分区), 不依赖 Supervisor API 版本。"""
    try:
        st = os.statvfs("/data")
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        return {"total": total, "used": total - free, "free": free}
    except OSError:
        return {"total": 0, "used": 0, "free": 0}


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return send_from_directory(os.path.join(APP_DIR, "static"), "index.html")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.get("/api/info")
def api_info():
    options = load_options()
    disk = get_disk_info()
    items = probe_items(options)
    return jsonify({"ok": True, "options": options, "disk": disk, "items": items})


@app.post("/api/clean")
def api_clean():
    body = request.get_json(silent=True) or {}
    selected = body.get("items", [])
    if not selected:
        return jsonify({"ok": False, "error": "未选择任何清理项"}), 400

    unknown = [i for i in selected if i not in CleanupItems.REGISTRY]
    if unknown:
        return jsonify({"ok": False, "error": f"未知清理项: {unknown}"}), 400

    with _state_lock:
        if _task["running"]:
            return jsonify({"ok": False, "error": "已有清理任务进行中"}), 409
        _task.update(
            running=True, current=None, current_name=None,
            selected_total=len(selected), results=[],
            total_freed=0, started_at=time.time(),
            finished_at=None, error=None,
        )

    thread = threading.Thread(target=_run_task, args=(selected,), daemon=True)
    thread.start()
    return jsonify({"ok": True})


@app.get("/api/status")
def api_status():
    with _state_lock:
        return jsonify(_task)


def _run_task(selected: list[str]) -> None:
    options = load_options()
    for item_id in selected:
        spec = CleanupItems.REGISTRY[item_id]
        with _state_lock:
            _task["current"] = item_id
            _task["current_name"] = spec["name"]
        try:
            result = spec["clean_fn"](options)
            entry = {"id": item_id, "name": spec["name"], "status": "done",
                     "freed": result.get("freed", 0), "msg": result.get("msg", "")}
        except Exception as exc:  # noqa: BLE001
            entry = {"id": item_id, "name": spec["name"], "status": "error",
                     "freed": 0, "msg": str(exc)}
        with _state_lock:
            _task["results"].append(entry)
            _task["total_freed"] += entry["freed"]

    with _state_lock:
        _task["running"] = False
        _task["current"] = None
        _task["current_name"] = None
        _task["finished_at"] = time.time()


if __name__ == "__main__":
    # waitress 生产级 WSGI 服务器, 替代 Flask 开发服务器
    from waitress import serve
    serve(app, host="0.0.0.0", port=8099, threads=8)

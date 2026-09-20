"""System Cleanup Tool - Ingress Web 服务。"""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, jsonify, request, send_from_directory

from cleaner import CleanupItems, human_size

OPTIONS_FILE = os.environ.get("OPTIONS_FILE", "/data/options.json")
LAST_RUN_FILE = os.environ.get("LAST_RUN_FILE", "/data/last_cleanup.json")
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


def _read_last_run() -> dict | None:
    try:
        with open(LAST_RUN_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _save_last_run(data: dict) -> None:
    try:
        with open(LAST_RUN_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
    except OSError:
        pass  # 持久化失败不影响清理本身


# 进程刚启动时不可能有进行中的任务:
# 文件若标记 running 说明上次清理被插件重启中断, 转为中断记录
_startup_last_run = _read_last_run()
if _startup_last_run and _startup_last_run.get("running"):
    _startup_last_run["running"] = False
    _startup_last_run["interrupted"] = True
    _save_last_run(_startup_last_run)


def _probe_one(item_id: str, spec: dict, options: dict) -> dict:
    """探测单个清理项, 任何异常都收敛为 error 字段, 不向上抛。"""
    size, error = -1, None
    try:
        size = spec["size_fn"]()
    except Exception as exc:  # noqa: BLE001
        error = str(exc) or exc.__class__.__name__
    if size < 0 and not error:
        error = "无法访问宿主机资源 (权限不足)"
    return {
        "id": item_id,
        "name": spec["name"],
        "checked": item_id in CleanupItems.DEFAULT_CHECKED,
        "size": size,
        "size_text": human_size(size) if size >= 0 else "未知",
        "param": spec["param_desc"](options),
        "danger": spec["danger"],
        "error": error,
    }


def probe_items(options: dict) -> list[dict]:
    """并行探测各清理项当前大小, 顺序与注册表一致。

    探测均为只读操作可安全并行; 总耗时由最慢一项决定而非逐项累加
    (journal/Docker 探测在宿主机繁忙时可能达到数十秒)。
    """
    registry = list(CleanupItems.REGISTRY.items())
    if not registry:
        return []
    with ThreadPoolExecutor(max_workers=len(registry)) as ex:
        futures = [
            (item_id, ex.submit(_probe_one, item_id, spec, options))
            for item_id, spec in registry
        ]
        return [fut.result() for _item_id, fut in futures]


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

# Ingress 可信代理地址（HA Supervisor 反代来源），写操作仅信任该来源
INGRESS_PROXY_ADDRESS = "172.30.32.2"


def _is_trusted_proxy(address):
    """校验请求来源是否为 Ingress 可信代理（兼容 IPv6 映射前缀）"""
    return isinstance(address, str) and address.replace("::ffff:", "", 1) == INGRESS_PROXY_ADDRESS


@app.before_request
def _require_ingress_for_writes():
    # 写操作仅信任 Supervisor 反代来源 IP, 防止同网络内容器伪造请求头直连触发清理
    if request.method == "POST" and not _is_trusted_proxy(request.remote_addr):
        return jsonify({"ok": False, "error": "仅允许通过 Home Assistant 入口访问该接口"}), 403
    return None


@app.get("/api/info")
def api_info():
    options = load_options()
    disk = get_disk_info()
    items = probe_items(options)
    return jsonify({"ok": True, "options": options, "disk": disk, "items": items,
                    "last_run": _read_last_run()})


@app.post("/api/clean")
def api_clean():
    body = request.get_json(silent=True) or {}
    selected = body.get("items", [])
    if not isinstance(selected, list) or not all(isinstance(i, str) for i in selected):
        return jsonify({"ok": False, "error": "参数 items 必须为字符串数组"}), 400
    if not selected:
        return jsonify({"ok": False, "error": "未选择任何清理项"}), 400
    selected = list(dict.fromkeys(selected))  # 去重, 防止同一项重复执行

    unknown = [i for i in selected if i not in CleanupItems.REGISTRY]
    if unknown:
        return jsonify({"ok": False, "error": f"未知清理项: {unknown}"}), 400

    # 危险项服务端二次确认: 前端 confirm 可被绕过, 必须显式携带确认标记才执行
    has_danger = any(CleanupItems.REGISTRY[i]["danger"] >= 2 for i in selected)
    if has_danger and body.get("confirm_danger") is not True:
        return jsonify({"ok": False, "error": "包含不可恢复的清理项, 需携带 confirm_danger 确认"}), 400

    with _state_lock:
        if _task["running"]:
            return jsonify({"ok": False, "error": "已有清理任务进行中"}), 409
        _task.update(
            running=True, current=None, current_name=None,
            selected_total=len(selected), results=[],
            total_freed=0, started_at=time.time(),
            finished_at=None, error=None,
        )
        _save_last_run({
            "running": True,
            "started_at": _task["started_at"],
            "selected_total": len(selected),
        })

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
        _save_last_run({
            "running": False,
            "interrupted": False,
            "started_at": _task["started_at"],
            "finished_at": _task["finished_at"],
            "selected_total": _task["selected_total"],
            "results": _task["results"],
            "total_freed": _task["total_freed"],
        })


if __name__ == "__main__":
    # waitress 生产级 WSGI 服务器, 替代 Flask 开发服务器
    from waitress import serve
    serve(app, host="0.0.0.0", port=8099, threads=8)

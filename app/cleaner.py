"""System Cleanup Tool - 清理逻辑实现。

包含对 Home Assistant 数据库、日志、journal、备份、缓存等目标的
大小探测与清理。所有清理函数返回统一结构:
    {"freed": <释放字节数>, "msg": "<人类可读结果>"}
失败时抛出 Exception，由上层捕获并记录为单项失败。
"""

from __future__ import annotations

import glob
import http.client
import json
import os
import shutil
import socket
import subprocess
import time

import requests

SUPERVISOR_URL = os.environ.get("SUPERVISOR_URL", "http://supervisor")
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

def _detect_config_dir() -> str:
    """探测 HA 配置目录在容器内的挂载点。

    新版 Supervisor 的 homeassistant_config 映射到 /homeassistant,
    旧键 config 或自定义 path 时为 /config。以 configuration.yaml 为标志。
    """
    for cand in ("/homeassistant", "/config"):
        if os.path.isfile(os.path.join(cand, "configuration.yaml")):
            return cand
    return "/homeassistant"


CONFIG_DIR = os.environ.get("CONFIG_DIR") or _detect_config_dir()

# host_pid: true 时, 容器内通过 /proc/1/root 可直接访问宿主机文件系统
HOST_ROOT = "/proc/1/root"

DB_PATH = os.path.join(CONFIG_DIR, "home-assistant_v2.db")
CORE_LOG = os.path.join(CONFIG_DIR, "home-assistant.log")


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def _sup(method: str, path: str, json_body: dict | None = None, timeout: int = 120) -> dict:
    """调用 Supervisor API。"""
    headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
    resp = requests.request(
        method,
        f"{SUPERVISOR_URL}{path}",
        headers=headers,
        json=json_body,
        timeout=timeout,
    )
    resp.raise_for_status()
    if resp.content:
        return resp.json()
    return {}


def _nsenter(args: list[str], timeout: int = 120) -> str:
    """进入宿主机(PID 1)的 mount namespace 并以宿主机根文件系统执行命令。

    需要 add-on 开启 full_access 与 host_pid。返回 stdout 文本。
    """
    cmd = ["nsenter", "-t", "1", "-m", "-r", "--"] + args
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"命令失败: {args[0]}")
    return proc.stdout.strip()


def _path_size(path: str) -> int:
    """文件或目录的大小(字节)。不存在返回 0。"""
    if os.path.isfile(path) or os.path.islink(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    if os.path.isdir(path):
        total = 0
        for root, _dirs, files in os.walk(path, onerror=lambda _e: None):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
        return total
    return 0


def _files_size(patterns: list[str]) -> int:
    total = 0
    for pattern in patterns:
        for f in glob.glob(pattern):
            total += _path_size(f)
    return total


def _truncate(path: str) -> None:
    """清空文件内容(保留 inode, 正在被写的日志也安全)。"""
    with open(path, "r+b") as fh:
        fh.truncate(0)


def _clean_dir_contents(path: str) -> int:
    """清空目录内容但保留目录本身。返回释放字节数。"""
    freed = _path_size(path)
    if not os.path.isdir(path):
        return 0
    for entry in os.listdir(path):
        full = os.path.join(path, entry)
        try:
            if os.path.isdir(full) and not os.path.islink(full):
                shutil.rmtree(full)
            else:
                os.remove(full)
        except OSError:
            pass
    return freed


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0:
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024.0
    return f"{num:.1f} PB"


# ---------------------------------------------------------------------------
# 1. 历史数据库 (recorder)
# ---------------------------------------------------------------------------

def recorder_size() -> int:
    size = _path_size(DB_PATH)
    size += _path_size(DB_PATH + "-wal")
    size += _files_size([DB_PATH + ".backup*", DB_PATH + ".corrupt*"])
    return size


def clean_recorder(keep_days: int) -> dict:
    """通过 HA 官方 recorder.purge 服务清理历史数据并 repack 压缩数据库。

    keep_days: 保留最近 N 天的历史记录, 0 表示全部清理。
    """
    before = recorder_size()

    # 删除历史迁移遗留的备份文件
    backup_freed = 0
    for pattern in (DB_PATH + ".backup*", DB_PATH + ".corrupt*"):
        for f in glob.glob(pattern):
            backup_freed += _path_size(f)
            try:
                os.remove(f)
            except OSError:
                pass

    # 调用 Home Assistant 服务 (经 Supervisor 代理)
    try:
        _sup(
            "POST",
            "/core/api/services/recorder/purge",
            json_body={"keep_days": keep_days, "repack": True},
            timeout=60,
        )
    except requests.RequestException as exc:
        raise RuntimeError(
            f"调用 Home Assistant recorder.purge 服务失败: {exc}"
        ) from exc

    # purge 在 HA 后台执行, 轮询数据库文件大小直至稳定 (最多 30 分钟)
    last = -1
    stable = 0
    for i in range(360):
        time.sleep(5)
        current = _path_size(DB_PATH) + _path_size(DB_PATH + "-wal")
        if current == last:
            stable += 1
        else:
            stable = 0
        last = current
        if i >= 12 and stable >= 6:  # 至少 1 分钟后, 连续 30 秒无变化视为完成
            break

    freed = before - last
    return {
        "freed": max(freed, 0) + backup_freed,
        "msg": f"已清理 {keep_days} 天前的历史记录并压缩数据库"
        if keep_days > 0
        else "已清空全部历史记录并压缩数据库",
    }


# ---------------------------------------------------------------------------
# 2. ESPhome 构建缓存 / Zigbee2MQTT 日志
# ---------------------------------------------------------------------------

def esphome_cache_size() -> int:
    """ESPhome 固件编译中间产物 (esphome/.esphome/build)。"""
    return _path_size(os.path.join(CONFIG_DIR, "esphome", ".esphome", "build"))


def clean_esphome_cache() -> dict:
    path = os.path.join(CONFIG_DIR, "esphome", ".esphome", "build")
    freed = _clean_dir_contents(path)
    return {"freed": freed, "msg": "已清空 ESPhome 构建缓存 (下次编译自动重建)"}


def z2m_logs_size() -> int:
    """Zigbee2MQTT 运行日志 (不含设备数据库)。"""
    return _path_size(os.path.join(CONFIG_DIR, "zigbee2mqtt", "log"))


def clean_z2m_logs() -> dict:
    path = os.path.join(CONFIG_DIR, "zigbee2mqtt", "log")
    freed = _clean_dir_contents(path)
    return {"freed": freed, "msg": "已清空 Zigbee2MQTT 日志"}


# ---------------------------------------------------------------------------
# 3. HA Core 日志文件
# ---------------------------------------------------------------------------

def core_logs_size() -> int:
    """HA Core 日志: 主日志 + 轮转/故障日志 (home-assistant.log.1 等)。

    HAOS 上 Core 日志同时写入 journald (由 journal 清理项管理),
    这里只处理配置目录下的日志文件。
    """
    return _path_size(CORE_LOG) + _files_size([CORE_LOG + ".*"])


def clean_core_logs() -> dict:
    before = core_logs_size()
    if before == 0:
        return {"freed": 0, "msg": "没有可清理的 Core 日志文件"}

    # 轮转与故障日志文件直接删除
    for f in glob.glob(CORE_LOG + ".*"):
        try:
            os.remove(f)
        except OSError:
            pass

    # 主日志清空但保留 inode, 正在被写的日志也安全
    if os.path.isfile(CORE_LOG):
        try:
            _truncate(CORE_LOG)
        except OSError:
            pass

    after = core_logs_size()
    return {"freed": max(before - after, 0), "msg": "已清空 HA Core 日志文件"}


# ---------------------------------------------------------------------------
# 4. 系统 journal 日志 (宿主机)
# ---------------------------------------------------------------------------

def journal_size() -> int:
    """宿主机 journal 大小(持久 + volatile)。失败返回 -1。

    优先通过 /proc/1/root 直接统计(host_pid), 失败回退 nsenter。
    """
    for base in (HOST_ROOT, ""):
        paths = [
            os.path.join(base, "var/log/journal"),
            os.path.join(base, "run/log/journal"),
        ]
        if any(os.path.isdir(p) for p in paths):
            return sum(_path_size(p) for p in paths if os.path.isdir(p))
    try:
        out = _nsenter(
            [
                "sh",
                "-c",
                "du -sk /var/log/journal /run/log/journal 2>/dev/null"
                " | awk '{s+=$1} END {print s+0}'",
            ],
            timeout=30,
        )
        return int(out.split()[0]) * 1024
    except Exception:
        return -1


def clean_journal(vacuum_size_mb: int) -> dict:
    before = journal_size()
    if before < 0:
        raise RuntimeError("无法定位宿主机 journal 目录 (需要 host_pid 权限)")

    # journalctl 是宿主机工具, 通过 nsenter 以宿主机环境执行
    try:
        out = _nsenter(["sh", "-c", "command -v journalctl"], timeout=30)
        journalctl = out.splitlines()[0] if out else "/usr/bin/journalctl"
    except Exception:
        journalctl = "/usr/bin/journalctl"

    _nsenter(
        [journalctl, f"--vacuum-size={vacuum_size_mb}M"],
        timeout=300,
    )

    after = journal_size()
    freed = max(before - after, 0)
    return {
        "freed": freed,
        "msg": f"已将系统 journal 压缩至 {vacuum_size_mb} MB 以内"
        f" (当前 {human_size(after)})",
    }


# ---------------------------------------------------------------------------
# Docker 悬空镜像 (通过 docker_api 的 unix socket)
# ---------------------------------------------------------------------------

DOCKER_SOCK = "/var/run/docker.sock"


class _UnixHTTPConnection(http.client.HTTPConnection):
    """通过 unix socket 连接 Docker API 的 HTTP 连接 (无外部依赖)。"""

    def __init__(self, socket_path: str, timeout: float = 60) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


def _docker_request(method: str, path: str, timeout: int = 120) -> str:
    """调用 Docker API (unix socket), 返回响应体文本。

    相比 curl 子进程: 不依赖镜像内 curl, 且能识别 HTTP 错误状态码
    (如删除镜像被拒时的 409), 失败信息更明确。
    """
    conn = _UnixHTTPConnection(DOCKER_SOCK, timeout=timeout)
    try:
        # 注意: http.client 不支持绝对 URL (不会像 curl 那样剥离 scheme/host),
        # 必须直接传 path, 否则请求行会成为代理形式的绝对 URI
        conn.request(method, path)
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", "replace")
        if resp.status >= 400:
            raise RuntimeError(f"Docker API {method} {path} 失败: HTTP {resp.status}")
        return body
    except OSError as exc:
        raise RuntimeError(f"无法连接 Docker API ({exc})") from exc
    finally:
        conn.close()


def _dangling_images() -> list[dict]:
    out = _docker_request(
        "GET", "/images/json?filters=%7B%22dangling%22%3A%5B%22true%22%5D%7D",
        timeout=60,
    )
    return json.loads(out or "null") or []


def _build_cache_size() -> int:
    """未使用的 Docker 构建缓存 (本地构建插件的中间层)。"""
    out = _docker_request("GET", "/system/df", timeout=60)
    df = json.loads(out or "null") or {}
    caches = df.get("BuildCache") or []
    return sum(int(c.get("Size") or 0) for c in caches if not c.get("InUse"))


def docker_images_size() -> int:
    """悬空(<none>)镜像 + 未使用构建缓存。无法访问 Docker API 时返回 -1。"""
    if not os.path.exists(DOCKER_SOCK):
        return -1
    try:
        return sum(int(img.get("Size") or 0) for img in _dangling_images()) + _build_cache_size()
    except Exception:
        return -1


def clean_docker_images() -> dict:
    before = docker_images_size()
    if before < 0:
        raise RuntimeError("无法访问 Docker API (需要 docker_api 权限)")

    # 1) 删除悬空镜像 (插件更新遗留); 单个失败(被引用 409 等)只跳过不中断
    images = _dangling_images()
    freed = 0
    removed = 0
    for img in images:
        image_id = (img.get("Id") or "").split(":")[-1]  # sha256:xxx -> xxx
        try:
            _docker_request("DELETE", f"/images/{image_id}", timeout=180)
            freed += int(img.get("Size") or 0)
            removed += 1
        except Exception:  # noqa: BLE001
            continue

    # 2) 清理未使用的构建缓存 (本地构建插件的中间层)
    cache_freed = 0
    try:
        out = _docker_request("POST", "/build/prune?all=1", timeout=300)
        result = json.loads(out or "null") or {}
        cache_freed = int(result.get("SpaceReclaimed") or 0)
    except Exception:
        pass

    if not removed and not cache_freed:
        return {"freed": 0, "msg": "没有可清理的悬空镜像与构建缓存"}
    parts = []
    if removed:
        parts.append(f"{removed} 个悬空镜像")
    if cache_freed:
        parts.append("构建缓存")
    return {
        "freed": freed + cache_freed,
        "msg": f"已清理 {'与'.join(parts)}",
    }


# 注: HAOS 的 Docker 日志驱动为 journald, 容器(插件/HA Core)日志全部
# 写入 systemd journal, 由上方 journal 清理项统一管理, 无独立日志文件可清。

# ---------------------------------------------------------------------------
# 清理项注册表
# ---------------------------------------------------------------------------

class CleanupItems:
    """清理项注册表: id -> (名称, 探测函数, 清理函数, 参数来源)。"""

    REGISTRY = {
        "recorder_db": {
            "name": "历史数据库",
            "size_fn": recorder_size,
            "clean_fn": lambda opts: clean_recorder(opts["purge_keep_days"]),
            "param_desc": lambda opts: (
                f"Home Assistant 传感器历史与状态记录, 保留最近 {opts['purge_keep_days']} 天"
                if opts["purge_keep_days"] > 0
                else "Home Assistant 传感器历史与状态记录, 清空全部历史"
            ),
            "danger": 1,
        },
        "journal": {
            "name": "HAOS 系统日志",
            "size_fn": journal_size,
            "clean_fn": lambda opts: clean_journal(opts["journal_vacuum_size_mb"]),
            "param_desc": lambda opts: f"HAOS 系统、各系统组件及全部插件(含 HA Core)的运行日志, 压缩至 {opts['journal_vacuum_size_mb']} MB 以内",
            "danger": 0,
        },
        "core_logs": {
            "name": "HA Core 日志",
            "size_fn": core_logs_size,
            "clean_fn": lambda opts: clean_core_logs(),
            "param_desc": lambda opts: "Home Assistant Core 运行日志文件及轮转文件, 不含历史数据库",
            "danger": 0,
        },
        "esphome_cache": {
            "name": "ESPhome 构建缓存",
            "size_fn": esphome_cache_size,
            "clean_fn": lambda opts: clean_esphome_cache(),
            "param_desc": lambda opts: "ESPHome 编译固件产生的中间文件, 下次编译自动重建",
            "danger": 1,
        },
        "z2m_logs": {
            "name": "Zigbee2MQTT 日志",
            "size_fn": z2m_logs_size,
            "clean_fn": lambda opts: clean_z2m_logs(),
            "param_desc": lambda opts: "Zigbee2MQTT 网关运行日志, 不含设备数据库",
            "danger": 0,
        },
        "docker_images": {
            "name": "Docker 未使用镜像与构建缓存",
            "size_fn": docker_images_size,
            "clean_fn": lambda opts: clean_docker_images(),
            "param_desc": lambda opts: "插件更新与本地构建遗留的镜像层和编译缓存",
            "danger": 1,
        },
    }

    DEFAULT_CHECKED = [
        "recorder_db",
        "journal",
        "core_logs",
        "esphome_cache",
        "z2m_logs",
        "docker_images",
    ]

# -*- coding: utf-8 -*-
"""AstrBot 插件：Bot Console —— 以 Bot 为发送方视角的 WebUI 聊天控制台。

设计要点（与实现强相关，改代码前请先读这一节）
========================================================================

1. 会话寻址只认 ``unified_msg_origin``（下称 UMO）
   UMO 的格式是 ``platform_id:message_type:session_id``，其中第一段是
   **平台适配器的 id**（不是 name），中间段是 ``GroupMessage`` /
   ``FriendMessage``，最后一段对 QQ 官方机器人来说就是 ``group_openid``
   或用户 ``openid``。插件把这个字符串当主键持久化，用户在 Web 端只看到
   「备注名」，永远不需要手填 openid。

2. 不使用 ``send_message_by_id`` / ``create_event`` 作为主发送路径
   这两者在 AstrBot 核心里对 ``AiocqhttpAdapter`` 硬编码，对
   ``qq_official`` 不是通用接口。主路径固定为::

       await self.context.send_message(umo, MessageChain().message(text))

3. **QQ 官方群聊主动推送的静默失败陷阱（本插件存在的主要理由之一）**
   ``QQOfficialPlatformAdapter.send_by_session()`` 只有在
   ``self._session_scene[session_id] == "group"`` 时才会真正调
   ``post_group_message``；否则它只打一条 warning 然后 ``return``。
   注意：它 **不抛异常**，而 ``context.send_message()`` 只要找到平台实例就
   返回 ``True``。于是「看起来发送成功、群里什么都没有」是默认行为。
   而 ``_session_scene`` 是**进程内存里的 dict**，AstrBot 一重启就空了。
   因此每次发送前都必须补一次 ``remember_session_scene(target_id, "group")``，
   见 :meth:`BotConsolePlugin._ensure_scene`。

4. 兜底发送路径
   当主路径不可用（适配器声明不许主动发群消息）或抛异常时，直接调用
   ``platform.client.api.post_group_message(...)``。这里复用 botpy 已有的
   HTTP 客户端，因此 access_token 刷新、重试、签名都由 botpy 负责，
   不需要自己拼 ``Authorization`` 头。

5. 会话自动登记
   全局监听 ``EventMessageType.ALL``，把收到的每个会话 upsert 进
   ``conversations.json``，``alias`` 初始为空。备注名由目标会话里的
   ``/bind <备注名>`` 命令绑定。之所以必须靠「收到消息」来学习
   openid：同一个 QQ 群在不同 Bot 下拿到的 ``group_openid`` 是不同的，
   无法由群号推导。

6. 频控与审计
   QQ 官方认证机器人主动消息限制约为 60 条/分钟、单群每天 1000 条。
   插件侧只做本地计数并在 UI 上告警，**不阻塞**发送——最终以 API 返回为准。
   每次发送（含模拟）都写一行 JSON 到 ``audit.jsonl``。

数据目录：``data/plugin_data/astrbot_plugin_bot_console/``
    - ``conversations.json``  会话登记表
    - ``audit.jsonl``         发送审计日志（每行一个 JSON）
    - ``quota.json``          按天、按会话的发送计数
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import error_response, json_response, request

PLUGIN_NAME = "astrbot_plugin_bot_console"
PLUGIN_VERSION = "1.0.1"
API_PREFIX = f"/{PLUGIN_NAME}"

#: 被视为「QQ 官方机器人」的适配器 name，兜底与场景补登记只对它们生效。
QQ_OFFICIAL_PLATFORMS = frozenset({"qq_official", "qq_official_webhook"})

SCENE_GROUP = "group"
SCENE_PRIVATE = "private"
SCENE_OTHER = "other"

#: 频控窗口（秒）与默认上限，可在插件配置里覆盖。
QUOTA_WINDOW_SECONDS = 60
DEFAULT_QPM_LIMIT = 60
DEFAULT_DAILY_LIMIT = 1000

#: 审计日志里正文预览的最大长度。
PREVIEW_LIMIT = 120
#: 错误文本进 UI 前的最大长度，避免把整段 stack/HTML 贴到浏览器里。
ERROR_LIMIT = 400
#: conversations.json 落盘的最小间隔（秒），挡住刷屏消息导致的频繁写盘。
SAVE_THROTTLE_SECONDS = 3.0

_MESSAGE_TYPE_2_SCENE = {
    "GroupMessage": SCENE_GROUP,
    "FriendMessage": SCENE_PRIVATE,
}


# ---------------------------------------------------------------------------
# 纯函数小工具
# ---------------------------------------------------------------------------


def _now() -> int:
    """当前 Unix 时间戳（秒，整数）。"""
    return int(time.time())


def _today() -> str:
    """本地日期，用作按天计数的 key。"""
    return datetime.now().strftime("%Y-%m-%d")


def _format_time(ts: Any) -> str:
    """把时间戳格式化成 ``YYYY-MM-DD HH:MM:SS``，非法输入返回空串。"""
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _collapse(text: Any) -> str:
    """把任意文本折成单行，去掉多余空白。"""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _preview(text: Any, limit: int = PREVIEW_LIMIT) -> str:
    """生成用于列表/审计展示的正文预览。"""
    collapsed = _collapse(text)
    if len(collapsed) > limit:
        return collapsed[: limit - 1] + "…"
    return collapsed


def _sanitize_error(exc: Any, limit: int = ERROR_LIMIT) -> str:
    """把异常压成适合回给浏览器的一行文本。

    原始异常常常带完整 URL、HTML 片段或换行，直接回显既难读也泄露内部信息。
    """
    collapsed = _collapse(exc)
    if len(collapsed) > limit:
        collapsed = collapsed[: limit - 1] + "…"
    return collapsed or "未知错误"


def _scene_from_umo(umo: str) -> str:
    """从 UMO 推断场景。无法识别时返回 ``other``。"""
    parts = str(umo or "").split(":", 2)
    if len(parts) != 3:
        return SCENE_OTHER
    return _MESSAGE_TYPE_2_SCENE.get(parts[1], SCENE_OTHER)


def _message_type_from_scene(scene: str) -> str:
    """场景反查 ``MessageType.value``，用于伪造事件。"""
    if scene == SCENE_GROUP:
        return "GroupMessage"
    if scene == SCENE_PRIVATE:
        return "FriendMessage"
    return "OtherMessage"


def _read_text(path: Path) -> str:
    with path.open("r", encoding="utf-8") as handle:
        return handle.read()


def _write_text_atomic(path: Path, text: str) -> None:
    """先写临时文件再原子替换，避免断电/崩溃留下半个 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# 会话登记表
# ---------------------------------------------------------------------------


class ConversationStore:
    """``conversations.json`` 的读写封装。

    结构::

        {
          "version": 1,
          "updated_at": 1730000000,
          "conversations": {
            "<umo>": {
              "unified_msg_origin": "...",
              "alias": "高三(2)班通知群",
              "platform": "qq_official",
              "platform_id": "qq_official_1",
              "scene": "group",
              "target_id": "GROUP_OPENID",
              "last_seen_at": 1730000000,
              "last_message_id": "....",
              "self_id": "....",
              "last_sender_name": "张三",
              "last_preview": "……",
              "message_count": 12,
              "note": ""
            }
          }
        }

    以 UMO 为主键（同一个 UMO 唯一），**不用群号**——不同 Bot 拿到的
    同一个群的 group_openid 不同，群号根本无法反查。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._items: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._loaded = False
        self._last_save = 0.0

    # -- 载入 / 落盘 ---------------------------------------------------------

    async def load(self) -> None:
        """从磁盘读取登记表；文件不存在或损坏时以空表启动。"""
        async with self._lock:
            if self._loaded:
                return
            self._loaded = True
            try:
                raw = await asyncio.to_thread(_read_text, self.path)
            except FileNotFoundError:
                return
            except OSError:
                return
            try:
                payload = json.loads(raw) if raw.strip() else {}
            except (json.JSONDecodeError, ValueError):
                # 损坏的登记表不应该让插件起不来；保留坏文件供人工排查。
                try:
                    await asyncio.to_thread(
                        os.replace, self.path, self.path.with_suffix(".corrupt.json")
                    )
                except OSError:
                    pass
                return
            items = payload.get("conversations") if isinstance(payload, dict) else None
            if isinstance(items, dict):
                for umo, record in items.items():
                    if isinstance(record, dict) and isinstance(umo, str) and umo:
                        self._items[umo] = dict(record)
            elif isinstance(items, list):
                for record in items:
                    if isinstance(record, dict):
                        umo = str(record.get("unified_msg_origin") or "")
                        if umo:
                            self._items[umo] = dict(record)

    async def flush(self, *, force: bool = False) -> None:
        """落盘。默认受 ``SAVE_THROTTLE_SECONDS`` 节流。"""
        now = time.monotonic()
        if not force and now - self._last_save < SAVE_THROTTLE_SECONDS:
            return
        async with self._lock:
            now = time.monotonic()
            if not force and now - self._last_save < SAVE_THROTTLE_SECONDS:
                return
            self._last_save = now
            payload = {
                "version": 1,
                "updated_at": _now(),
                "conversations": list(self._items.values()),
            }
            text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            try:
                await asyncio.to_thread(_write_text_atomic, self.path, text)
            except OSError:
                # 写盘失败只影响持久化，不应该打断消息处理。
                pass

    # -- 查询 ---------------------------------------------------------------

    def get(self, umo: str) -> dict[str, Any] | None:
        record = self._items.get(umo)
        return dict(record) if record else None

    def all(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._items.values()]

    def __contains__(self, umo: object) -> bool:
        return isinstance(umo, str) and umo in self._items

    # -- 变更 ---------------------------------------------------------------

    def upsert_seen(
        self,
        *,
        umo: str,
        platform_name: str,
        platform_id: str,
        scene: str,
        target_id: str,
        message_id: str = "",
        self_id: str = "",
        sender_name: str = "",
        preview: str = "",
    ) -> None:
        """登记一次「看见了这条会话」，保留已有 alias / note。"""
        record = self._items.get(umo)
        if record is None:
            record = {
                "unified_msg_origin": umo,
                "alias": "",
                "platform": platform_name,
                "platform_id": platform_id,
                "scene": scene,
                "target_id": target_id,
                "first_seen_at": _now(),
                "last_seen_at": _now(),
                "last_message_id": "",
                "self_id": "",
                "last_sender_name": "",
                "last_preview": "",
                "message_count": 0,
                "note": "",
            }
            self._items[umo] = record
        record["platform"] = platform_name or record.get("platform", "")
        record["platform_id"] = platform_id or record.get("platform_id", "")
        record["scene"] = scene or record.get("scene", SCENE_OTHER)
        record["target_id"] = target_id or record.get("target_id", "")
        record["last_seen_at"] = _now()
        if message_id:
            record["last_message_id"] = message_id
        if self_id:
            record["self_id"] = self_id
        if sender_name:
            record["last_sender_name"] = sender_name
        if preview:
            record["last_preview"] = preview
        record["message_count"] = int(record.get("message_count") or 0) + 1

    def bind(self, umo: str, alias: str, note: str | None = None) -> dict[str, Any] | None:
        """给已登记的会话绑定备注名（以及可选的备注）。"""
        record = self._items.get(umo)
        if record is None:
            return None
        record["alias"] = alias
        if note is not None:
            record["note"] = note
        record["renamed_at"] = _now()
        return dict(record)

    def remove(self, umo: str) -> bool:
        return self._items.pop(umo, None) is not None

    def sorted_records(self) -> list[dict[str, Any]]:
        """按「有备注名的在前，再按最近活跃倒序」排序。"""
        records = [dict(item) for item in self._items.values()]
        records.sort(
            key=lambda item: (
                1 if str(item.get("alias") or "").strip() else 0,
                int(item.get("last_seen_at") or 0),
            ),
            reverse=True,
        )
        return records


# ---------------------------------------------------------------------------
# 审计日志
# ---------------------------------------------------------------------------


class AuditLog:
    """``audit.jsonl``：每次发送写一行 JSON，便于 grep 与外部归档。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    async def append(self, entry: dict[str, Any]) -> None:
        line = json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
        async with self._lock:
            try:
                await asyncio.to_thread(self._append_sync, line)
            except OSError:
                # 审计写失败不应让发送本身失败，但要在插件日志里留痕。
                raise

    def _append_sync(self, line: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)

    async def tail(self, limit: int = 200) -> list[dict[str, Any]]:
        """读取最近的 ``limit`` 条记录（最新的在前）。"""
        try:
            raw = await asyncio.to_thread(_read_text, self.path)
        except (FileNotFoundError, OSError):
            return []
        lines = [line for line in raw.splitlines() if line.strip()]
        entries: list[dict[str, Any]] = []
        for line in reversed(lines[-max(1, limit) :]):
            try:
                payload = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(payload, dict):
                entries.append(payload)
        return entries

    async def trim(self, retention_days: int) -> int:
        """删除超过保留期的记录，返回被删掉的行数。"""
        if retention_days <= 0:
            return 0
        cutoff = _now() - retention_days * 86400
        try:
            raw = await asyncio.to_thread(_read_text, self.path)
        except (FileNotFoundError, OSError):
            return 0
        kept: list[str] = []
        dropped = 0
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                ts = int(payload.get("ts") or 0)
            except (json.JSONDecodeError, ValueError, TypeError, AttributeError):
                kept.append(line)
                continue
            if ts and ts < cutoff:
                dropped += 1
                continue
            kept.append(line)
        if not dropped:
            return 0
        text = "\n".join(kept) + "\n" if kept else ""
        try:
            await asyncio.to_thread(_write_text_atomic, self.path, text)
        except OSError:
            return 0
        return dropped


# ---------------------------------------------------------------------------
# 插件主体
# ---------------------------------------------------------------------------


class BotConsolePlugin(Star):
    """Bot Console 控制台插件。

    继承 ``Star`` 即可被 AstrBot 自动识别，无需 ``@register`` 装饰器
    （该装饰器在 4.x 已标记废弃）。元数据来自 ``metadata.yaml``。
    """

    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context, config)
        self.config = config if config is not None else {}
        self._closing = False

        # -- 数据目录与存储 --------------------------------------------------
        data_dir = self._resolve_data_dir()
        self._data_dir = data_dir
        self.store = ConversationStore(data_dir / "conversations.json")
        self.audit = AuditLog(data_dir / "audit.jsonl")
        self._quota_path = data_dir / "quota.json"

        # -- 频控计数的内存态 ------------------------------------------------
        self._quota_window: dict[str, list[float]] = {}
        self._quota_daily: dict[str, dict[str, int]] = {}
        self._quota_dirty = False

        # -- 后台任务 --------------------------------------------------------
        self._init_task: asyncio.Task | None = None
        self._flush_task: asyncio.Task | None = None

        # -- Web API ---------------------------------------------------------
        self._register_web_apis()

    # ------------------------------------------------------------------
    # 配置读取
    # ------------------------------------------------------------------

    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def _cfg_bool(self, key: str, default: bool = False) -> bool:
        value = self._cfg(key, default)
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "是")
        return bool(value)

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self._cfg(key, default))
        except (TypeError, ValueError):
            return default

    def _cfg_float(self, key: str, default: float) -> float:
        try:
            return float(self._cfg(key, default))
        except (TypeError, ValueError):
            return default

    # ------------------------------------------------------------------
    # 数据目录 / 首次载入
    # ------------------------------------------------------------------

    def _resolve_data_dir(self) -> Path:
        """定位插件数据目录，失败时退回到插件自身目录下的 ``data/``。"""
        try:
            return Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            fallback = Path(__file__).resolve().parent / "data"
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback

    async def _ensure_loaded(self) -> None:
        """幂等初始化：载入登记表、频控计数，并按保留期裁剪审计日志。"""
        if self._init_task is None:
            self._init_task = asyncio.create_task(self._bootstrap())
        try:
            await self._init_task
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.error(f"[BotConsole] 初始化失败：{_sanitize_error(exc)}")

    async def _bootstrap(self) -> None:
        await self.store.load()
        await self._load_quota()
        trimmed = await self.audit.trim(self._cfg_int("audit_retention_days", 90))
        if trimmed:
            self.logger.info(f"[BotConsole] 审计日志已裁剪 {trimmed} 条过期记录")
        self.logger.info(
            f"[BotConsole] 已载入 {len(self.store.all())} 个已登记会话，"
            f"数据目录 {self._data_dir}"
        )

    async def _load_quota(self) -> None:
        try:
            raw = await asyncio.to_thread(_read_text, self._quota_path)
        except (FileNotFoundError, OSError):
            return
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except (json.JSONDecodeError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        day = _today()
        counts = payload.get(day)
        if isinstance(counts, dict):
            self._quota_daily = {day: {str(k): int(v) for k, v in counts.items() if str(v).isdigit()}}
        else:
            # 跨天重启：只保留今天的计数，历史的丢掉了也不影响判断。
            self._quota_daily = {}

    async def _save_quota(self) -> None:
        if not self._quota_dirty:
            return
        self._quota_dirty = False
        payload = {"date": _today(), "counts": self._quota_daily.get(_today(), {})}
        try:
            await asyncio.to_thread(
                _write_text_atomic,
                self._quota_path,
                json.dumps(payload, ensure_ascii=False, indent=2),
            )
        except OSError:
            pass

    async def _flush_loop(self) -> None:
        """后台低频落盘，避免每条消息都写文件。"""
        while True:
            try:
                await asyncio.sleep(SAVE_THROTTLE_SECONDS)
                await self.store.flush(force=True)
                await self._save_quota()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 落盘异常不应该让任务退出
                self.logger.warning(f"[BotConsole] 后台落盘异常：{_sanitize_error(exc)}")

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        await self._ensure_loaded()
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_loop())
        self.logger.info("[BotConsole] Bot 控制台已就绪")

    async def initialize(self) -> None:
        await self._ensure_loaded()

    async def terminate(self) -> None:
        self._closing = True
        if self._flush_task is not None:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            self._flush_task = None
        try:
            await self.store.flush(force=True)
            await self._save_quota()
        except Exception:
            pass
        self.logger.info("[BotConsole] 插件已卸载")

    # ------------------------------------------------------------------
    # 平台适配器辅助
    # ------------------------------------------------------------------

    def _find_platform(self, umo: str) -> Any:
        """按 UMO 的第一段（platform_id）找平台适配器实例。"""
        platform_id = str(umo or "").split(":", 1)[0]
        if not platform_id:
            return None
        try:
            instances = self.context.platform_manager.platform_insts
        except Exception:
            return None
        for platform in instances:
            try:
                meta = platform.meta()
            except Exception:
                continue
            if str(getattr(meta, "id", "") or "") == platform_id:
                return platform
        return None

    def _platform_name(self, platform: Any) -> str:
        if platform is None:
            return ""
        try:
            return str(getattr(platform.meta(), "name", "") or "")
        except Exception:
            return ""

    def _ensure_scene(self, platform: Any, scene: str, target_id: str) -> bool:
        """让 QQ 官方适配器把该会话登记成目标场景。

        这是本插件最关键的一处补丁。``qq_official`` 的
        ``send_by_session`` 在发群主动消息前会检查内存里的
        ``_session_scene[session_id]`` 是否等于 ``"group"``，不满足就只打
        warning 然后 return——既不发消息也不抛异常。这个 dict 在 AstrBot
        重启后是空的，所以必须每次发送前补登记。

        Returns:
            是否成功登记（适配器没有该方法时返回 ``False``）。
        """
        remember = getattr(platform, "remember_session_scene", None)
        if not callable(remember):
            return False
        if not scene or not target_id or scene == SCENE_OTHER:
            return False
        try:
            remember(target_id, scene)
            return True
        except Exception as exc:
            self.logger.warning(
                f"[BotConsole] 登记会话场景失败：{_sanitize_error(exc)}"
            )
            return False

    def _primary_can_send(self, platform: Any, platform_name: str, scene: str) -> tuple[bool, str]:
        """预判 ``context.send_message`` 是否真的会发出这条消息。

        与适配器内部的跳过条件保持一致，从而区分「发送失败」和
        「适配器压根没发」——后者不会抛异常，只能靠预判识别。
        """
        if platform is None:
            return False, "找不到 UMO 对应的平台适配器"
        if platform_name in QQ_OFFICIAL_PLATFORMS:
            if scene == SCENE_GROUP:
                if not bool(getattr(platform, "_allow_group_proactive_send", True)):
                    return (
                        False,
                        "QQ 官方适配器当前禁止群聊主动推送"
                        "（_allow_group_proactive_send=False）",
                    )
                return True, ""
            if scene == SCENE_PRIVATE:
                return True, ""
            return False, f"该适配器不支持向 scene={scene} 的会话主动发送"
        if not hasattr(platform, "send_by_session"):
            return False, "该平台适配器没有实现 send_by_session"
        return True, ""

    async def _qq_group_fallback(
        self, platform: Any, target_id: str, content: str
    ) -> dict[str, Any]:
        """直接调用 QQ 官方 ``post_group_message``（兜底路径）。

        复用 botpy 自带的 HTTP 客户端，access_token 刷新与重试都由它负责，
        插件不需要自己拼 ``Authorization`` 头。
        """
        api = getattr(getattr(platform, "client", None), "api", None)
        poster = getattr(api, "post_group_message", None)
        if not callable(poster):
            return {
                "ok": False,
                "error": "兜底发送不可用：适配器没有暴露 botpy API 客户端",
            }
        if not target_id:
            return {"ok": False, "error": "兜底发送缺少 group_openid"}
        try:
            ret = await poster(
                group_openid=target_id,
                msg_type=0,
                content=content,
                msg_seq=random.randint(1, 10000),
            )
        except Exception as exc:
            return {
                "ok": False,
                "error": f"兜底发送失败：{_sanitize_error(exc)}",
            }
        message_id = None
        if isinstance(ret, dict):
            message_id = ret.get("id")
        else:
            message_id = getattr(ret, "id", None)
        return {"ok": True, "message_id": str(message_id) if message_id else None}

    # ------------------------------------------------------------------
    # 频控计数
    # ------------------------------------------------------------------

    def _quota_snapshot(self, target_id: str, scene: str) -> dict[str, Any]:
        """返回该会话的频控快照。仅用于告警，不参与放行判断。"""
        window_limit = max(1, self._cfg_int("qq_group_qpm_limit", DEFAULT_QPM_LIMIT))
        daily_limit = max(1, self._cfg_int("qq_group_daily_limit", DEFAULT_DAILY_LIMIT))
        warn_ratio = self._cfg_float("quota_warn_ratio", 0.8)
        now = time.time()

        stamps = [
            stamp
            for stamp in self._quota_window.get(target_id, [])
            if now - stamp < QUOTA_WINDOW_SECONDS
        ]
        self._quota_window[target_id] = stamps
        window_used = len(stamps)
        daily_used = int(self._quota_daily.get(_today(), {}).get(target_id, 0))

        window_ratio = window_used / window_limit
        daily_ratio = daily_used / daily_limit
        ratio = max(window_ratio, daily_ratio)
        if ratio >= 1.0:
            level = "critical"
        elif ratio >= warn_ratio:
            level = "warn"
        else:
            level = "ok"

        message = ""
        if level == "critical":
            message = (
                f"已用完本地估算的额度（近 1 分钟 {window_used}/{window_limit}，"
                f"今日 {daily_used}/{daily_limit}），继续发送很可能被 QQ 拒绝"
            )
        elif level == "warn":
            message = (
                f"接近频控阈值（近 1 分钟 {window_used}/{window_limit}，"
                f"今日 {daily_used}/{daily_limit}）"
            )

        return {
            "target_id": target_id,
            "scene": scene,
            "window_used": window_used,
            "window_limit": window_limit,
            "window_remaining": max(0, window_limit - window_used),
            "daily_used": daily_used,
            "daily_limit": daily_limit,
            "daily_remaining": max(0, daily_limit - daily_used),
            "ratio": round(ratio, 4),
            "level": level,
            "message": message,
            "window_seconds": QUOTA_WINDOW_SECONDS,
        }

    def _quota_consume(self, target_id: str) -> None:
        """记一次发送尝试。故意在「尝试」而不是「确认成功」时计数。"""
        self._quota_window.setdefault(target_id, []).append(time.time())
        day = _today()
        bucket = self._quota_daily.setdefault(day, {})
        bucket[target_id] = int(bucket.get(target_id, 0)) + 1
        # 只保留今天的桶，避免无限增长。
        for stale in [key for key in self._quota_daily if key != day]:
            self._quota_daily.pop(stale, None)
        self._quota_dirty = True

    # ------------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------------

    async def _write_audit(
        self,
        *,
        mode: str,
        record: dict[str, Any] | None,
        umo: str,
        content: str,
        success: bool,
        error: str = "",
        path: str = "none",
        message_id: str | None = None,
        operator: str | None = None,
    ) -> None:
        alias = str((record or {}).get("alias") or "")
        entry = {
            "ts": _now(),
            "time": _format_time(_now()),
            "mode": mode,
            "alias": alias,
            "unified_msg_origin": umo,
            "scene": str((record or {}).get("scene") or _scene_from_umo(umo)),
            "target_id": str((record or {}).get("target_id") or ""),
            "content_preview": _preview(content),
            "success": bool(success),
            "error": error or None,
            "path": path,
            "message_id": message_id,
            "operator": operator,
        }
        try:
            await self.audit.append(entry)
        except OSError as exc:
            self.logger.error(f"[BotConsole] 写审计日志失败：{_sanitize_error(exc)}")

    # ------------------------------------------------------------------
    # 会话登记（监听所有消息）
    # ------------------------------------------------------------------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent) -> None:
        """监听所有消息，把会话 upsert 进登记表。

        注意：本 handler 不 yield 任何结果，因此不会回复任何消息。
        """
        if self._closing or not self._cfg_bool("enabled", True):
            return
        if not self._cfg_bool("auto_register", True):
            return
        try:
            await self._ensure_loaded()
            await self._record_event(event)
        except Exception as exc:
            # 登记失败绝不能影响到正常对话流程。
            self.logger.debug(f"[BotConsole] 会话登记失败：{_sanitize_error(exc)}")

    async def _record_event(self, event: AstrMessageEvent) -> None:
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo or umo.count(":") < 2:
            return
        platform_id, message_type, session_id = umo.split(":", 2)
        scene = _MESSAGE_TYPE_2_SCENE.get(message_type, SCENE_OTHER)
        if scene == SCENE_OTHER:
            return

        # 群聊的 session_id 就是 group_openid（见 qq_official 适配器
        # ``abm.session_id = abm.group_id``），私聊则是用户 openid。
        target_id = session_id
        message_obj = getattr(event, "message_obj", None)
        message_id = str(getattr(message_obj, "message_id", "") or "")
        self_id = str(getattr(message_obj, "self_id", "") or "")
        try:
            sender_name = str(event.get_sender_name() or "")
        except Exception:
            sender_name = ""

        self.store.upsert_seen(
            umo=umo,
            platform_name=str(event.get_platform_name() or ""),
            platform_id=platform_id,
            scene=scene,
            target_id=target_id,
            message_id=message_id,
            self_id=self_id,
            sender_name=sender_name,
            preview=_preview(event.get_message_str()),
        )
        await self.store.flush()

    # ------------------------------------------------------------------
    # /bind 命令
    # ------------------------------------------------------------------

    @filter.command("bind", alias={"绑定", "bindme"})
    async def cmd_bind(self, event: AstrMessageEvent) -> None:
        """``/bind <备注名>`` —— 把当前会话绑定到该备注名。

        用法：
            /bind 测试群            绑定备注名
            /bind                   查看当前会话的绑定情况
            /bind -                 清除备注名
        """
        if self._closing or not self._cfg_bool("enabled", True):
            yield event.plain_result("Bot 控制台已停用。")
            return

        await self._ensure_loaded()

        umo = str(event.unified_msg_origin or "")
        raw = str(event.get_message_str() or "").strip()
        # 去掉命令本身（第一个空白分隔的 token），其余整段作为备注名，
        # 这样备注名里带空格也能正常绑定。
        tokens = raw.split(maxsplit=1)
        alias = tokens[1].strip() if len(tokens) > 1 else ""

        if not alias:
            record = self.store.get(umo)
            current = str((record or {}).get("alias") or "")
            if current:
                yield event.plain_result(
                    f"🔖 当前会话已绑定备注名：{current}\n"
                    f"UMO：{umo}\n"
                    "修改：/bind 新备注名\n"
                    "清除：/bind -"
                )
            else:
                yield event.plain_result(
                    "用法：/bind <备注名>\n"
                    "例如：/bind 高三(2)班通知群\n"
                    f"当前会话 UMO：{umo}"
                )
            return

        record = self.store.get(umo)
        if record is None:
            # 理论上前面的 on_any_message 已经登记过了；这里兜一层，
            # 保证即使在 auto_register 关闭时 /bind 也能建立记录。
            platform_id, message_type, session_id = (
                umo.split(":", 2) if umo.count(":") >= 2 else ("", "", umo)
            )
            scene = _MESSAGE_TYPE_2_SCENE.get(message_type, SCENE_OTHER)
            self.store.upsert_seen(
                umo=umo,
                platform_name=str(event.get_platform_name() or ""),
                platform_id=platform_id,
                scene=scene,
                target_id=session_id,
                message_id=str(getattr(event.message_obj, "message_id", "") or ""),
                self_id=str(getattr(event.message_obj, "self_id", "") or ""),
                sender_name=str(event.get_sender_name() or ""),
                preview=_preview(raw),
            )

        if alias in ("-", "－", "清除", "清空"):
            self.store.bind(umo, "", "")
            await self.store.flush(force=True)
            yield event.plain_result("✅ 已清除本会话的备注名（不会再出现在控制台可发送列表里）。")
            return

        current_note = str((self.store.get(umo) or {}).get("note") or "")
        record = self.store.bind(umo, alias, current_note)
        await self.store.flush(force=True)
        scene = str((record or {}).get("scene") or SCENE_OTHER)
        yield event.plain_result(
            f"✅ 已绑定备注名：{alias}\n"
            f"📮 场景：{'群聊' if scene == SCENE_GROUP else '私聊'}\n"
            f"🆔 UMO：{umo}\n"
            "现在可以在 AstrBot 的 WebUI 插件页 → Bot Console 里选中它并发送消息了。"
        )

    # ------------------------------------------------------------------
    # 发送（主路径 + 兜底）
    # ------------------------------------------------------------------

    async def send_as_bot(
        self,
        umo: str,
        content: str,
        *,
        operator: str | None = None,
    ) -> dict[str, Any]:
        """以 Bot 身份向 ``umo`` 主动发送纯文本。

        Returns:
            结构化结果，包含 ``ok`` / ``path`` / ``error`` / ``quota`` 等字段。
            本方法不抛异常——所有失败都通过返回值表达。
        """
        record = self.store.get(umo)
        scene = str((record or {}).get("scene") or _scene_from_umo(umo))
        target_id = str((record or {}).get("target_id") or "")
        if not target_id:
            parts = umo.split(":", 2)
            target_id = parts[2] if len(parts) == 3 else umo
        alias = str((record or {}).get("alias") or "")

        warnings: list[str] = []
        quota = self._quota_snapshot(target_id, scene)
        if quota["level"] != "ok" and quota["message"]:
            warnings.append(quota["message"])

        def _fail(message: str, path: str = "none") -> dict[str, Any]:
            return {
                "ok": False,
                "path": path,
                "message_id": None,
                "error": message,
                "warnings": warnings,
                "quota": quota,
                "alias": alias,
                "scene": scene,
                "unified_msg_origin": umo,
                "content_preview": _preview(content),
                "at": _now(),
            }

        if not self._cfg_bool("enabled", True):
            return _fail("配置里关闭了 Bot 控制台（enabled=false）")
        if not self._cfg_bool("allow_send", True):
            return _fail("配置里关闭了 Web 端主动发送（allow_send=false）")
        if self._cfg_bool("require_bind_before_send", True) and not alias:
            return _fail(
                "该会话还没有备注名，请先在对应的群聊/私聊里发送 /bind <备注名>"
            )
        if record is None:
            return _fail("该会话尚未登记，请先在目标会话里发一条消息让插件学习它")

        platform = self._find_platform(umo)
        if platform is None:
            return _fail(
                "找不到 UMO 对应的平台适配器。AstrBot 重载后 platform id 可能变化，"
                "请重新发送一条消息让插件刷新登记"
            )
        platform_name = self._platform_name(platform)

        # 关键一步：补登记场景，否则 QQ 官方适配器会静默跳过群主动消息。
        if platform_name in QQ_OFFICIAL_PLATFORMS and not self._ensure_scene(
            platform, scene, target_id
        ):
            warnings.append(
                "适配器没有 remember_session_scene，无法确认群聊主动推送会被执行"
            )

        primary_ok, primary_reason = self._primary_can_send(
            platform, platform_name, scene
        )

        success = False
        path = "none"
        message_id: str | None = None
        error_text = ""

        # ---- 主路径：context.send_message ----
        if primary_ok:
            try:
                found = await self.context.send_message(
                    umo, MessageChain().message(content)
                )
            except Exception as exc:
                error_text = _sanitize_error(exc)
                self.logger.warning(
                    f"[BotConsole] 主发送路径失败（{alias or umo}）：{error_text}"
                )
            else:
                if found:
                    success = True
                    path = "primary"
                else:
                    error_text = "AstrBot 里没有找到该 UMO 对应的平台实例，消息未发出"
        else:
            error_text = primary_reason

        # ---- 兜底路径：直接调 QQ 官方接口 ----
        # 只在「主路径确实没发出去」时才走，避免重复发送。
        if not success and (
            platform_name in QQ_OFFICIAL_PLATFORMS
            and scene == SCENE_GROUP
            and self._cfg_bool("send_fallback_enabled", True)
        ):
            fallback = await self._qq_group_fallback(platform, target_id, content)
            if fallback.get("ok"):
                if error_text:
                    warnings.append(f"主发送路径未成功，已改用兜底接口：{error_text}")
                success = True
                path = "fallback"
                message_id = fallback.get("message_id")
                error_text = ""
            else:
                fb_error = str(fallback.get("error") or "兜底发送失败")
                error_text = f"{error_text}；{fb_error}" if error_text else fb_error

        self._quota_consume(target_id)
        await self._write_audit(
            mode="direct",
            record=record,
            umo=umo,
            content=content,
            success=success,
            error=error_text,
            path=path,
            message_id=message_id,
            operator=operator,
        )

        return {
            "ok": success,
            "path": path,
            "message_id": message_id,
            "error": error_text or None,
            "warnings": warnings,
            "quota": self._quota_snapshot(target_id, scene),
            "alias": alias,
            "scene": scene,
            "unified_msg_origin": umo,
            "content_preview": _preview(content),
            "at": _now(),
        }

    # ------------------------------------------------------------------
    # 模拟用户消息（调试）
    # ------------------------------------------------------------------

    async def simulate_user_message(
        self,
        umo: str,
        content: str,
        *,
        operator: str | None = None,
    ) -> dict[str, Any]:
        """把 ``content`` 伪装成用户消息注入 AstrBot 消息管道。

        实现方式：构造一个 ``AstrBotMessage``，用平台自己的 ``create_event``
        包成事件后 ``commit_event`` 丢进事件队列，后面的唤醒 / 插件 / LLM
        流程与真实消息完全一致。

        为了让 LLM 一定会回复，这里还做了两件事：
        - 群聊时在消息链最前面塞一个 ``At(qq=self_id)``，模拟「@了机器人」；
        - 显式置 ``is_wake`` / ``is_at_or_wake_command``，绕过唤醒词检查。

        Returns:
            结构化结果，``ok`` 表示事件是否成功入队（不代表 Bot 一定回复）。
        """
        record = self.store.get(umo)
        if record is None:
            return {
                "ok": False,
                "error": "该会话尚未登记，请先在目标会话里发一条消息让插件学习它",
            }
        if not self._cfg_bool("enabled", True):
            return {"ok": False, "error": "配置里关闭了 Bot 控制台（enabled=false）"}
        if not self._cfg_bool("allow_simulate", True):
            return {"ok": False, "error": "配置里关闭了模拟注入（allow_simulate=false）"}

        platform = self._find_platform(umo)
        if platform is None:
            return {"ok": False, "error": "找不到 UMO 对应的平台适配器"}
        create_event = getattr(platform, "create_event", None)
        commit_event = getattr(platform, "commit_event", None)
        if not callable(create_event) or not callable(commit_event):
            return {"ok": False, "error": "该平台适配器不支持注入模拟事件"}

        scene = str(record.get("scene") or _scene_from_umo(umo))
        target_id = str(record.get("target_id") or "")
        self_id = str(record.get("self_id") or "")

        try:
            message_obj, event = self._build_simulated_event(
                platform=platform,
                scene=scene,
                target_id=target_id,
                self_id=self_id,
                content=content,
                record=record,
            )
        except Exception as exc:
            return {
                "ok": False,
                "error": f"构造模拟事件失败：{_sanitize_error(exc)}",
            }
        if event is None:
            return {"ok": False, "error": "平台适配器没有返回可用的消息事件"}

        try:
            commit_event(event)
        except Exception as exc:
            return {
                "ok": False,
                "error": f"注入消息管道失败：{_sanitize_error(exc)}",
            }

        await self._write_audit(
            mode="simulate",
            record=record,
            umo=umo,
            content=content,
            success=True,
            error="",
            path="simulate",
            message_id=str(getattr(message_obj, "message_id", "") or ""),
            operator=operator,
        )
        self.logger.info(
            f"[BotConsole] 已注入模拟用户消息（{record.get('alias') or umo}）"
        )
        return {
            "ok": True,
            "path": "simulate",
            "alias": str(record.get("alias") or ""),
            "scene": scene,
            "unified_msg_origin": umo,
            "content_preview": _preview(content),
            "note": (
                "事件已入队。Bot 的回复走被动回复接口，依赖 5 分钟内的 msg_id；"
                "若超时无回复，多半是 QQ 侧拒绝了主动回复。"
            ),
            "at": _now(),
        }

    def _build_simulated_event(
        self,
        *,
        platform: Any,
        scene: str,
        target_id: str,
        self_id: str,
        content: str,
        record: dict[str, Any],
    ) -> tuple[Any, Any]:
        """构造 ``AstrBotMessage`` 并交给平台适配器包成事件。"""
        # 延迟导入：这些是 astrbot 内部模块，放在顶层会在插件加载早期
        # 触发不必要的循环导入风险。
        from astrbot.core.platform.astrbot_message import (
            AstrBotMessage,
            Group,
            MessageMember,
        )
        from astrbot.core.platform.message_type import MessageType

        message_obj = AstrBotMessage()
        message_obj.type = (
            MessageType.GROUP_MESSAGE if scene == SCENE_GROUP else MessageType.FRIEND_MESSAGE
        )
        message_obj.self_id = self_id
        message_obj.session_id = target_id
        # 复用真实消息的 msg_id 当被动回复凭据；没有就留空，让发送端走兜底。
        message_obj.message_id = str(record.get("last_message_id") or "")
        if scene == SCENE_GROUP:
            message_obj.group = Group(group_id=target_id)
        message_obj.sender = MessageMember(
            user_id=str(record.get("last_sender_id") or self_id or "console_debug"),
            nickname="控制台调试",
        )
        chain: list[Any] = []
        if scene == SCENE_GROUP and self_id:
            # 模拟「@了机器人」，保证 WakingStage 判定为唤醒。
            chain.append(At(qq=self_id))
        chain.append(Plain(content))
        message_obj.message = chain
        message_obj.message_str = content
        message_obj.raw_message = None

        event = platform.create_event(message_obj)
        if event is None:
            return message_obj, None
        # 显式标记为已唤醒，跳过唤醒词 / @ 的判定差异。
        try:
            event.is_wake = True
            event.is_at_or_wake_command = True
        except Exception:
            pass
        return message_obj, event

    # ------------------------------------------------------------------
    # Web API 注册
    # ------------------------------------------------------------------

    def _register_web_apis(self) -> None:
        """注册全部 Web API。

        路由名有坑：dashboard 拼出的 URL 是
        ``/api/v1/plugins/extensions/<pluginName>/<subpath>``，核心取
        ``<pluginName>/<subpath>`` 去匹配注册的路由，所以**带插件名前缀**的
        ``/{plugin}/conversations`` 才是能命中的形式。

        但前端 bridge 传的 ``endpoint`` 只是 ``<subpath>``（插件名由 dashboard
        补），因此这里同时注册「带前缀」和「不带前缀」两种写法：
        无论前端传不传前缀、无论 dashboard 补不补，都能命中。
        """
        routes: tuple[tuple[str, Any, list[str], str], ...] = (
            (
                f"{API_PREFIX}/conversations",
                self.api_conversations,
                ["GET"],
                "Bot Console: 已登记的会话列表",
            ),
            (
                f"{API_PREFIX}/conversations/bind",
                self.api_bind_conversation,
                ["POST"],
                "Bot Console: 重命名 / 绑定会话备注名",
            ),
            (
                f"{API_PREFIX}/conversations/forget",
                self.api_forget_conversation,
                ["POST"],
                "Bot Console: 删除一条会话登记",
            ),
            (
                f"{API_PREFIX}/send",
                self.api_send,
                ["POST"],
                "Bot Console: 以 Bot 身份主动发送纯文本",
            ),
            (
                f"{API_PREFIX}/simulate",
                self.api_simulate,
                ["POST"],
                "Bot Console: 模拟用户消息注入消息管道",
            ),
            (
                f"{API_PREFIX}/history",
                self.api_history,
                ["GET"],
                "Bot Console: 发送审计历史",
            ),
            (
                f"{API_PREFIX}/status",
                self.api_status,
                ["GET"],
                "Bot Console: 插件状态与频控额度",
            ),
        )
        for route, handler, methods, desc in routes:
            variants = [route]
            if route.startswith(API_PREFIX + "/"):
                # 裸路由，兼容不自动补插件名前缀的场景
                variants.append(route[len(API_PREFIX) :])
            for candidate in variants:
                try:
                    self.context.register_web_api(candidate, handler, methods, desc)
                except Exception as exc:
                    self.logger.error(
                        f"[BotConsole] 注册 Web API {candidate} 失败："
                        f"{_sanitize_error(exc)}"
                    )

    # -- 请求辅助 ---------------------------------------------------------

    async def _json_body(self) -> dict[str, Any]:
        try:
            payload = await request.json({})
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _operator(self) -> str | None:
        try:
            return request.username
        except Exception:
            return None

    def _guard(self) -> Any:
        """所有 Web 入口的公共守卫：关闭中/停用时返回错误响应。"""
        if self._closing:
            return error_response("Bot 控制台正在重新加载，请稍后重试", status_code=503)
        if not self._cfg_bool("enabled", True):
            return error_response(
                "Bot 控制台已在插件配置里停用（enabled=false）", status_code=403
            )
        return None

    def _decorate_record(self, record: dict[str, Any]) -> dict[str, Any]:
        """给前端补上派生字段：展示名、频控快照、时间文本。"""
        item = dict(record)
        target_id = str(item.get("target_id") or "")
        scene = str(item.get("scene") or SCENE_OTHER)
        item.setdefault("note", "")
        item["display_name"] = str(item.get("alias") or "").strip() or "未命名会话"
        item["bound"] = bool(str(item.get("alias") or "").strip())
        item["scene_label"] = {
            SCENE_GROUP: "群聊",
            SCENE_PRIVATE: "私聊",
        }.get(scene, "其他")
        item["last_seen_text"] = _format_time(item.get("last_seen_at"))
        item["quota"] = self._quota_snapshot(target_id, scene)
        return item

    # -- GET /conversations ----------------------------------------------

    async def api_conversations(self):
        blocked = self._guard()
        if blocked is not None:
            return blocked
        await self._ensure_loaded()
        records = [self._decorate_record(item) for item in self.store.sorted_records()]
        return json_response(
            {
                "ok": True,
                "conversations": records,
                "total": len(records),
                "bound": sum(1 for item in records if item["bound"]),
            }
        )

    # -- POST /conversations/bind ----------------------------------------

    async def api_bind_conversation(self):
        blocked = self._guard()
        if blocked is not None:
            return blocked
        await self._ensure_loaded()

        body = await self._json_body()
        umo = str(body.get("unified_msg_origin") or "").strip()
        alias = _collapse(body.get("alias"))
        note = body.get("note")
        if not umo:
            return error_response("缺少 unified_msg_origin")
        if not alias:
            return error_response("备注名不能为空")
        if len(alias) > 64:
            return error_response("备注名太长（最多 64 个字符）")
        if umo not in self.store:
            return error_response(
                "该会话尚未登记。请先在目标群/私聊里发一条消息，或发送 /bind <备注名>",
                status_code=404,
            )
        record = self.store.bind(
            umo,
            alias,
            (_collapse(note) if isinstance(note, str) else None),
        )
        await self.store.flush(force=True)
        return json_response(
            {"ok": True, "conversation": self._decorate_record(record or {})}
        )

    # -- POST /conversations/forget --------------------------------------

    async def api_forget_conversation(self):
        blocked = self._guard()
        if blocked is not None:
            return blocked
        await self._ensure_loaded()

        body = await self._json_body()
        umo = str(body.get("unified_msg_origin") or "").strip()
        if not umo:
            return error_response("缺少 unified_msg_origin")
        removed = self.store.remove(umo)
        if not removed:
            return error_response("该会话不在登记表里", status_code=404)
        await self.store.flush(force=True)
        return json_response({"ok": True, "unified_msg_origin": umo})

    # -- POST /send ------------------------------------------------------

    async def api_send(self):
        blocked = self._guard()
        if blocked is not None:
            return blocked
        await self._ensure_loaded()

        body = await self._json_body()
        umo = str(body.get("unified_msg_origin") or "").strip()
        content = str(body.get("content") or "").strip()
        if not umo:
            return error_response("缺少 unified_msg_origin")
        if not content:
            return error_response("消息内容不能为空")
        if len(content) > 4000:
            return error_response("消息太长（最多 4000 个字符）")

        result = await self.send_as_bot(umo, content, operator=self._operator())
        if result.get("ok"):
            self.logger.info(
                f"[BotConsole] 已以 Bot 身份发送到 {result.get('alias') or umo}"
                f"（路径 {result.get('path')}）"
            )
        else:
            self.logger.warning(
                f"[BotConsole] 发送失败（{result.get('alias') or umo}）："
                f"{result.get('error')}"
            )
        # 发送结果（含失败原因）一律用 200 + ok 字段返回，方便前端展示详情。
        return json_response(result)

    # -- POST /simulate --------------------------------------------------

    async def api_simulate(self):
        blocked = self._guard()
        if blocked is not None:
            return blocked
        await self._ensure_loaded()

        body = await self._json_body()
        umo = str(body.get("unified_msg_origin") or "").strip()
        content = str(body.get("content") or "").strip()
        if not umo:
            return error_response("缺少 unified_msg_origin")
        if not content:
            return error_response("消息内容不能为空")

        result = await self.simulate_user_message(
            umo, content, operator=self._operator()
        )
        return json_response(result)

    # -- GET /history ----------------------------------------------------

    async def api_history(self):
        blocked = self._guard()
        if blocked is not None:
            return blocked
        await self._ensure_loaded()

        limit = self._cfg_int("audit_max_lines", 200)
        try:
            raw_limit = request.query.get("limit")
            if raw_limit:
                limit = int(raw_limit)
        except Exception:
            pass
        limit = max(1, min(limit, 1000))

        umo_filter = ""
        try:
            umo_filter = str(request.query.get("unified_msg_origin") or "").strip()
        except Exception:
            umo_filter = ""

        entries = await self.audit.tail(limit if not umo_filter else 1000)
        if umo_filter:
            entries = [
                item for item in entries if item.get("unified_msg_origin") == umo_filter
            ]
        entries = entries[:limit]
        return json_response(
            {
                "ok": True,
                "entries": entries,
                "total": len(entries),
                "audit_file": str(self.audit.path),
            }
        )

    # -- GET /status -----------------------------------------------------

    async def api_status(self):
        blocked = self._guard()
        if blocked is not None:
            return blocked
        await self._ensure_loaded()

        platforms: list[dict[str, Any]] = []
        try:
            for platform in self.context.platform_manager.platform_insts:
                try:
                    meta = platform.meta()
                except Exception:
                    continue
                name = str(getattr(meta, "name", "") or "")
                platforms.append(
                    {
                        "id": str(getattr(meta, "id", "") or ""),
                        "name": name,
                        "description": str(getattr(meta, "description", "") or ""),
                        "is_qq_official": name in QQ_OFFICIAL_PLATFORMS,
                        "support_proactive_message": bool(
                            getattr(meta, "support_proactive_message", False)
                        ),
                    }
                )
        except Exception:
            platforms = []

        return json_response(
            {
                "ok": True,
                "plugin": PLUGIN_NAME,
                "version": PLUGIN_VERSION,
                "data_dir": str(self._data_dir),
                "audit_file": str(self.audit.path),
                "conversations_file": str(self.store.path),
                "platforms": platforms,
                "conversations_total": len(self.store.all()),
                "settings": {
                    "allow_send": self._cfg_bool("allow_send", True),
                    "allow_simulate": self._cfg_bool("allow_simulate", True),
                    "auto_register": self._cfg_bool("auto_register", True),
                    "send_fallback_enabled": self._cfg_bool(
                        "send_fallback_enabled", True
                    ),
                    "require_bind_before_send": self._cfg_bool(
                        "require_bind_before_send", True
                    ),
                    "qq_group_qpm_limit": self._cfg_int(
                        "qq_group_qpm_limit", DEFAULT_QPM_LIMIT
                    ),
                    "qq_group_daily_limit": self._cfg_int(
                        "qq_group_daily_limit", DEFAULT_DAILY_LIMIT
                    ),
                    "quota_warn_ratio": self._cfg_float("quota_warn_ratio", 0.8),
                },
            }
        )


__all__ = ["BotConsolePlugin"]

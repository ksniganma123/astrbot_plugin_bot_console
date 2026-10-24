# -*- coding: utf-8 -*-
"""Bot Console 插件自检脚本（开发用，不随插件发布）。

覆盖：
  1. 模块可导入、路由能匹配 dashboard 转发的路径
  2. 插件的 Web API 注册齐全
  3. send_as_bot 的四条分支：主路径 / 场景补登记 / 兜底 / 双失败
  4. simulate_user_message 能构造事件并入队
  5. 登记表与频控计数的基本行为
"""

import asyncio
import os
import re
import sys
import tempfile
import traceback
from pathlib import Path
from types import SimpleNamespace

PLUGIN = str(Path(__file__).resolve().parent.parent)
APP = os.environ.get("ASTRBOT_APP_PATH") or r"D:\QQBOT\astrbot\backend\app"
if not Path(APP).is_dir():
    sys.exit(
        "找不到 AstrBot 源码目录。请设置环境变量 ASTRBOT_APP_PATH 指向 "
        r"<AstrBot>/backend/app（当前为 " + APP + "）"
    )
sys.path.insert(0, APP)
sys.path.insert(0, PLUGIN)

FAILURES = []
CHECKS = [0]


def check(name, condition, detail=""):
    CHECKS[0] += 1
    if condition:
        print(f"  PASS  {name}")
    else:
        FAILURES.append(f"{name} {detail}".strip())
        print(f"  FAIL  {name}  {detail}")


# --------------------------------------------------------------------------
# 把插件数据目录重定向到临时目录，避免污染源码树
# --------------------------------------------------------------------------
TMP = Path(tempfile.mkdtemp(prefix="botconsole-selftest-"))

from astrbot.core.star.star_tools import StarTools  # noqa: E402

StarTools.get_data_dir = classmethod(lambda cls, plugin_name=None: TMP)

import main as plugin_main  # noqa: E402

print("[1] 模块导入与路由匹配")
try:
    from astrbot.dashboard.api.plugins import _plugin_api_route_pattern

    routes = [
        "/astrbot_plugin_bot_console/conversations",
        "/astrbot_plugin_bot_console/conversations/bind",
        "/astrbot_plugin_bot_console/conversations/forget",
        "/astrbot_plugin_bot_console/send",
        "/astrbot_plugin_bot_console/simulate",
        "/astrbot_plugin_bot_console/history",
        "/astrbot_plugin_bot_console/status",
    ]
    for route in routes:
        pattern = _plugin_api_route_pattern(route)
        # dashboard 传给核心的是去掉前导斜杠的 plugin_path
        check(
            f"route match {route}",
            re.fullmatch(pattern, route) is not None,
            f"pattern={pattern}",
        )
except Exception:
    traceback.print_exc()
    FAILURES.append("route pattern import")


# --------------------------------------------------------------------------
# 假平台 / 假 context
# --------------------------------------------------------------------------
class FakeMeta:
    def __init__(self, pid, name, description="", support_proactive_message=True):
        self.id = pid
        self.name = name
        self.description = description
        self.support_proactive_message = support_proactive_message


class FakeAPI:
    def __init__(self, fails=False):
        self.calls = []
        self.fails = fails

    async def post_group_message(self, **kwargs):
        self.calls.append(kwargs)
        if self.fails:
            raise RuntimeError("QQ API 拒绝了这条消息")
        return {"id": "fallback_msg_id"}


class FakeClient:
    def __init__(self, api):
        self.api = api


class FakePlatform:
    def __init__(self, pid="qq_official_1", name="qq_official", *,
                 allow_group_proactive=True, fallback_fails=False):
        self._meta = FakeMeta(pid, name)
        self._session_scene = {}
        self._allow_group_proactive_send = allow_group_proactive
        self.client = FakeClient(FakeAPI(fallback_fails))
        self.remember_calls = []
        self.events = []

    def meta(self):
        return self._meta

    def remember_session_scene(self, session_id, scene):
        self._session_scene[session_id] = scene
        self.remember_calls.append((session_id, scene))

    async def send_by_session(self, session, message_chain):
        return None

    def create_event(self, message):
        return SimpleNamespace(
            message_obj=message,
            is_wake=False,
            is_at_or_wake_command=False,
        )

    def commit_event(self, event):
        self.events.append(event)


class FakeContext:
    def __init__(self, platforms):
        self.platform_manager = SimpleNamespace(platform_insts=platforms)
        self.web_apis = []
        self.send_result = True
        self.send_raises = None
        self.send_calls = []

    def register_web_api(self, route, view_handler, methods, desc):
        self.web_apis.append((route, view_handler, tuple(methods), desc))

    async def send_message(self, session, message_chain):
        self.send_calls.append(session)
        if self.send_raises is not None:
            raise self.send_raises
        return self.send_result


UMO = "qq_official_1:GroupMessage:GROUP_OPENID_ABC"
CONFIG = {
    "enabled": True,
    "allow_send": True,
    "allow_simulate": True,
    "send_fallback_enabled": True,
    "require_bind_before_send": True,
    "qq_group_qpm_limit": 60,
    "qq_group_daily_limit": 1000,
    "quota_warn_ratio": 0.8,
}


def build_plugin(platform, config=None):
    context = FakeContext([platform])
    plugin = plugin_main.BotConsolePlugin(context, config or dict(CONFIG))
    plugin.store.upsert_seen(
        umo=UMO,
        platform_name="qq_official",
        platform_id="qq_official_1",
        scene="group",
        target_id="GROUP_OPENID_ABC",
        message_id="real_msg_1",
        self_id="BOT_SELF_ID",
        sender_name="张三",
        preview="hi",
    )
    plugin.store.bind(UMO, "测试群")
    return plugin, context


print("[2] Web API 注册")
platform = FakePlatform()
plugin, context = build_plugin(platform)
registered = {route for route, _, _, _ in context.web_apis}
API_PREFIX = "/astrbot_plugin_bot_console"
SUBPATHS = [
    "conversations",
    "conversations/bind",
    "conversations/forget",
    "send",
    "simulate",
    "history",
    "status",
]
expected_prefixed = {API_PREFIX + "/" + s for s in SUBPATHS}
expected_bare = {"/" + s for s in SUBPATHS}
check(
    "带插件名前缀的 7 条路由全部注册",
    expected_prefixed <= registered,
    f"缺少 {expected_prefixed - registered}",
)
check(
    "不带前缀的 7 条路由也注册（兼容）",
    expected_bare <= registered,
    f"缺少 {expected_bare - registered}",
)
check("注册的 handler 都是可调用对象",
      all(callable(h) for _, h, _, _ in context.web_apis))


# --------------------------------------------------------------------------
# [2b] 端到端路由回归测试
#
# 复刻 webui 里的真实拼接：
#   B = e => `/api/v1/plugins/extensions/${encodeURIComponent(pluginName)}/${e}`
# 然后交给核心的 _match_registered_web_api 用整串匹配。
#
# 历史 bug：前端 endpoint 自己又带了一次插件名，URL 变成
#   .../extensions/astrbot_plugin_bot_console/astrbot_plugin_bot_console/conversations
# 整串匹配失败，用户看到「未找到该路由」。这里把正确和错误两种形态都钉死。
# --------------------------------------------------------------------------
print("[2b] 端到端路由匹配（复刻 dashboard 拼接）")
try:
    from astrbot.dashboard.api.plugins import _match_registered_web_api

    DASHBOARD_PREFIX = "/api/v1/plugins/extensions/"

    def dashboard_plugin_path(plugin_name, endpoint):
        """返回 core 实际拿到的 plugin_path（路由 {plugin_path:path} 的值）。"""
        url = f"{DASHBOARD_PREFIX}{plugin_name}/{endpoint}"
        return url[len(DASHBOARD_PREFIX):]

    def resolve(endpoint, method="GET", plugin_name="astrbot_plugin_bot_console"):
        return _match_registered_web_api(
            context.web_apis,
            dashboard_plugin_path(plugin_name, endpoint),
            method,
        )

    bare = resolve("conversations")
    check("裸 endpoint 'conversations' 能命中路由", bare is not None,
          dashboard_plugin_path("astrbot_plugin_bot_console", "conversations"))
    if bare:
        check("命中的是 api_conversations",
              bare[0].__name__ == "api_conversations", str(bare[0]))
        check("没有多余路径参数", bare[1] == {}, str(bare[1]))

    doubled = resolve("astrbot_plugin_bot_console/conversations")
    check("重复带插件名时不命中（复现用户遇到的 bug）", doubled is None,
          str(doubled))

    for subpath, handler_name, method in (
        ("conversations/bind", "api_bind_conversation", "POST"),
        ("conversations/forget", "api_forget_conversation", "POST"),
        ("send", "api_send", "POST"),
        ("simulate", "api_simulate", "POST"),
        ("history", "api_history", "GET"),
        ("status", "api_status", "GET"),
    ):
        hit = resolve(subpath, method)
        check(f"endpoint '{subpath}' ({method}) 命中 {handler_name}",
              hit is not None and hit[0].__name__ == handler_name,
              str(hit[0] if hit else None))

    check("GET 打 POST 路由不命中",
          resolve("send", "GET") is None)
    check("未知子路径不命中",
          resolve("nope", "GET") is None)
except Exception:
    traceback.print_exc()
    FAILURES.append("端到端路由测试异常")


async def scenario_primary():
    print("[3] 主路径成功")
    p = FakePlatform()
    plugin, ctx = build_plugin(p)
    result = await plugin.send_as_bot(UMO, "你好，这是 Bot 主动消息")
    check("ok=True", result["ok"] is True, str(result))
    check("path=primary", result["path"] == "primary", str(result.get("path")))
    check("调用了 context.send_message", ctx.send_calls == [UMO], str(ctx.send_calls))
    check("发送前补登记了 group 场景",
          p._session_scene.get("GROUP_OPENID_ABC") == "group",
          str(p._session_scene))
    check("没有走兜底（避免重复发送）", p.client.api.calls == [], str(p.client.api.calls))
    check("频控计数 +1", result["quota"]["window_used"] == 1, str(result["quota"]))


async def scenario_fallback_when_skipped():
    print("[4] 适配器禁止主动群发 → 走兜底")
    p = FakePlatform(allow_group_proactive=False)
    plugin, ctx = build_plugin(p)
    result = await plugin.send_as_bot(UMO, "兜底测试")
    check("ok=True", result["ok"] is True, str(result))
    check("path=fallback", result["path"] == "fallback", str(result.get("path")))
    check("主路径没有被调用", ctx.send_calls == [], str(ctx.send_calls))
    check("兜底收到正确 openid",
          p.client.api.calls and p.client.api.calls[0].get("group_openid") == "GROUP_OPENID_ABC",
          str(p.client.api.calls))
    check("兜底 msg_type=0（纯文本）",
          p.client.api.calls and p.client.api.calls[0].get("msg_type") == 0,
          str(p.client.api.calls))
    check("有告警说明走了兜底", any("兜底" in w for w in result["warnings"]),
          str(result["warnings"]))


async def scenario_fallback_after_exception():
    print("[5] 主路径抛异常 → 走兜底")
    p = FakePlatform()
    plugin, ctx = build_plugin(p)
    ctx.send_raises = RuntimeError("平台内部炸了")
    result = await plugin.send_as_bot(UMO, "异常兜底")
    check("ok=True", result["ok"] is True, str(result))
    check("path=fallback", result["path"] == "fallback", str(result.get("path")))
    check("保留了主路径失败原因",
          any("平台内部炸了" in w for w in result["warnings"]), str(result["warnings"]))


async def scenario_both_fail():
    print("[6] 主路径与兜底都失败")
    p = FakePlatform(fallback_fails=True)
    plugin, ctx = build_plugin(p)
    ctx.send_raises = RuntimeError("平台内部炸了")
    result = await plugin.send_as_bot(UMO, "全失败")
    check("ok=False", result["ok"] is False, str(result))
    check("error 里两段原因都有",
          "平台内部炸了" in (result["error"] or "")
          and "QQ API 拒绝了这条消息" in (result["error"] or ""),
          str(result.get("error")))
    check("仍然写了频控计数", result["quota"]["window_used"] == 1, str(result["quota"]))


async def scenario_unbound():
    print("[7] 未绑定备注名时拒绝发送")
    p = FakePlatform()
    plugin, ctx = build_plugin(p, dict(CONFIG, require_bind_before_send=True))
    plugin.store.bind(UMO, "")
    result = await plugin.send_as_bot(UMO, "应该被拒")
    check("ok=False", result["ok"] is False, str(result))
    check("提示需要 /bind", "备注名" in (result["error"] or ""), str(result.get("error")))
    check("没有真的发出去", ctx.send_calls == [], str(ctx.send_calls))


async def scenario_unknown_platform():
    print("[8] UMO 找不到平台实例")
    p = FakePlatform(pid="another_bot")
    plugin, ctx = build_plugin(p)
    result = await plugin.send_as_bot(UMO, "找不到平台")
    check("ok=False", result["ok"] is False, str(result))
    check("提示适配器问题", "适配器" in (result["error"] or ""), str(result.get("error")))


async def scenario_simulate():
    print("[9] 模拟用户消息注入")
    p = FakePlatform()
    plugin, ctx = build_plugin(p)
    result = await plugin.simulate_user_message(UMO, "帮我看看今天天气")
    check("ok=True", result["ok"] is True, str(result))
    check("事件已入队", len(p.events) == 1, str(len(p.events)))
    event = p.events[0]
    check("事件被标记为已唤醒",
          event.is_wake is True and event.is_at_or_wake_command is True,
          f"is_wake={event.is_wake}")
    message_obj = event.message_obj
    check("消息类型是群消息", str(message_obj.type.value) == "GroupMessage",
          str(message_obj.type))
    check("session_id 是 group_openid",
          message_obj.session_id == "GROUP_OPENID_ABC", str(message_obj.session_id))
    check("消息链首个是 At（模拟 @机器人）",
          type(message_obj.message[0]).__name__ == "At", str(message_obj.message))
    check("复用真实 msg_id 作为被动回复凭据",
          message_obj.message_id == "real_msg_1", str(message_obj.message_id))
    check("message_str 正确", message_obj.message_str == "帮我看看今天天气",
          str(message_obj.message_str))


async def scenario_store_and_audit():
    print("[10] 登记表 / 审计 / 频控持久化")
    p = FakePlatform()
    plugin, ctx = build_plugin(p)
    records = plugin.store.sorted_records()
    check("登记表里有 1 条", len(records) == 1, str(len(records)))
    record = records[0]
    check("备注名已绑定", record["alias"] == "测试群", str(record.get("alias")))
    check("按 UMO 唯一", record["unified_msg_origin"] == UMO)

    # 再登记一次不应该产生新记录
    plugin.store.upsert_seen(
        umo=UMO, platform_name="qq_official", platform_id="qq_official_1",
        scene="group", target_id="GROUP_OPENID_ABC", preview="second",
    )
    check("重复 upsert 不会新增记录", len(plugin.store.sorted_records()) == 1)
    check("message_count 递增",
          plugin.store.sorted_records()[0]["message_count"] == 2,
          str(plugin.store.sorted_records()[0]["message_count"]))

    await plugin.store.flush(force=True)
    check("conversations.json 已落盘", plugin.store.path.is_file(),
          str(plugin.store.path))
    reloaded = plugin_main.ConversationStore(plugin.store.path)
    await reloaded.load()
    check("重新载入后记录数一致", len(reloaded.all()) == 1, str(len(reloaded.all())))
    check("重新载入后备注名保留",
          reloaded.get(UMO)["alias"] == "测试群")

    await plugin.send_as_bot(UMO, "写一条审计")
    entries = await plugin.audit.tail(10)
    check("产生了审计记录", len(entries) >= 1, str(len(entries)))
    entry = entries[-1]
    for field in ("ts", "alias", "unified_msg_origin", "content_preview", "success",
                  "error", "mode", "path"):
        check(f"审计字段 {field} 存在", field in entry, str(entry.keys()))
    check("审计 alias 正确", entry["alias"] == "测试群", str(entry.get("alias")))


async def scenario_quota_warn():
    print("[11] 频控告警阈值")
    p = FakePlatform()
    plugin, ctx = build_plugin(p, dict(CONFIG, qq_group_qpm_limit=10, quota_warn_ratio=0.8))
    for _ in range(8):
        plugin._quota_consume("GROUP_OPENID_ABC")
    snap = plugin._quota_snapshot("GROUP_OPENID_ABC", "group")
    check("8/10 触发 warn", snap["level"] == "warn", str(snap))
    check("剩余额度正确", snap["window_remaining"] == 2, str(snap))
    for _ in range(3):
        plugin._quota_consume("GROUP_OPENID_ABC")
    snap = plugin._quota_snapshot("GROUP_OPENID_ABC", "group")
    check("超额触发 critical", snap["level"] == "critical", str(snap))
    check("超额后剩余不为负", snap["window_remaining"] == 0, str(snap))


async def scenario_pure_helpers():
    print("[12] 纯函数")
    check("_scene_from_umo 群聊",
          plugin_main._scene_from_umo("p:GroupMessage:1") == "group")
    check("_scene_from_umo 私聊",
          plugin_main._scene_from_umo("p:FriendMessage:1") == "private")
    check("_scene_from_umo 未知",
          plugin_main._scene_from_umo("garbage") == "other")
    check("_preview 截断",
          plugin_main._preview("x" * 200).endswith("…"))
    check("_preview 折叠空白",
          plugin_main._preview("a\n\n b") == "a b")
    check("_sanitize_error 非空兜底",
          plugin_main._sanitize_error("") == "未知错误")


async def main_async():
    for scenario in (
        scenario_primary,
        scenario_fallback_when_skipped,
        scenario_fallback_after_exception,
        scenario_both_fail,
        scenario_unbound,
        scenario_unknown_platform,
        scenario_simulate,
        scenario_store_and_audit,
        scenario_quota_warn,
        scenario_pure_helpers,
    ):
        try:
            await scenario()
        except Exception:
            traceback.print_exc()
            FAILURES.append(f"{scenario.__name__} 抛异常")


asyncio.run(main_async())

print()
print("=" * 62)
print(f"共 {CHECKS[0]} 项断言，失败 {len(FAILURES)} 项")
if FAILURES:
    for item in FAILURES:
        print("  - " + item)
    sys.exit(1)
print("全部通过 ✅")

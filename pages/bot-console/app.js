/* ==========================================================================
   Bot Console — 前端逻辑
   --------------------------------------------------------------------------
   与 AstrBot 通信走 Plugin Page bridge（window.AstrBotPluginPage）：
     - apiGet(endpoint, params) / apiPost(endpoint, body)
     - endpoint 是「插件名/子路径」，dashboard 会转发到
       /api/plugins/extensions/<endpoint>
     - 若后端返回 {status:"error", ...}，bridge 会 reject 一个 Error(message)
     - 否则 resolve 为响应体本身（没有顶层 data 字段时不会二次解包）

   所有用户可控文本一律用 textContent 写入，不拼 innerHTML，避免备注名里
   带 < > 之类的字符造成注入。
   ========================================================================== */

(function () {
  "use strict";

  var API = "astrbot_plugin_bot_console";
  var POLL_INTERVAL = 20000;
  var CONFIRM_TIMEOUT = 30000;

  var ICON_GROUP =
    '<svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">' +
    '<circle cx="6" cy="5.6" r="2.3" fill="none" stroke="currentColor" stroke-width="1.4"/>' +
    '<path d="M2.2 13c0-2.1 1.7-3.4 3.8-3.4S9.8 10.9 9.8 13" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>' +
    '<circle cx="11.4" cy="6.2" r="1.7" fill="none" stroke="currentColor" stroke-width="1.3"/>' +
    '<path d="M10.6 9.9c1.7.2 3 1.4 3 3.1" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>' +
    "</svg>";

  var ICON_PRIVATE =
    '<svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">' +
    '<circle cx="8" cy="5.4" r="2.5" fill="none" stroke="currentColor" stroke-width="1.4"/>' +
    '<path d="M3.4 13.2c0-2.4 2-4 4.6-4s4.6 1.6 4.6 4" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>' +
    "</svg>";

  // ---------------------------------------------------------------- 状态

  var state = {
    bridge: null,
    conversations: [],
    selectedUmo: "",
    mode: "direct",
    pending: null,
    busy: false,
    inspectorOpen: true,
    search: "",
    history: [],
    status: null,
    settings: {},
    confirmTimer: null,
    pollTimer: null,
  };

  // ---------------------------------------------------------------- DOM

  function $(id) {
    return document.getElementById(id);
  }

  var dom = {};

  function cacheDom() {
    [
      "shell", "status-line", "refresh-btn", "search", "conv-list", "conv-empty",
      "conv-count", "banner-toggle", "main-title", "main-sub", "scene-pill",
      "toggle-inspector", "banner", "banner-text", "banner-close", "stream",
      "stream-empty", "confirm-bar", "confirm-text", "confirm-cancel",
      "confirm-ok", "mode-direct", "mode-simulate", "mode-hint",
      "composer-input", "send-btn", "composer-warn", "inspector",
      "close-inspector", "inspector-empty", "inspector-content", "field-alias",
      "field-scene", "field-umo", "field-target", "field-lastseen",
      "quota-window", "quota-day", "bar-window", "bar-day", "quota-note",
      "rename-btn", "forget-btn", "copy-umo", "toast",
    ].forEach(function (id) {
      dom[id] = $(id);
    });
    dom.shell = $("shell");
  }

  // ---------------------------------------------------------------- 工具

  function sleep(ms) {
    return new Promise(function (resolve) {
      setTimeout(resolve, ms);
    });
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function toast(message, kind) {
    if (!dom.toast) return;
    dom.toast.textContent = String(message || "");
    dom.toast.className = "toast" + (kind ? " " + kind : "");
    dom.toast.hidden = false;
    window.clearTimeout(toast._timer);
    toast._timer = window.setTimeout(function () {
      dom.toast.hidden = true;
    }, kind === "err" ? 6500 : 3600);
  }

  function formatRelative(ts) {
    var n = Number(ts);
    if (!n) return "—";
    var diff = Math.floor(Date.now() / 1000) - n;
    if (diff < 0) diff = 0;
    if (diff < 60) return "刚刚";
    if (diff < 3600) return Math.floor(diff / 60) + " 分钟前";
    if (diff < 86400) return Math.floor(diff / 3600) + " 小时前";
    if (diff < 172800) return "昨天";
    var d = new Date(n * 1000);
    var mm = String(d.getMonth() + 1).padStart(2, "0");
    var dd = String(d.getDate()).padStart(2, "0");
    var hh = String(d.getHours()).padStart(2, "0");
    var mi = String(d.getMinutes()).padStart(2, "0");
    return mm + "-" + dd + " " + hh + ":" + mi;
  }

  function formatClock(ts) {
    var n = Number(ts);
    if (!n) return "";
    var d = new Date(n * 1000);
    var hh = String(d.getHours()).padStart(2, "0");
    var mi = String(d.getMinutes()).padStart(2, "0");
    var ss = String(d.getSeconds()).padStart(2, "0");
    return hh + ":" + mi + ":" + ss;
  }

  function selectedRecord() {
    if (!state.selectedUmo) return null;
    for (var i = 0; i < state.conversations.length; i += 1) {
      if (state.conversations[i].unified_msg_origin === state.selectedUmo) {
        return state.conversations[i];
      }
    }
    return null;
  }

  // ---------------------------------------------------------------- Bridge

  function findBridge() {
    if (window.AstrBotPluginPage) return window.AstrBotPluginPage;
    return null;
  }

  async function waitForBridge(timeoutMs) {
    var deadline = Date.now() + (timeoutMs || 12000);
    while (Date.now() < deadline) {
      var bridge = findBridge();
      if (bridge) return bridge;
      await sleep(60);
    }
    throw new Error(
      "没有找到 AstrBot Plugin Page bridge SDK。请从 AstrBot WebUI 的插件详情页打开这个 Page。"
    );
  }

  // ---------------------------------------------------------------- API 调用
  //
  // 重要：这里的 path 是「插件内子路径」，**不要带插件名**。
  // dashboard 自己会拼成 /api/v1/plugins/extensions/<pluginName>/<path>，
  // 核心再拿 <pluginName>/<path> 去匹配 register_web_api 注册的路由。
  // 自己再加一次插件名会变成 <plugin>/<plugin>/<path>，整串匹配失败，
  // 表现就是「未找到该路由」。
  //
  // 为兼容将来可能不自动补前缀的版本，路由未命中时会再试一次带前缀的形式。

  var ROUTE_MISS = /未找到该路由|Route not found|Not Found/i;

  function isRouteMiss(error) {
    return Boolean(error && ROUTE_MISS.test(String(error.message || error)));
  }

  async function apiGet(path, params) {
    try {
      return await state.bridge.apiGet(path, params || undefined);
    } catch (error) {
      if (!isRouteMiss(error)) throw error;
      return state.bridge.apiGet(API + "/" + path, params || undefined);
    }
  }

  async function apiPost(path, body) {
    try {
      return await state.bridge.apiPost(path, body || {});
    } catch (error) {
      if (!isRouteMiss(error)) throw error;
      return state.bridge.apiPost(API + "/" + path, body || {});
    }
  }

  // ---------------------------------------------------------------- 载入

  async function loadStatus() {
    try {
      var data = await apiGet("status");
      state.status = data || {};
      state.settings = (data && data.settings) || {};
      applySettingsHints();
      updateStatusLine();
      if (state.status && state.status.platforms) {
        var qq = state.status.platforms.filter(function (p) {
          return p.is_qq_official;
        });
        if (!state.status.platforms.length) {
          showBanner(
            "当前 AstrBot 没有启用任何平台适配器，控制台无法发送消息。请先在「消息平台」里配置 qq_official。"
          );
        } else if (!qq.length) {
          showBanner(
            "没有检测到 QQ 官方机器人适配器（qq_official / qq_official_webhook）。其他平台也能用，但兜底发送只对 QQ 官方生效。"
          );
        }
      }
    } catch (error) {
      updateStatusLine("读取插件状态失败：" + error.message);
    }
  }

  async function loadConversations(options) {
    var silent = options && options.silent;
    try {
      var data = await apiGet("conversations");
      var list = (data && data.conversations) || [];
      state.conversations = list;
      if (state.selectedUmo && !selectedRecord()) {
        state.selectedUmo = "";
        state.history = [];
      }
      renderConversations();
      renderInspector();
      updateComposer();
      updateStatusLine();
      if (!silent && !list.length) {
        // 首次进入且一个会话都没有时，给一个更明确的指引
        showBanner(
          "还没有登记任何会话。到目标群里发一条消息，然后在群里发送 /bind 备注名，这里就会出现它。"
        );
      }
    } catch (error) {
      if (!silent) toast(error.message || "读取会话列表失败", "err");
      updateStatusLine("读取会话列表失败");
    }
  }

  async function loadHistory() {
    var record = selectedRecord();
    if (!record) {
      state.history = [];
      renderStream();
      return;
    }
    try {
      var data = await apiGet("history", {
        unified_msg_origin: record.unified_msg_origin,
        limit: 200,
      });
      state.history = (data && data.entries) || [];
    } catch (error) {
      state.history = [];
      toast(error.message || "读取发送历史失败", "err");
    }
    renderStream();
  }

  // ---------------------------------------------------------------- 渲染

  function renderConversations() {
    if (!dom["conv-list"]) return;
    var query = state.search.trim().toLowerCase();
    var items = state.conversations.filter(function (item) {
      if (!query) return true;
      var haystack = [
        item.display_name, item.alias, item.unified_msg_origin,
        item.target_id, item.scene_label, item.platform,
      ].join(" ").toLowerCase();
      return haystack.indexOf(query) !== -1;
    });

    dom["conv-list"].textContent = "";

    items.forEach(function (item) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "conv-item" + (item.unified_msg_origin === state.selectedUmo ? " selected" : "");
      button.setAttribute("role", "option");
      button.setAttribute("aria-selected", item.unified_msg_origin === state.selectedUmo ? "true" : "false");

      var avatar = el("span", "conv-avatar");
      avatar.innerHTML = item.scene === "group" ? ICON_GROUP : ICON_PRIVATE;

      var body = el("span", "conv-body");
      var alias = el("span", "conv-alias" + (item.bound ? "" : " unbound"), item.display_name);
      alias.title = item.display_name;
      var meta = el(
        "span",
        "conv-meta",
        item.scene_label + " · " + formatRelative(item.last_seen_at)
      );
      meta.title = item.unified_msg_origin;
      body.appendChild(alias);
      body.appendChild(meta);

      button.appendChild(avatar);
      button.appendChild(body);
      button.addEventListener("click", function () {
        selectConversation(item.unified_msg_origin);
      });
      dom["conv-list"].appendChild(button);
    });

    dom["conv-empty"].hidden = state.conversations.length > 0;
    dom["conv-count"].textContent =
      state.conversations.length + " 个会话";

    var total = state.conversations.length;
    var bound = state.conversations.filter(function (item) {
      return item.bound;
    }).length;
    if (total) {
      dom["conv-count"].textContent = bound + " / " + total + " 个已命名";
    }
  }

  function renderStream() {
    if (!dom.stream) return;
    var record = selectedRecord();
    dom.stream.textContent = "";

    if (!record) {
      dom["stream-empty"].hidden = false;
      dom.stream.appendChild(dom["stream-empty"]);
      return;
    }

    var entries = state.history.slice().reverse(); // 审计是新的在前，展示要旧→新
    if (!entries.length) {
      dom["stream-empty"].hidden = false;
      dom.stream.appendChild(dom["stream-empty"]);
      var hint = dom["stream-empty"].querySelector(".empty-body");
      if (hint) {
        hint.textContent = "这个会话还没有发送记录。写下要说的话，确认后发送吧。";
      }
      return;
    }

    entries.forEach(function (entry) {
      var isSimulate = entry.mode === "simulate";
      var ok = entry.success !== false;
      var wrap = el("div", "msg " + (isSimulate ? "sim" : "out") + (ok ? "" : " failed"));

      var bubble = el("div", "bubble", entry.content_preview || "（空内容）");
      bubble.title = entry.content_preview || "";
      wrap.appendChild(bubble);

      var meta = el("div", "msg-meta");
      meta.appendChild(el("span", null, formatClock(entry.ts) || entry.time || ""));

      if (isSimulate) {
        meta.appendChild(el("span", "tag", "模拟用户"));
      } else if (entry.path === "fallback") {
        meta.appendChild(el("span", "tag", "兜底接口"));
      } else if (ok) {
        meta.appendChild(el("span", "tag", "Bot 主动"));
      }

      if (!ok) {
        meta.appendChild(el("span", "tag", "未送达"));
      }
      if (entry.operator) {
        meta.appendChild(el("span", null, "by " + entry.operator));
      }

      wrap.appendChild(meta);

      if (!ok && entry.error) {
        var err = el("div", "field-note", "⚠︎ " + entry.error);
        wrap.appendChild(err);
      }

      dom.stream.appendChild(wrap);
    });

    dom.stream.scrollTop = dom.stream.scrollHeight;
  }

  function renderInspector() {
    var record = selectedRecord();
    var hasSelection = Boolean(record);

    dom["inspector-empty"].hidden = hasSelection;
    dom["inspector-content"].hidden = !hasSelection;
    if (!hasSelection) return;

    // 正在重命名时不要覆写，否则后台轮询会把输入框冲掉
    if (!dom["field-alias"].querySelector("input")) {
      dom["field-alias"].textContent = record.display_name || "—";
    }
    dom["field-scene"].textContent =
      (record.scene_label || "其他") +
      (record.platform ? " · " + record.platform : "") +
      (record.platform_id ? " · " + record.platform_id : "");
    dom["field-umo"].textContent = record.unified_msg_origin || "—";
    dom["field-target"].textContent = record.target_id || "—";
    dom["field-lastseen"].textContent =
      (record.last_seen_text || formatRelative(record.last_seen_at)) +
      (record.message_count ? " · 已见 " + record.message_count + " 条消息" : "");

    var quota = record.quota || {};
    var windowLimit = quota.window_limit || 0;
    var dailyLimit = quota.daily_limit || 0;
    dom["quota-window"].textContent = (quota.window_used || 0) + " / " + windowLimit;
    dom["quota-day"].textContent = (quota.daily_used || 0) + " / " + dailyLimit;

    setBar(dom["bar-window"], (quota.window_used || 0) / (windowLimit || 1), quota.level);
    setBar(dom["bar-day"], (quota.daily_used || 0) / (dailyLimit || 1), quota.level);

    dom["quota-note"].textContent = quota.message
      ? "⚠︎ " + quota.message
      : "仅本地计数，最终以 QQ 接口返回为准。";
  }

  function setBar(node, ratio, level) {
    if (!node) return;
    var pct = Math.max(0, Math.min(1, ratio || 0)) * 100;
    node.style.width = pct.toFixed(1) + "%";
    node.className = "bar-fill" + (level === "critical" ? " critical" : level === "warn" ? " warn" : "");
  }

  function updateStatusLine(override) {
    if (!dom["status-line"]) return;
    if (override) {
      dom["status-line"].textContent = override;
      return;
    }
    var count = state.conversations.length;
    dom["status-line"].textContent = "已连接 · 已登记 " + count + " 个会话";
  }

  function updateMainHeader() {
    var record = selectedRecord();
    if (!dom["main-title"]) return;
    if (!record) {
      dom["main-title"].textContent = "选择一个会话";
      dom["main-sub"].textContent = "发送方是 Bot 本人，消息会直接出现在目标会话里";
      dom["scene-pill"].hidden = true;
      return;
    }
    dom["main-title"].textContent = record.display_name;
    dom["main-sub"].textContent = record.bound
      ? "发送方是 Bot 本人 · " + (record.scene_label || "")
      : "该会话还没有备注名，按约定不允许直接发送";
    dom["scene-pill"].hidden = false;
    dom["scene-pill"].textContent = record.scene_label || "会话";
  }

  function applySettingsHints() {
    var settings = state.settings || {};
    if (settings.allow_send === false) {
      showBanner("插件配置里关闭了主动发送（allow_send=false），这里只能查看会话和发送模拟消息。");
    }
  }

  function showBanner(text) {
    if (!dom.banner) return;
    dom["banner-text"].textContent = text;
    dom.banner.hidden = false;
    if (dom["banner-toggle"]) dom["banner-toggle"].hidden = false;
  }

  function hideBanner() {
    if (dom.banner) dom.banner.hidden = true;
  }

  // ---------------------------------------------------------------- 交互

  function selectConversation(umo) {
    if (state.selectedUmo === umo) return;
    state.selectedUmo = umo;
    cancelConfirm();
    renderConversations();
    updateMainHeader();
    renderInspector();
    updateComposer();
    loadHistory();
  }

  function setMode(mode) {
    state.mode = mode === "simulate" ? "simulate" : "direct";
    var isDirect = state.mode === "direct";
    dom["mode-direct"].setAttribute("aria-selected", isDirect ? "true" : "false");
    dom["mode-simulate"].setAttribute("aria-selected", isDirect ? "false" : "true");
    dom["mode-hint"].textContent = isDirect
      ? "消息会作为 Bot 本人直接发到目标会话"
      : "注入一条伪造的用户消息，Bot 可能会在群里回复（调试用）";
    cancelConfirm();
    updateComposer();
  }

  function updateComposer() {
    var record = selectedRecord();
    var text = dom["composer-input"].value.trim();
    var settings = state.settings || {};
    var blockedReason = "";

    if (!record) {
      blockedReason = "先在左侧选择会话";
    } else if (state.mode === "direct") {
      if (settings.allow_send === false) {
        blockedReason = "配置里关闭了主动发送（allow_send=false）";
      } else if (!record.bound && settings.require_bind_before_send !== false) {
        blockedReason = "该会话还没有备注名，请先在群里发送 /bind 备注名";
      }
    } else if (settings.allow_simulate === false) {
      blockedReason = "配置里关闭了模拟注入（allow_simulate=false）";
    }

    dom["send-btn"].disabled = state.busy || Boolean(blockedReason) || !text;
    dom["composer-warn"].hidden = !blockedReason;
    dom["composer-warn"].textContent = blockedReason;

    var quotaWarn = "";
    if (record && record.quota && record.quota.level !== "ok" && record.quota.message) {
      quotaWarn = "⚠︎ " + record.quota.message;
    }
    if (quotaWarn && !blockedReason) {
      dom["composer-warn"].hidden = false;
      dom["composer-warn"].textContent = quotaWarn;
    }
  }

  function cancelConfirm() {
    state.pending = null;
    if (dom["confirm-bar"]) dom["confirm-bar"].hidden = true;
    window.clearTimeout(state.confirmTimer);
  }

  function armSend() {
    if (state.busy) return;
    var record = selectedRecord();
    if (!record) {
      toast("请先在左侧选择一个会话", "warn");
      return;
    }
    var content = dom["composer-input"].value.trim();
    if (!content) {
      dom["composer-input"].focus();
      return;
    }
    if (state.mode === "direct") {
      if (state.settings.allow_send === false) {
        toast("配置里关闭了主动发送（allow_send=false）", "err");
        return;
      }
      if (!record.bound && state.settings.require_bind_before_send !== false) {
        toast("该会话还没有备注名，请先发送 /bind 备注名", "err");
        return;
      }
    } else if (state.settings.allow_simulate === false) {
      toast("配置里关闭了模拟注入（allow_simulate=false）", "err");
      return;
    }

    state.pending = {
      umo: record.unified_msg_origin,
      content: content,
      mode: state.mode,
      alias: record.display_name,
      scene: record.scene_label || "会话",
    };

    var text = dom["confirm-text"];
    text.textContent = "";
    if (state.mode === "direct") {
      text.appendChild(document.createTextNode("将以 Bot 身份发送到："));
      var strong = el("b", null, state.pending.alias);
      text.appendChild(strong);
      text.appendChild(document.createTextNode("（" + state.pending.scene + "）"));
      text.appendChild(document.createElement("br"));
      var quote = el("span", null, "「" + (state.pending.content.length > 80
        ? state.pending.content.slice(0, 79) + "…"
        : state.pending.content) + "」");
      text.appendChild(quote);
    } else {
      text.appendChild(document.createTextNode("将把一条「模拟用户消息」注入："));
      text.appendChild(el("b", null, state.pending.alias));
      text.appendChild(document.createTextNode("（" + state.pending.scene + "）"));
      text.appendChild(document.createElement("br"));
      text.appendChild(document.createTextNode(
        "事件会走完整消息管道，Bot 的回复是真实发到这个会话里的。"
      ));
    }

    dom["confirm-ok"].textContent = state.mode === "direct" ? "确认发送" : "确认注入";
    dom["confirm-bar"].hidden = false;
    dom["confirm-ok"].focus();

    window.clearTimeout(state.confirmTimer);
    state.confirmTimer = window.setTimeout(cancelConfirm, CONFIRM_TIMEOUT);
  }

  async function commitSend() {
    var pending = state.pending;
    if (!pending || state.busy) return;
    cancelConfirm();

    state.busy = true;
    updateComposer();
    dom["send-btn"].disabled = true;

    try {
      if (pending.mode === "direct") {
        var result = await apiPost("send", {
          unified_msg_origin: pending.umo,
          content: pending.content,
        });
        if (result && result.ok) {
          var viaFallback = result.path === "fallback";
          dom["composer-input"].value = "";
          autosize();
          if (result.warnings && result.warnings.length) {
            toast(result.warnings.join("；"), "warn");
          } else {
            toast(
              "已发送到「" + pending.alias + "」" + (viaFallback ? "（兜底接口）" : ""),
              "ok"
            );
          }
        } else {
          toast(
            "发送失败：" + ((result && result.error) || "未知原因"),
            "err"
          );
        }
      } else {
        var simResult = await apiPost("simulate", {
          unified_msg_origin: pending.umo,
          content: pending.content,
        });
        if (simResult && simResult.ok) {
          toast("已注入模拟用户消息，留意会话里的 Bot 回复", "ok");
          dom["composer-input"].value = "";
          autosize();
        } else {
          toast(
            "注入失败：" + ((simResult && simResult.error) || "未知原因"),
            "err"
          );
        }
      }
      await loadConversations({ silent: true });
      await loadHistory();
    } catch (error) {
      toast(error.message || "请求失败", "err");
    } finally {
      state.busy = false;
      updateComposer();
    }
  }

  function autosize() {
    var input = dom["composer-input"];
    if (!input) return;
    input.style.height = "auto";
    var next = Math.min(input.scrollHeight, 140);
    input.style.height = Math.max(40, next) + "px";
  }

  // ---------------------------------------------------------------- 会话操作

  function startRename() {
    var record = selectedRecord();
    if (!record) return;
    var host = dom["field-alias"];
    if (!host || host.querySelector("input")) return;

    var originalText = host.textContent;
    var wrap = el("div", "inline-edit");
    var input = document.createElement("input");
    input.type = "text";
    input.className = "mini-input";
    input.maxLength = 64;
    input.value = record.bound ? record.alias : "";
    input.placeholder = "输入备注名";

    var save = el("button", "btn primary", "保存");
    save.type = "button";
    var cancel = el("button", "btn ghost", "取消");
    cancel.type = "button";

    function restore() {
      host.textContent = originalText;
    }

    async function doSave() {
      var alias = input.value.trim();
      if (!alias) {
        toast("备注名不能为空", "warn");
        input.focus();
        return;
      }
      try {
        await apiPost("conversations/bind", {
          unified_msg_origin: record.unified_msg_origin,
          alias: alias,
        });
        toast("已更新备注名", "ok");
        restore();
        await loadConversations({ silent: true });
        updateMainHeader();
        renderInspector();
      } catch (error) {
        toast(error.message || "保存失败", "err");
      }
    }

    save.addEventListener("click", doSave);
    cancel.addEventListener("click", restore);
    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        event.preventDefault();
        doSave();
      } else if (event.key === "Escape") {
        event.preventDefault();
        restore();
      }
    });

    wrap.appendChild(input);
    wrap.appendChild(save);
    wrap.appendChild(cancel);
    host.textContent = "";
    host.appendChild(wrap);
    input.focus();
    input.select();
  }

  async function forgetConversation() {
    var record = selectedRecord();
    if (!record) return;
    if (!window.confirm("从控制台删除「" + record.display_name + "」的登记？\n\n只删除本地记录，不影响 QQ 群聊本身。该会话下次发消息时会重新出现。")) {
      return;
    }
    try {
      await apiPost("conversations/forget", {
        unified_msg_origin: record.unified_msg_origin,
      });
      toast("已删除该会话登记", "ok");
      state.selectedUmo = "";
      state.history = [];
      await loadConversations({ silent: true });
      updateMainHeader();
      renderInspector();
      renderStream();
      updateComposer();
    } catch (error) {
      toast(error.message || "删除失败", "err");
    }
  }

  async function copyUmo() {
    var record = selectedRecord();
    if (!record) return;
    var text = record.unified_msg_origin || "";
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        throw new Error("no clipboard");
      }
      toast("UMO 已复制到剪贴板", "ok");
    } catch (error) {
      toast("复制失败，请手动选中文本：" + text, "warn");
    }
  }

  // ---------------------------------------------------------------- 事件绑定

  function bindEvents() {
    dom["refresh-btn"].addEventListener("click", async function () {
      var btn = dom["refresh-btn"];
      btn.classList.add("spinning");
      try {
        await loadStatus();
        await loadConversations({ silent: true });
        if (state.selectedUmo) await loadHistory();
        toast("已刷新", "ok");
      } finally {
        window.setTimeout(function () {
          btn.classList.remove("spinning");
        }, 400);
      }
    });

    dom.search.addEventListener("input", function () {
      state.search = dom.search.value;
      renderConversations();
    });

    dom["mode-direct"].addEventListener("click", function () {
      setMode("direct");
    });
    dom["mode-simulate"].addEventListener("click", function () {
      setMode("simulate");
    });

    dom["composer-input"].addEventListener("input", function () {
      autosize();
      updateComposer();
      if (state.pending) cancelConfirm();
    });

    dom["composer-input"].addEventListener("keydown", function (event) {
      if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
        event.preventDefault();
        if (state.pending) {
          commitSend();
        } else {
          armSend();
        }
      } else if (event.key === "Escape" && state.pending) {
        event.preventDefault();
        cancelConfirm();
      }
    });

    dom["send-btn"].addEventListener("click", function () {
      if (state.pending) {
        commitSend();
      } else {
        armSend();
      }
    });

    dom["confirm-ok"].addEventListener("click", commitSend);
    dom["confirm-cancel"].addEventListener("click", cancelConfirm);

    dom["toggle-inspector"].addEventListener("click", function () {
      state.inspectorOpen = !state.inspectorOpen;
      dom.shell.classList.toggle("no-inspector", !state.inspectorOpen);
      dom["toggle-inspector"].setAttribute(
        "aria-pressed",
        state.inspectorOpen ? "true" : "false"
      );
    });

    dom["close-inspector"].addEventListener("click", function () {
      state.inspectorOpen = false;
      dom.shell.classList.add("no-inspector");
      dom["toggle-inspector"].setAttribute("aria-pressed", "false");
    });

    dom["rename-btn"].addEventListener("click", startRename);
    dom["forget-btn"].addEventListener("click", forgetConversation);
    dom["copy-umo"].addEventListener("click", copyUmo);

    dom["banner-close"].addEventListener("click", hideBanner);
    dom["banner-toggle"].addEventListener("click", function () {
      dom.banner.hidden = !dom.banner.hidden;
    });

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && state.pending) {
        cancelConfirm();
      }
    });
  }

  // ---------------------------------------------------------------- 启动

  async function boot() {
    cacheDom();
    bindEvents();
    setMode("direct");

    // 窄屏默认收起右栏，避免三栏挤在一起
    if (window.innerWidth < 1100) {
      state.inspectorOpen = false;
      dom.shell.classList.add("no-inspector");
      dom["toggle-inspector"].setAttribute("aria-pressed", "false");
    }

    if (window.parent === window) {
      updateStatusLine("请从 AstrBot WebUI 的插件 Page 打开");
      showBanner(
        "当前页面不在 AstrBot WebUI 的 iframe 里，插件的 Web API 无法访问。请到「插件 → Bot Console → 打开 Page」使用。"
      );
      return;
    }

    try {
      state.bridge = await waitForBridge();
    } catch (error) {
      updateStatusLine("bridge 加载失败");
      toast(error.message, "err");
      return;
    }

    try {
      await state.bridge.ready();
    } catch (error) {
      /* 上下文没到也不影响 API 调用 */
    }

    await loadStatus();
    await loadConversations();
    updateMainHeader();
    renderStream();
    updateComposer();
    autosize();

    if (state.conversations.length && !state.selectedUmo) {
      var first = null;
      for (var i = 0; i < state.conversations.length; i += 1) {
        if (state.conversations[i].bound) {
          first = state.conversations[i];
          break;
        }
      }
      if (!first) first = state.conversations[0];
      if (first) selectConversation(first.unified_msg_origin);
    }

    state.pollTimer = window.setInterval(function () {
      if (state.busy || state.pending) return;
      loadConversations({ silent: true });
    }, POLL_INTERVAL);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();

"use strict";

(() => {
  let snapshot = null;
  let agents = [];
  let plan = null;
  const byId = (id) => document.getElementById(id);

  function option(select, value, label) {
    const node = document.createElement("option");
    node.value = value;
    node.textContent = label;
    select.appendChild(node);
  }

  function notice(id, message, error = false) {
    const node = byId(id);
    node.textContent = message;
    node.classList.toggle("error", error);
  }

  function selectedDevice() {
    const id = byId("device").value;
    return snapshot?.runtime?.devices?.find((item) => item.device_id === id) || null;
  }

  const capabilityLabels = {
    "speech.stop": "停止播报",
    "speaker.volume.set": "扬声器音量",
    "device.status.read": "设备状态",
    "audio.diagnostics.read": "音频诊断",
    "screen.brightness.set": "屏幕亮度",
    "screen.theme.set": "屏幕主题",
    "maintenance.firmware.update": "固件更新",
    "maintenance.assets.update": "资源包更新",
    "network.provisioning.enter": "进入配网",
    "device.reboot": "设备重启",
    "device.factory_reset": "恢复出厂",
  };

  function renderCapabilities(device) {
    const root = byId("capability-list");
    lingxi.clear(root);
    (device?.capability_inventory || []).forEach((item) => {
      const node = document.createElement("div");
      node.className = `capability ${item.available ? "available" : "blocked"}`;
      const title = document.createElement("strong");
      title.textContent = capabilityLabels[item.capability_id] || item.capability_id;
      const status = document.createElement("span");
      status.textContent = item.available ? "可用" : (item.confirmation === "physical_only" ? "仅物理操作" : "未开放");
      const detail = document.createElement("small");
      detail.textContent = item.available ? `${item.effect} · ${item.source}` : item.blocker;
      node.append(title, status, detail);
      root.appendChild(node);
    });
  }

  function renderSnapshot(value) {
    snapshot = value;
    const runtime = value.runtime || {};
    lingxi.text("runtime-status", runtime.status === "ok" ? "运行中" : "未连接");
    const select = byId("device");
    const previous = select.value;
    lingxi.clear(select);
    (runtime.devices || []).forEach((item) => option(select, item.device_id, item.device_id));
    if ([...select.options].some((item) => item.value === previous)) select.value = previous;
    const device = selectedDevice();
    const binding = device?.binding;
    if (binding?.bound) {
      notice("binding-notice", `已绑定 ${binding.host} / ${binding.agent}\n${binding.title || binding.stored_session_id}\n修订号 ${binding.binding_revision}`);
    } else if (device) {
      notice("binding-notice", "设备在线，尚未绑定电脑端会话。");
    } else {
      notice("binding-notice", "当前没有已注册的在线设备。", true);
    }
    lingxi.text("connection-generation", device?.connection_generation ? device.connection_generation.slice(0, 12) : "—");
    lingxi.text("device-volume", device?.device_status?.audio_speaker?.volume ?? "—");
    lingxi.text("network-signal", device?.device_status?.network?.signal || "—");
    renderCapabilities(device);
    const settings = value.settings || {};
    const configured = settings.configured || {};
    const loaded = settings.loaded || {};
    lingxi.text("config-state", settings.pending_restart ? "已写入，等待重启" : "已加载");
    lingxi.text("loaded-llm", loaded.llm_mode || loaded.llm_module || "—");
    lingxi.text("configured-llm", configured.llm_mode || configured.llm_module || "—");
    lingxi.text("configured-voice", configured.tts_voice || "—");
    lingxi.text("observed-state", settings.observed?.available ? "已验证" : "尚未发布");
    const maintenance = value.maintenance || {};
    lingxi.text("maintenance-state", maintenance.executable ? "可执行" : "硬禁用");
    lingxi.text("physical-only", (maintenance.physical_only || []).join(" / ") || "—");
    if ((maintenance.blockers || []).length) {
      notice("maintenance-notice", `当前阻断：\n${maintenance.blockers.join("\n")}`);
    }
    byId("llm-mode").value = configured.llm_mode || "bound_agent";
    const providers = configured.available?.tts_providers || [];
    const tts = byId("tts-provider");
    lingxi.clear(tts);
    providers.forEach((item) => option(tts, item, item));
    tts.value = configured.tts_provider || "";
    byId("tts-voice").value = configured.tts_voice || "";
    byId("vad-silence").value = configured.vad_min_silence_ms ?? "";
    byId("tool-timeout").value = configured.tool_call_timeout_seconds ?? "";
    renderAgents();
  }

  function renderAgents() {
    const host = byId("host").value;
    const select = byId("agent");
    const previous = select.value;
    lingxi.clear(select);
    agents.filter((item) => item.host === host).forEach((item) => option(select, item.id, `${item.name}${item.available ? "" : "（不可用）"}`));
    if ([...select.options].some((item) => item.value === previous)) select.value = previous;
  }

  async function refresh() {
    const [nextSnapshot, inventory] = await Promise.all([
      lingxi.request("/api/snapshot"),
      lingxi.request("/api/agents"),
    ]);
    agents = inventory.agents || [];
    renderSnapshot(nextSnapshot);
  }

  async function loadSessions() {
    const host = byId("host").value;
    const agent = byId("agent").value;
    if (!agent) throw new Error("请先选择智能体");
    const value = await lingxi.request(`/api/sessions?host=${encodeURIComponent(host)}&agent=${encodeURIComponent(agent)}&limit=50`);
    const select = byId("session");
    lingxi.clear(select);
    (value.sessions || []).forEach((item) => option(select, item.id, item.title || item.id));
    notice("binding-notice", `找到 ${value.sessions?.length || 0} 条可绑定会话。`);
  }

  function bindingRevision() {
    return selectedDevice()?.binding?.binding_revision || 0;
  }

  async function bind() {
    const device = byId("device").value;
    const session = byId("session").value;
    if (!device || !session) throw new Error("请选择在线设备和会话");
    await lingxi.request("/api/bind", { method: "POST", body: {
      device_id: device,
      host: byId("host").value,
      agent: byId("agent").value,
      stored_session_id: session,
      expected_binding_revision: bindingRevision(),
      expected_generation: selectedDevice().connection_generation,
    }});
    await refresh();
  }

  async function unbind() {
    const device = byId("device").value;
    if (!device) throw new Error("请选择在线设备");
    await lingxi.request("/api/unbind", { method: "POST", body: {
      device_id: device,
      expected_binding_revision: bindingRevision(),
      expected_generation: selectedDevice().connection_generation,
    }});
    await refresh();
  }

  function configPatch() {
    return {
      llm_mode: byId("llm-mode").value,
      tts_provider: byId("tts-provider").value,
      tts_voice: byId("tts-voice").value,
      vad_min_silence_ms: Number(byId("vad-silence").value),
      tool_call_timeout_seconds: Number(byId("tool-timeout").value),
    };
  }

  async function preview() {
    plan = await lingxi.request("/api/config/preview", { method: "POST", body: {
      patch: configPatch(),
      expected_configured_sha256: snapshot.settings.configured_sha256,
    }});
    byId("apply").disabled = false;
    notice("config-notice", `预览已生成：\n${plan.changes.map((item) => `${item.field}: ${item.before} → ${item.after}`).join("\n")}\n写入后仍需人工确认重启。`);
  }

  async function apply() {
    if (!plan) throw new Error("请先预览配置差异");
    const value = await lingxi.request("/api/config/apply", { method: "POST", body: {
      plan_id: plan.plan_id,
      candidate_sha256: plan.candidate_sha256,
    }});
    plan = null;
    byId("apply").disabled = true;
    notice("config-notice", `配置已原子写入，尚未重启。备份：${value.backup_id}`);
    await refresh();
  }

  async function rollback() {
    const backupId = byId("backup-id").value;
    if (!backupId) throw new Error("请输入备份 ID");
    const value = await lingxi.request("/api/config/rollback", { method: "POST", body: {
      backup_id: backupId,
      expected_configured_sha256: snapshot.settings.configured_sha256,
    }});
    notice("config-notice", `已回滚到备份，仍需人工确认重启。新的保护备份：${value.backup_id}`);
    await refresh();
  }

  async function diagnostics() {
    const current = selectedDevice();
    if (!current) throw new Error("请选择在线设备");
    const value = await lingxi.request("/api/diagnostics", { method: "POST", body: {
      device_id: current.device_id,
      expected_generation: current.connection_generation,
    }});
    const rows = Object.entries(value.diagnostics || {}).map(([key, item]) => `${key}: ${item}`);
    notice("diagnostics-notice", rows.length ? rows.join("\n") : "设备未返回可显示的诊断指标。");
  }

  function run(action, noticeId = "binding-notice") {
    return async () => {
      try { await action(); }
      catch (error) { notice(noticeId, error.message, true); }
    };
  }

  function showLogin() {
    byId("login").hidden = false;
    byId("login-password").focus();
  }

  lingxi.setUnauthorizedHandler(showLogin);

  byId("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const password = byId("login-password");
    try {
      await lingxi.login(password.value);
      password.value = "";
      byId("login").hidden = true;
      await refresh();
    } catch (error) {
      password.value = "";
      notice("login-notice", error.message, true);
    }
  });
  byId("host").addEventListener("change", renderAgents);
  byId("device").addEventListener("change", () => renderSnapshot(snapshot));
  byId("load-sessions").addEventListener("click", run(loadSessions));
  byId("bind").addEventListener("click", run(bind));
  byId("unbind").addEventListener("click", run(unbind));
  byId("preview").addEventListener("click", run(preview, "config-notice"));
  byId("apply").addEventListener("click", run(apply, "config-notice"));
  byId("rollback").addEventListener("click", run(rollback, "config-notice"));
  byId("diagnostics").addEventListener("click", run(diagnostics, "diagnostics-notice"));
  byId("refresh").addEventListener("click", run(refresh, "config-notice"));

  lingxi.session().then(() => { byId("login").hidden = true; return refresh(); }).catch(showLogin);
})();

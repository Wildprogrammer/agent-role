"use strict";

(() => {
  let device = null;
  let pending = null;
  let lastEventId = 0;
  let pollTimer = null;
  let liveTimer = null;
  let uiTimer = null;
  let lastOnline = null;
  let eventExpression = "neutral";
  let subscription = null;
  let subscriptionDevice = "";
  let liveResumeToken = "";
  let liveResumeDevice = "";
  let liveCursor = 0;
  let liveSyncPromise = null;
  let livePollInFlight = false;
  let viewGeneration = 0;
  let dialogueClearTimer = null;
  let moodTimer = null;
  let moodClearTimer = null;
  let moodIndex = 0;
  let butterflyIndex = 0;
  const textFrames = { question: null, answer: null };
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const UI_IDLE_MS = 4000;
  const LIVE_POLL_MS = 500;
  const COMPLETED_DIALOGUE_MS = 60000;
  const byId = (id) => document.getElementById(id);
  const liveState = {
    privacy: false,
    captureActive: false,
    speechActive: false,
    turnId: "",
    revision: 0,
    question: "",
    answer: "",
    answerTruncated: false,
    answerHeadLength: 0,
    answerOmittedCharacters: 0,
  };
  const labels = {
    neutral: "我在听", listening: "正在聆听", thinking: "正在思考",
    speaking: "正在回答", question: "等待确认", happy: "完成啦",
    error: "遇到问题", offline: "等待设备", pairing: "正在配网",
    updating: "正在更新", audio_testing: "音频检查", privacy: "隐私模式",
  };

  function notice(message, error = false) {
    byId("notice").textContent = message;
    byId("notice").classList.toggle("error", error);
  }

  function scheduleUiHide() {
    if (uiTimer !== null) window.clearTimeout(uiTimer);
    uiTimer = null;
    if (pending?.state === "prepared" || !byId("login").hidden) return;
    uiTimer = window.setTimeout(() => byId("companion").classList.add("ui-hidden"), UI_IDLE_MS);
  }

  function showUi() {
    byId("companion").classList.remove("ui-hidden");
    scheduleUiHide();
  }

  function releaseButterfly(event) {
    if (!byId("login").hidden || event.button !== 0) return;
    const stage = byId("butterfly-stage");
    const paths = [
      { mx: "28px", my: "-56px", dx: "72px", dy: "-124px", midSpin: "-6deg", spin: "18deg" },
      { mx: "-26px", my: "-60px", dx: "-68px", dy: "-132px", midSpin: "8deg", spin: "-22deg" },
      { mx: "16px", my: "-66px", dx: "42px", dy: "-146px", midSpin: "-4deg", spin: "9deg" },
    ];
    const path = paths[butterflyIndex % paths.length];
    const butterfly = document.createElement("span");
    const body = document.createElement("i");
    butterfly.className = `click-butterfly tone-${butterflyIndex % 3}`;
    butterfly.setAttribute("aria-hidden", "true");
    butterfly.style.setProperty("--x", `${event.clientX}px`);
    butterfly.style.setProperty("--y", `${event.clientY}px`);
    butterfly.style.setProperty("--mx", path.mx);
    butterfly.style.setProperty("--my", path.my);
    butterfly.style.setProperty("--dx", path.dx);
    butterfly.style.setProperty("--dy", path.dy);
    butterfly.style.setProperty("--mid-spin", path.midSpin);
    butterfly.style.setProperty("--spin", path.spin);
    body.className = "butterfly-body";
    butterfly.append(body);
    while (stage.childElementCount >= 8) stage.firstElementChild?.remove();
    stage.append(butterfly);
    butterflyIndex += 1;
    window.setTimeout(() => butterfly.remove(), reducedMotion.matches ? 800 : 1800);
  }

  function setFaceState(value) {
    const safe = labels[value] ? value : "neutral";
    byId("face").dataset.state = safe;
    lingxi.text("state-title", labels[safe]);
  }

  function effectiveExpression() {
    if (!device?.online) return "offline";
    if (liveState.privacy) return "privacy";
    if (liveState.captureActive) return "listening";
    if (liveState.speechActive) return "speaking";
    if (pending?.state === "prepared") return "question";
    return eventExpression === "speaking" ? "neutral" : eventExpression;
  }

  function clearMood() {
    if (moodTimer !== null) window.clearTimeout(moodTimer);
    if (moodClearTimer !== null) window.clearTimeout(moodClearTimer);
    moodTimer = null;
    moodClearTimer = null;
    byId("face").dataset.mood = "none";
  }

  function scheduleMood(delay = 2400) {
    clearMood();
    if (!liveState.speechActive || liveState.privacy || reducedMotion.matches) return;
    const generation = viewGeneration;
    moodTimer = window.setTimeout(() => {
      if (generation !== viewGeneration || !liveState.speechActive || liveState.privacy) return;
      const moods = ["sparkle", "wink", "bounce", "cheeky"];
      byId("face").dataset.mood = moods[moodIndex % moods.length];
      moodIndex += 1;
      moodClearTimer = window.setTimeout(() => {
        if (generation !== viewGeneration) return;
        byId("face").dataset.mood = "none";
        if (liveState.speechActive) scheduleMood(12000);
      }, 950);
    }, delay);
  }

  function renderActivity() {
    const mic = byId("mic-indicator");
    const micActive = liveState.captureActive && !liveState.privacy && Boolean(device?.online);
    mic.hidden = !micActive;
    mic.classList.toggle("active", micActive);
    byId("face").classList.toggle("capturing", micActive);
    setFaceState(effectiveExpression());
    if (liveState.speechActive && byId("face").dataset.mood === "none" && moodTimer === null) scheduleMood();
    else if (!liveState.speechActive || liveState.privacy) clearMood();
  }

  function cancelTextAnimation(kind) {
    const frame = textFrames[kind];
    if (frame !== null) window.cancelAnimationFrame(frame);
    textFrames[kind] = null;
  }

  function cancelTextAnimations() {
    cancelTextAnimation("question");
    cancelTextAnimation("answer");
  }

  function cancelDialogueClear() {
    if (dialogueClearTimer !== null) window.clearTimeout(dialogueClearTimer);
    dialogueClearTimer = null;
  }

  function scheduleDialogueClear(ignoreActiveSpeech = false) {
    if (
      dialogueClearTimer !== null
      || (!liveState.question && !liveState.answer)
      || (!ignoreActiveSpeech && liveState.speechActive)
      || liveState.privacy
    ) return;
    const generation = viewGeneration;
    const turnId = liveState.turnId;
    dialogueClearTimer = window.setTimeout(() => {
      dialogueClearTimer = null;
      if (generation !== viewGeneration || turnId !== liveState.turnId || liveState.privacy) return;
      clearDialogue();
      renderActivity();
    }, COMPLETED_DIALOGUE_MS);
  }

  function graphemes(value) {
    if (window.Intl?.Segmenter) {
      const segmenter = new Intl.Segmenter("zh-CN", { granularity: "grapheme" });
      return Array.from(segmenter.segment(value), (item) => item.segment);
    }
    return Array.from(value);
  }

  function scrollDialogueToLatest() {
    const dialogue = byId("live-dialogue");
    dialogue.scrollTop = dialogue.scrollHeight;
  }

  function animateText(kind, value, truncated, turnId, revision, headLength = 0, omitted = 0) {
    const node = byId(`${kind}-text`);
    const line = byId(`${kind}-line`);
    const cut = byId(`${kind}-cut`);
    const gap = kind === "answer" ? byId("answer-gap") : null;
    const tail = kind === "answer" ? byId("answer-tail") : null;
    cancelTextAnimation(kind);
    line.hidden = !value;
    cut.hidden = !value || !truncated || omitted > 0;
    if (gap) { gap.hidden = true; gap.textContent = ""; }
    if (tail) tail.textContent = "";
    if (!value) { node.textContent = ""; return; }
    byId("live-dialogue").hidden = false;
    if (kind === "answer" && truncated && omitted > 0 && headLength > 0) {
      const codePoints = Array.from(value);
      node.textContent = codePoints.slice(0, headLength).join("");
      gap.textContent = `中间省略 ${omitted.toLocaleString("zh-CN")} 个字符，完整内容请查看智能体会话`;
      gap.hidden = false;
      tail.textContent = codePoints.slice(headLength).join("");
      scrollDialogueToLatest();
      return;
    }
    const parts = graphemes(value);
    const current = node.textContent || "";
    let index = value.startsWith(current) ? graphemes(current).length : 0;
    if (reducedMotion.matches) index = parts.length;
    node.textContent = parts.slice(0, index).join("");
    scrollDialogueToLatest();
    if (index >= parts.length) return;
    const generation = viewGeneration;
    const duration = Math.min(5200, Math.max(420, parts.length * (kind === "question" ? 24 : 13)));
    const step = Math.max(1, Math.ceil(parts.length / Math.max(1, duration / 16)));
    const tick = () => {
      if (generation !== viewGeneration || turnId !== liveState.turnId || revision !== liveState.revision || liveState.privacy) return;
      index = Math.min(parts.length, index + step);
      node.textContent = parts.slice(0, index).join("");
      scrollDialogueToLatest();
      if (index < parts.length) textFrames[kind] = window.requestAnimationFrame(tick);
      else textFrames[kind] = null;
    };
    textFrames[kind] = window.requestAnimationFrame(tick);
  }

  function clearDialogue() {
    cancelDialogueClear();
    cancelTextAnimations();
    liveState.turnId = "";
    liveState.revision = 0;
    liveState.question = "";
    liveState.answer = "";
    liveState.answerTruncated = false;
    liveState.answerHeadLength = 0;
    liveState.answerOmittedCharacters = 0;
    lingxi.text("question-text", "");
    lingxi.text("answer-text", "");
    lingxi.text("answer-tail", "");
    lingxi.text("answer-gap", "");
    byId("answer-gap").hidden = true;
    byId("question-line").hidden = true;
    byId("answer-line").hidden = true;
    byId("question-cut").hidden = true;
    byId("answer-cut").hidden = true;
    byId("live-dialogue").hidden = true;
  }

  function applyProjection(projection) {
    if (!projection || liveState.privacy) return;
    const nextTurn = typeof projection.turn_id === "string" ? projection.turn_id : "";
    const question = typeof projection.question === "string" ? projection.question : "";
    const answer = typeof projection.answer === "string" ? projection.answer : "";
    const hasDialogue = Boolean(question || answer);
    if (!nextTurn && liveState.turnId) {
      viewGeneration += 1;
      clearDialogue();
    } else if (nextTurn !== liveState.turnId && hasDialogue) {
      viewGeneration += 1;
      clearDialogue();
      liveState.turnId = nextTurn;
    }
    liveState.captureActive = projection.capture_active === true;
    liveState.speechActive = projection.speech_active === true;
    const revision = Number.isInteger(projection.revision) ? projection.revision : 0;
    if (nextTurn && nextTurn === liveState.turnId && revision >= liveState.revision) {
      liveState.revision = revision;
      if (question !== liveState.question) {
        liveState.question = question;
        animateText("question", question, projection.question_truncated === true, nextTurn, revision);
      }
      const answerTruncated = projection.answer_truncated === true;
      const answerHeadLength = Number.isInteger(projection.answer_head_length) ? projection.answer_head_length : 0;
      const answerOmittedCharacters = Number.isInteger(projection.answer_omitted_characters) ? projection.answer_omitted_characters : 0;
      if (answer !== liveState.answer || answerTruncated !== liveState.answerTruncated || answerHeadLength !== liveState.answerHeadLength || answerOmittedCharacters !== liveState.answerOmittedCharacters) {
        liveState.answer = answer;
        liveState.answerTruncated = answerTruncated;
        liveState.answerHeadLength = answerHeadLength;
        liveState.answerOmittedCharacters = answerOmittedCharacters;
        animateText("answer", answer, answerTruncated, nextTurn, revision, answerHeadLength, answerOmittedCharacters);
      }
      byId("live-dialogue").hidden = !question && !answer;
    }
    const projectionTargetsDisplayedTurn = Boolean(nextTurn && nextTurn === liveState.turnId);
    if (projectionTargetsDisplayedTurn && liveState.speechActive) cancelDialogueClear();
    else if (projectionTargetsDisplayedTurn && liveState.answer) scheduleDialogueClear();
    else if (!projectionTargetsDisplayedTurn) scheduleDialogueClear(true);
    renderActivity();
  }

  async function unsubscribeLive(notifyServer = true, preserveResume = false) {
    const previous = subscription;
    const previousResume = liveResumeToken;
    const previousDevice = subscriptionDevice || liveResumeDevice;
    const previousCursor = liveCursor;
    subscription = null;
    subscriptionDevice = "";
    if (preserveResume && previousResume) {
      liveResumeDevice = previousDevice;
      liveCursor = previousCursor;
    } else {
      liveResumeToken = "";
      liveResumeDevice = "";
      liveCursor = 0;
    }
    if (notifyServer && (previous || previousResume)) {
      try {
        await lingxi.request("/api/live/unsubscribe", {
          method: "POST",
          body: { subscription: previous || "", resume_token: previousResume || "" },
        });
      } catch (_error) {
        // Expired and disconnected subscriptions are already gone server-side.
      }
    }
  }

  async function syncLiveSubscriptionNow() {
    const eligible = !liveState.privacy && device?.online && device.connection_generation;
    const key = eligible ? `${device.device_id}\u0000${device.connection_generation}` : "";
    if (!eligible) { if (subscription || liveResumeToken) await unsubscribeLive(); return; }
    if (subscription && subscriptionDevice === key) return;
    if (subscription) await unsubscribeLive();
    const generation = viewGeneration;
    const initialBody = { device_id: device.device_id, connection_generation: device.connection_generation };
    const canResume = Boolean(liveResumeToken && liveResumeDevice === key);
    let value;
    try {
      value = await lingxi.request("/api/live/subscribe", {
        method: "POST",
        body: canResume ? { ...initialBody, resume_token: liveResumeToken, after: liveCursor } : initialBody,
      });
    } catch (error) {
      if (!canResume || error.code !== "resume_unavailable") throw error;
      liveResumeToken = "";
      liveResumeDevice = "";
      liveCursor = 0;
      value = await lingxi.request("/api/live/subscribe", { method: "POST", body: initialBody });
    }
    if (generation !== viewGeneration || liveState.privacy) {
      try {
        await lingxi.request("/api/live/unsubscribe", {
          method: "POST",
          body: { subscription: value.subscription, resume_token: value.resume_token },
        });
      } catch (_error) {
        // The short-lived subscription will also expire automatically.
      }
      return;
    }
    subscription = value.subscription;
    subscriptionDevice = key;
    liveResumeToken = value.resume_token;
    liveResumeDevice = key;
    liveCursor = value.cursor || 0;
  }

  async function syncLiveSubscription() {
    if (liveSyncPromise) return liveSyncPromise;
    liveSyncPromise = syncLiveSubscriptionNow();
    try { return await liveSyncPromise; }
    finally { liveSyncPromise = null; }
  }

  async function pollLive() {
    if (!subscription || liveState.privacy || livePollInFlight) return;
    livePollInFlight = true;
    const requestedSubscription = subscription;
    const requestedDevice = subscriptionDevice;
    const requestedGeneration = viewGeneration;
    try {
      const value = await lingxi.request(`/api/live?subscription=${encodeURIComponent(requestedSubscription)}&after=${liveCursor}`);
      if (subscription !== requestedSubscription || subscriptionDevice !== requestedDevice || viewGeneration !== requestedGeneration || liveState.privacy) return;
      liveCursor = Math.max(liveCursor, value.cursor || 0);
      if (value.connected === false) {
        await unsubscribeLive(false);
        liveState.captureActive = false;
        liveState.speechActive = false;
        clearDialogue();
        renderActivity();
        return;
      }
      if (value.changed) applyProjection(value.projection);
    } catch (error) {
      if (error.code === "live_rate_limited") return;
      if (error.code === "subscription_unavailable") {
        if (subscription !== requestedSubscription || subscriptionDevice !== requestedDevice) return;
        await unsubscribeLive(false, true);
        await syncLiveSubscription();
        return;
      }
      throw error;
    } finally {
      livePollInFlight = false;
    }
  }

  async function render(snapshot) {
    const devices = (snapshot.runtime?.devices || []).filter((item) => item?.online);
    const nextDevice = devices.length === 1 ? devices[0] : null;
    const previousKey = device ? `${device.device_id}\u0000${device.connection_generation}` : "";
    const nextKey = nextDevice ? `${nextDevice.device_id}\u0000${nextDevice.connection_generation}` : "";
    if (previousKey && previousKey !== nextKey) {
      viewGeneration += 1;
      clearDialogue();
      liveState.captureActive = false;
      liveState.speechActive = false;
      eventExpression = "neutral";
      await unsubscribeLive();
    }
    device = nextDevice;
    pending = device?.pending_action || null;
    const online = Boolean(device?.online);
    lingxi.text("device-id", online ? device.device_id : devices.length > 1 ? "多设备" : "离线");
    const binding = device?.binding;
    lingxi.text("binding-title", binding?.bound ? `${binding.host} / ${binding.agent} · ${binding.title || "当前会话"}` : "尚未绑定电脑端会话");
    byId("stop").disabled = !device?.capabilities?.stop_speaking;
    byId("volume").disabled = !device?.capabilities?.set_volume;
    byId("confirmation").hidden = !pending || pending.state !== "prepared";
    if (devices.length > 1) notice("检测到多个在线设备，请只保留一台灵溪连接", true);
    else notice(online ? "灵溪设备已上线" : "正在等待灵溪设备上线……");
    renderActivity();
    await syncLiveSubscription();
    if (lastOnline !== online || pending?.state === "prepared") showUi();
    lastOnline = online;
  }

  async function refresh() {
    await render(await lingxi.request("/api/snapshot"));
    const events = await lingxi.request(`/api/events?after=${lastEventId}`);
    for (const event of events.events || []) {
      lastEventId = Math.max(lastEventId, event.event_id || 0);
      if (device && (!event.device_id || event.device_id === device.device_id)) eventExpression = labels[event.expression] ? event.expression : "neutral";
    }
    renderActivity();
  }

  async function post(path, body) {
    try { await lingxi.request(path, { method: "POST", body }); await refresh(); }
    catch (error) { notice(error.message, true); eventExpression = "error"; renderActivity(); }
  }

  function stopPolling() {
    if (pollTimer !== null) window.clearInterval(pollTimer);
    if (liveTimer !== null) window.clearInterval(liveTimer);
    pollTimer = null;
    liveTimer = null;
  }

  function showLogin() {
    stopPolling();
    viewGeneration += 1;
    void unsubscribeLive(false);
    clearDialogue();
    liveState.captureActive = false;
    liveState.speechActive = false;
    clearMood();
    if (uiTimer !== null) window.clearTimeout(uiTimer);
    uiTimer = null;
    byId("companion").classList.remove("ui-hidden");
    byId("login").hidden = false;
    byId("login-password").focus();
  }

  function startPolling() {
    stopPolling();
    pollTimer = window.setInterval(() => refresh().catch(() => {
      viewGeneration += 1;
      clearDialogue();
      liveState.captureActive = false;
      liveState.speechActive = false;
      eventExpression = "offline";
      renderActivity();
    }), 1000);
    liveTimer = window.setInterval(() => pollLive().catch(() => {}), LIVE_POLL_MS);
  }

  async function setPrivacy(enabled) {
    liveState.privacy = enabled;
    viewGeneration += 1;
    byId("companion").classList.toggle("private", enabled);
    lingxi.text("privacy", enabled ? "退出隐私" : "隐私模式");
    clearDialogue();
    liveState.captureActive = false;
    liveState.speechActive = false;
    clearMood();
    if (enabled) await unsubscribeLive();
    else await syncLiveSubscription();
    renderActivity();
  }

  function motionPreferenceChanged() {
    cancelTextAnimations();
    if (liveState.question) lingxi.text("question-text", liveState.question);
    if (liveState.answer) animateText(
      "answer",
      liveState.answer,
      liveState.answerTruncated,
      liveState.turnId,
      liveState.revision,
      liveState.answerHeadLength,
      liveState.answerOmittedCharacters,
    );
    if (reducedMotion.matches) clearMood();
    else if (liveState.speechActive) scheduleMood();
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
      startPolling();
    } catch (error) {
      password.value = "";
      lingxi.text("login-notice", error.message);
    }
  });
  byId("stop").addEventListener("click", () => device && post("/api/stop", { device_id: device.device_id, expected_generation: device.connection_generation }));
  byId("volume").addEventListener("change", () => device && post("/api/volume", { device_id: device.device_id, volume: Number(byId("volume").value), expected_generation: device.connection_generation }));
  byId("confirm").addEventListener("click", () => device && pending && post("/api/action", { device_id: device.device_id, action_id: pending.action_id, decision: "confirm", expected_generation: device.connection_generation }));
  byId("cancel").addEventListener("click", () => device && pending && post("/api/action", { device_id: device.device_id, action_id: pending.action_id, decision: "cancel", expected_generation: device.connection_generation }));
  byId("privacy").addEventListener("click", () => setPrivacy(!liveState.privacy).catch((error) => notice(error.message, true)));
  byId("fullscreen").addEventListener("click", () => document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen());
  window.addEventListener("pointerdown", releaseButterfly, { passive: true });
  for (const eventName of ["pointermove", "pointerdown", "keydown", "touchstart"]) window.addEventListener(eventName, showUi, { passive: true });
  if (typeof reducedMotion.addEventListener === "function") reducedMotion.addEventListener("change", motionPreferenceChanged);

  lingxi.session()
    .then(() => { byId("login").hidden = true; return refresh(); })
    .then(() => { startPolling(); showUi(); })
    .catch(showLogin);
})();

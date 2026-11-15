/* Lingxi presentation controller. No network APIs, conversations or device writes. */
"use strict";
(() => {
  const clips = Object.freeze({
    neutral: "idle", listening: "look", thinking: "think", speaking: "talk",
    question: "wait", happy: "success", error: "error", offline: "wait",
    pairing: "look", updating: "work", audio_testing: "work", privacy: "sleep",
  });
  const idleClips = ["look", "stretch", "magic", "sleep"];
  const clickClips = ["wave", "shy", "laugh", "magic"];
  const labels = Object.freeze({
    neutral: "陪你待机", listening: "认真聆听", thinking: "思考中", speaking: "正在说话",
    question: "等你确认", happy: "完成啦", error: "遇到问题", offline: "等待连接",
    pairing: "正在配网", updating: "更新中", audio_testing: "检查音频", privacy: "安静休息",
  });

  function mount(root) {
    if (!root) return null;
    const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
    let video, state = "offline", current = "", ready = false;
    let idleTimer, actionTimer, loadTimer, stallTimer, idleIndex = 0, clickIndex = 0;
    let suspended = true, disposed = false, alphaSupported, assetsAvailable = false;
    if (typeof window.fetch === "function") {
      window.fetch("/static/lingxi-pet-assets.json", {cache: "no-store"})
        .then(response => response.ok ? response.json() : null)
        .then(manifest => {
          if (disposed || manifest?.version !== "0.4.7") return;
          assetsAvailable = true;
          visibilityChanged();
        })
        .catch(() => { /* Optional media must not affect the interface. */ });
    }
    root.setAttribute("role", "button");
    root.setAttribute("tabindex", "0");
    root.setAttribute("title", "点击和灵溪打招呼");

    function clearTimers() {
      window.clearTimeout(idleTimer);
      window.clearTimeout(actionTimer);
      idleTimer = actionTimer = undefined;
    }
    function hidden() { return suspended || document.hidden || disposed; }
    function showFallback(media) {
      video?.classList.remove("visible");
      root.classList.remove("pet-ready");
      root.dataset.media = media;
    }
    function releaseVideo() {
      window.clearTimeout(loadTimer);
      window.clearTimeout(stallTimer);
      showFallback("fallback");
      ready = false;
      const old = video;
      video = undefined;
      current = "";
      if (!old) return;
      old.pause();
      old.removeAttribute("src");
      old.load();
      old.remove();
    }
    function play(clip, once = false) {
      if (hidden() || current === clip) return;
      releaseVideo();
      current = clip;
      root.dataset.clip = clip;
      if (!assetsAvailable || motion.matches || alphaSupported === false) return;

      const candidate = document.createElement("video");
      candidate.className = "pet-video";
      candidate.muted = candidate.defaultMuted = true;
      candidate.playsInline = true;
      candidate.controls = false;
      candidate.disableRemotePlayback = true;
      candidate.preload = "auto";
      candidate.loop = !once;
      candidate.setAttribute("playsinline", "");
      candidate.setAttribute("webkit-playsinline", "");
      candidate.setAttribute("aria-hidden", "true");
      candidate.setAttribute("disablepictureinpicture", "");
      candidate.setAttribute("disableremoteplayback", "");
      candidate.setAttribute("tabindex", "-1");
      if (candidate.canPlayType && !candidate.canPlayType('video/webm; codecs="vp9"')) return;
      video = candidate;
      root.append(candidate);
      showFallback("loading");
      let frameRequest = null;
      const isCurrent = () => video === candidate && !hidden();
      const fail = () => {
        if (!isCurrent()) return;
        releaseVideo();
        current = clip;
        if (once) returnToState();
      };
      const reveal = () => {
        frameRequest = null;
        if (!isCurrent() || candidate.paused || candidate.readyState < 2) return;
        // The bundled clips have a transparent top-left pixel. VP9 playback
        // support alone does not guarantee that the decoder preserves alpha.
        if (alphaSupported === undefined) {
          const probe = document.createElement("canvas");
          probe.width = probe.height = 1;
          const context = probe.getContext?.("2d", {willReadFrequently: true});
          if (context) {
            try {
              context.drawImage(candidate, 0, 0, 1, 1, 0, 0, 1, 1);
              alphaSupported = context.getImageData(0, 0, 1, 1).data[3] < 32;
            } catch { /* The sampled frame may not be readable on every browser. */ }
          }
        }
        if (alphaSupported === false) { fail(); return; }
        window.clearTimeout(loadTimer);
        window.clearTimeout(stallTimer);
        ready = true;
        candidate.classList.add("visible");
        root.classList.add("pet-ready");
        root.dataset.media = "ready";
      };
      candidate.onplaying = () => {
        if (!isCurrent()) return;
        if (candidate.requestVideoFrameCallback) {
          if (frameRequest === null) frameRequest = candidate.requestVideoFrameCallback(reveal);
        } else reveal();
      };
      candidate.onwaiting = () => {
        if (!isCurrent() || !ready) return;
        if (frameRequest !== null && candidate.cancelVideoFrameCallback) {
          candidate.cancelVideoFrameCallback(frameRequest);
          frameRequest = null;
        }
        window.clearTimeout(stallTimer);
        stallTimer = window.setTimeout(() => {
          if (!isCurrent()) return;
          showFallback("loading");
          stallTimer = window.setTimeout(fail, 7000);
        }, 450);
      };
      candidate.onended = () => { if (isCurrent() && once) returnToState(); };
      candidate.onerror = fail;
      loadTimer = window.setTimeout(fail, 7000);
      candidate.src = `/static/lingxi-pet-${clip}.webm?v=0.4.7`;
      Promise.resolve(candidate.play()).catch(fail);
    }
    function scheduleIdle() {
      window.clearTimeout(idleTimer);
      if (!assetsAvailable || state !== "neutral" || hidden() || motion.matches) return;
      idleTimer = window.setTimeout(() => {
        if (state !== "neutral" || hidden()) return;
        oneShot(idleClips[idleIndex++ % idleClips.length]);
      }, 24000);
    }
    function returnToState() {
      window.clearTimeout(actionTimer);
      current = "";
      play(clips[state]);
      scheduleIdle();
    }
    function oneShot(clip) {
      if (!assetsAvailable || state !== "neutral" || hidden() || motion.matches) return;
      clearTimers();
      play(clip, true);
      // A malformed/looping clip must not trap the character in an interaction.
      actionTimer = window.setTimeout(returnToState, 12000);
    }
    function setState(value) {
      const next = Object.hasOwn(clips, value) ? value : "neutral";
      root.dataset.state = next;
      root.setAttribute("aria-label", `灵溪 · ${labels[next]}。空闲时可点击互动。`);
      if (next === state && current) return;
      clearTimers();
      state = next;
      play(clips[state]);
      scheduleIdle();
    }
    function setSuspended(value) {
      if (disposed || suspended === value) return;
      suspended = value;
      visibilityChanged();
    }
    function visibilityChanged() {
      clearTimers();
      releaseVideo();
      if (!hidden()) { play(clips[state]); scheduleIdle(); }
    }
    function motionChanged() { visibilityChanged(); }
    function interact() { oneShot(clickClips[clickIndex++ % clickClips.length]); }
    function keydown(event) {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); interact(); }
    }
    root.addEventListener("click", interact);
    root.addEventListener("keydown", keydown);
    document.addEventListener("visibilitychange", visibilityChanged);
    motion.addEventListener("change", motionChanged);
    return Object.freeze({
      setState, setSuspended,
      dispose() {
        disposed = true;
        clearTimers();
        releaseVideo();
        root.removeEventListener("click", interact); root.removeEventListener("keydown", keydown);
        document.removeEventListener("visibilitychange", visibilityChanged);
        motion.removeEventListener("change", motionChanged);
      },
    });
  }
  function mountScreen({face, microphone, dialogue, status}) {
    function make(tag, className, text = "") {
      const node = document.createElement(tag);
      node.className = className;
      node.textContent = text;
      return node;
    }
    const fallback = make("div", "face");
    fallback.append(make("span", "eye left"), make("span", "eye right"), make("span", "mouth"));
    const wave = make("span", "pet-wave");
    wave.setAttribute("aria-hidden", "true");
    wave.append(...Array.from({length: 9}, () => make("i", "")));
    face.append(fallback, wave);

    const micWave = make("span", "mic-wave");
    micWave.append(...Array.from({length: 4}, () => make("i", "")));
    microphone.append(make("span", "mic-glyph"), micWave, make("span", "", "正在听你说"));

    const rows = Object.fromEntries([["question", "你"], ["answer", "灵溪"]].map(([kind, label]) => {
      const line = make("article", `dialogue-line ${kind}-line`);
      const paragraph = make("p", "");
      const text = make("span", ""), cut = make("span", "caption-cut", "内容已截断");
      const gap = make("span", "caption-gap"), tail = make("span", "");
      text.id = `${kind}-text`;
      gap.hidden = cut.hidden = true;
      paragraph.append(text, gap, tail);
      line.append(make("span", "dialogue-role", label), paragraph, cut);
      line.hidden = true;
      dialogue.append(line);
      return [kind, {line, text, gap, tail, cut, frame: null}];
    }));
    const pet = mount(face);
    const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
    const segmenter = window.Intl?.Segmenter && new Intl.Segmenter("zh-CN", {granularity: "grapheme"});
    const graphemes = value => segmenter
      ? Array.from(segmenter.segment(value), item => item.segment) : Array.from(value);
    function scrollDialogueToLatest() { dialogue.scrollTop = dialogue.scrollHeight; }
    function cancel(row) {
      if (row.frame !== null) window.cancelAnimationFrame(row.frame);
      row.frame = null;
    }
    function caption(kind, value, {truncated = false, headLength = 0, omitted = 0, animate = true} = {}) {
      const row = rows[kind];
      cancel(row);
      value = typeof value === "string" ? value : "";
      const split = kind === "answer" && truncated && omitted > 0 && headLength > 0;
      row.line.hidden = !value;
      row.cut.hidden = !value || !truncated || split;
      row.gap.hidden = !split;
      row.gap.textContent = "";
      row.tail.textContent = "";
      dialogue.hidden = rows.question.line.hidden && rows.answer.line.hidden;
      if (split) {
        const points = Array.from(value);
        row.text.textContent = points.slice(0, headLength).join("");
        row.gap.textContent = `中间省略 ${omitted.toLocaleString("zh-CN")} 个字符，完整内容请查看智能体会话`;
        row.tail.textContent = points.slice(headLength).join("");
      } else if (!value || !animate || motion.matches) {
        row.text.textContent = value;
      } else {
        const parts = graphemes(value), current = row.text.textContent || "";
        let index = value.startsWith(current) ? graphemes(current).length : 0;
        row.text.textContent = parts.slice(0, index).join("");
        const duration = Math.min(5200, Math.max(420, parts.length * (kind === "question" ? 24 : 13)));
        const step = Math.max(1, Math.ceil(parts.length / (duration / 16)));
        function tick() {
          index = motion.matches ? parts.length : Math.min(parts.length, index + step);
          row.text.textContent = parts.slice(0, index).join("");
          scrollDialogueToLatest();
          row.frame = index < parts.length ? window.requestAnimationFrame(tick) : null;
        }
        if (index < parts.length) row.frame = window.requestAnimationFrame(tick);
      }
      if (value) scrollDialogueToLatest();
    }
    return Object.freeze({
      caption,
      clearCaptions() { caption("question", ""); caption("answer", ""); },
      setState(name, label, {capturing = false, playing = false, suspended = false} = {}) {
        if (status.textContent !== label) status.textContent = label;
        microphone.hidden = !capturing;
        face.classList.toggle("capturing", capturing);
        face.dataset.playing = String(playing);
        pet.setState(name);
        pet.setSuspended(suspended);
      },
    });
  }
  window.LingxiPet = Object.freeze({mount, mountScreen});
})();

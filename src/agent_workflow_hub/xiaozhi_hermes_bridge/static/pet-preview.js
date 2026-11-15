"use strict";
(() => {
  const entries = [
    ["neutral", "陪伴待机", "呼吸 / 小动作", "我在呢，今天想聊些什么？"],
    ["listening", "认真聆听", "转头 / 注视", "嗯，我在认真听。"],
    ["thinking", "思考中", "冒泡 / 想一想", "让我想一想……"],
    ["speaking", "开口回答", "说话 / 声波", "找到啦，我来慢慢讲给你听。"],
    ["question", "等待确认", "踱步 / 张望", "这一步，等你来确认。"],
    ["happy", "任务完成", "雀跃 / 庆祝", "完成啦！"],
    ["error", "遇到问题", "叹气 / 冒汗", "遇到一点问题，我们看看提示吧。"],
    ["offline", "连接断开", "等待 / 低饱和", "等连接恢复，我们再继续。"],
    ["privacy", "安静休息", "睡觉 / 隐私", "我安静待着，不展示对话。"],
    ["pairing", "正在配网", "寻找 / 张望", "正在等网络准备好。"],
    ["updating", "正在更新", "忙碌 / 点按", "正在更新，请稍等。"],
    ["audio_testing", "音频检查", "专心 / 点按", "正在检查音频。"],
  ];
  const pet = window.LingxiPet.mount(document.getElementById("face"));
  const panel = document.getElementById("preview-states");
  function select(entry) {
    pet.setState(entry[0]);
    document.getElementById("preview-caption").textContent = entry[3];
    document.getElementById("preview-detail").textContent = entry[0] === "neutral"
      ? "空闲时点一下灵溪，看看她的回应。" : "预览状态，不代表设备或智能体当前状态。";
    for (const button of panel.children) button.setAttribute("aria-pressed", String(button.dataset.state === entry[0]));
  }
  for (const entry of entries) {
    const button = document.createElement("button");
    button.dataset.state = entry[0]; button.textContent = entry[1];
    const hint = document.createElement("small"); hint.textContent = entry[2]; button.append(hint);
    button.addEventListener("click", () => select(entry)); panel.append(button);
  }
  pet.setSuspended(false);
  select(entries[0]);
})();

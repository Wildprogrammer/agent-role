'use strict';
(() => {
  const $ = id => document.getElementById(id), uid = () => crypto.randomUUID().replaceAll('-', '');
  let csrf = '', ws, turn = '', busy = false, recording = false, stopping = false, audioContext, gain, source;
  let stream, captureNode, inputNode, muteNode, workletReady = false, file = null, photoUrl = '', audioMeta;
  let captureTimer, followTimer, finishTimer, epoch = 0, capturePending = false;
  let binding = null, bindingPending = false;
  let muted = false, audioPreferenceSupported = false, audioEpoch = 0;
  let uploadXhr = null, preparingPhoto = false;
  let reconnectAllowed = false, reconnecting = false, reconnectAttempt = 0;
  let reconnectTimer, socketTimer, bindingVerified = false;
  const states = {idle:'准备好了', listening:'正在听你说 · 再点一次发送', transcribing:'正在识别语音', thinking:'正在思考', synthesizing:'正在准备声音', speaking:'正在回答', offline:'连接已断开', error:'需要处理'};
  function notice(text) { $('notice').textContent = text; }
  function state(name, label) { $('state').textContent = label || states[name] || name; $('face').dataset.state = name; }
  function controls() {
    const connected = ws?.readyState === 1;
    const ready = bindingReady();
    $('remove-photo').disabled = $('compress-photo').disabled = busy;
    $('mute').disabled = !connected || !audioPreferenceSupported;
    $('send').disabled = busy || !connected || !ready;
    $('bind').disabled = busy || bindingPending || !connected || !$('sessions').value; $('camera').disabled = busy || !ready;
    $('agents').disabled = $('sessions').disabled = $('refresh').disabled = busy || bindingPending;
    $('mic').disabled = !connected || !ready || stopping || (busy && !recording); $('mic').textContent = recording ? '结束并发送' : '点击说话';
    $('mic-indicator').hidden = !recording;
  }
  function bindingReady() {
    return bindingVerified && !bindingPending && binding?.bound === true && $('agents').value === JSON.stringify([binding.host,binding.profile]) && $('sessions').value === binding.stored_session_id;
  }
  function bindingHint() {
    controls();
    if (busy) return;
    if (ws?.readyState !== 1 || !bindingVerified) { state('offline', '正在恢复连接…'); return; }
    if (!bindingReady()) {
      $('settings').open = true;
      state('idle', bindingPending ? '正在连接所选会话…' : '请选好会话，再点“使用此会话”');
    } else state('idle');
  }
  function send(data) { if (!ws || ws.readyState !== 1) throw Error('服务未连接'); ws.send(JSON.stringify(data)); }
  async function api(path, options = {}) {
    const response = await fetch(path, {...options, headers:{'X-CSRF':csrf, ...options.headers}});
    if (!response.ok) { let reason = '请求失败：' + response.status; try { reason = (await response.json()).error || reason; } catch {} const error = Error(reason); error.status = response.status; throw error; }
    return response.json();
  }
  async function unlockAudio() {
    if (!audioContext) { audioContext = new AudioContext(); gain = audioContext.createGain(); gain.gain.value = Number($('volume').value); gain.connect(audioContext.destination); }
    await audioContext.resume();
  }
  function stopAudio() { if (source) { source.onended = null; try { source.stop(); } catch {} source.disconnect(); source = null; } audioMeta = null; }
  function showAudioPreference() {
    $('mute').textContent = muted ? '仅文字 · 点击恢复语音' : '静音 / 仅文字';
    $('mute').setAttribute('aria-pressed', String(muted));
  }
  function releaseMic() {
    clearTimeout(captureTimer); clearTimeout(finishTimer);
    if (captureNode) { captureNode.port.onmessage = null; captureNode.disconnect(); captureNode = null; }
    inputNode?.disconnect(); muteNode?.disconnect(); inputNode = muteNode = null;
    stream?.getTracks().forEach(track => track.stop()); stream = null;
    recording = capturePending = false; controls();
  }
  function removePhoto() { if (photoUrl) URL.revokeObjectURL(photoUrl); photoUrl = ''; file = null; $('file').value = ''; $('photo-preview').hidden = true; $('preview').removeAttribute('src'); }
  function localStop() { epoch++; turn = ''; uploadXhr?.abort(); preparingPhoto = false; $('upload-panel').hidden = true; clearTimeout(followTimer); stopAudio(); releaseMic(); }
  function stop() {
    const beforeSubmit = preparingPhoto || uploadXhr !== null;
    localStop(); busy = stopping = true; state('idle', '正在停止…'); controls();
    if (beforeSubmit) { busy = stopping = false; bindingHint(); notice('已取消上传，照片和问题已保留，未提交给智能体。'); return; }
    try { send({type:'stop'}); } catch (e) { busy = stopping = false; controls(); notice(e.message); }
  }
  const photoSize = bytes => (bytes / (1024 * 1024)).toFixed(2) + ' MiB';
  async function compressPhoto(original) {
    if (!$('compress-photo').checked || original.size <= 1024 * 1024) return original;
    let bitmap, canvas;
    try {
      bitmap = await createImageBitmap(original);
      const scale = Math.min(1, 1600 / Math.max(bitmap.width, bitmap.height));
      canvas = document.createElement('canvas');
      canvas.width = Math.max(1, Math.round(bitmap.width * scale)); canvas.height = Math.max(1, Math.round(bitmap.height * scale));
      const context = canvas.getContext('2d');
      context.fillStyle = '#fff'; context.fillRect(0, 0, canvas.width, canvas.height);
      context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', 0.82));
      canvas.width = canvas.height = 0;
      if (!blob) throw Error('encode failed');
      if (blob.size >= original.size) return original;
      return new File([blob], original.name.replace(/\.[^.]*$/, '') + '.jpg', {type:'image/jpeg'});
    } catch { throw Error('图片压缩失败，照片已保留。可取消勾选压缩后重试。'); }
    finally { bitmap?.close(); if (canvas) canvas.width = canvas.height = 0; }
  }
  function uploadPhoto(payload, currentEpoch) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest(); uploadXhr = xhr;
      let loaded = 0, confirmed = false;
      const started = Date.now();
      const update = () => {
        if (currentEpoch !== epoch) return;
        const percent = Math.min(100, Math.round(loaded * 100 / payload.size));
        $('upload-progress').value = percent;
        $('upload-status').textContent = `${percent}% · ${photoSize(loaded)} / ${photoSize(payload.size)} · ${Math.floor((Date.now()-started)/1000)}秒${confirmed ? ' · 等待服务器确认' : ''}`;
      };
      const timer = setInterval(update, 1000);
      const done = (error, value) => {
        clearInterval(timer);
        if (uploadXhr === xhr) uploadXhr = null;
        error ? reject(Error(error)) : resolve(value);
      };
      xhr.upload.onprogress = event => { loaded = Math.min(event.loaded, payload.size); confirmed = loaded >= payload.size; update(); };
      xhr.onload = () => {
        let value; try { value = JSON.parse(xhr.responseText); } catch {}
        if (xhr.status === 200 && typeof value?.attachment_id === 'string') { done(null, value); return; }
        done((value?.error || `上传失败（HTTP ${xhr.status}）`) + ' 照片和问题已保留，未提交给智能体；请手动重试。');
      };
      xhr.onerror = () => done('网络连接中断，照片和问题已保留，未提交给智能体；请恢复连接后手动重试。');
      xhr.ontimeout = () => done('上传超时，照片和问题已保留；可开启压缩后手动重试。');
      xhr.onabort = () => done('上传已取消，照片和问题已保留。');
      update();
      try {
        xhr.open('POST', '/api/photo'); xhr.timeout = 630000;
        xhr.setRequestHeader('X-CSRF', csrf);
        xhr.setRequestHeader('Content-Type', 'application/octet-stream');
        xhr.setRequestHeader('X-File-Name', encodeURIComponent(payload.name));
        xhr.setRequestHeader('X-File-Type', payload.type);
        xhr.send(payload);
      } catch { done('无法开始上传，照片和问题已保留。'); }
    });
  }
  async function attachPhoto(currentEpoch) {
    if (!file) return [];
    preparingPhoto = true; $('upload-panel').hidden = false; $('upload-progress').value = 0;
    state('idle', '正在准备照片…'); $('upload-status').textContent = '正在准备图片，原照片保持不变';
    const original = file, payload = await compressPhoto(original);
    if (currentEpoch !== epoch) return [];
    if (binding.host === 'hermes' && payload.size > 25 * 1024 * 1024) throw Error('Hermes图片最多25 MiB，请开启压缩或选择较小图片。');
    state('idle', '正在上传照片…');
    notice(payload === original ? `上传原图 ${photoSize(payload.size)}` : `图片已压缩：${photoSize(original.size)} → ${photoSize(payload.size)}，原照片不变。`);
    const data = await uploadPhoto(payload, currentEpoch);
    if (currentEpoch === epoch) { preparingPhoto = false; $('upload-panel').hidden = true; }
    return [data.attachment_id];
  }
  async function submit(capture) {
    if (busy) return;
    if (!bindingReady()) { bindingHint(); notice('尚未连接所选会话，请点击“使用此会话”。问题已保留，没有发送。'); return; }
    const text = $('text').value.trim();
    if (!capture && !text) { notice('请先输入问题。'); return; }
    const currentEpoch = ++epoch; turn = ''; clearTimeout(followTimer); stopAudio(); busy = true; preparingPhoto = !!file; controls();
    try {
      if (!muted || capture) await unlockAudio();
      if (currentEpoch !== epoch) return;
      if (capture) {
        if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) throw Error('录音需要可信 HTTPS，请先安装并信任入口证书。');
        const acquired = await navigator.mediaDevices.getUserMedia({audio:{channelCount:1, echoCancellation:true, noiseSuppression:true, autoGainControl:true}});
        if (currentEpoch !== epoch) { acquired.getTracks().forEach(t => t.stop()); return; }
        stream = acquired;
        if (!workletReady) { await audioContext.audioWorklet.addModule('/static/mobile-recorder.js'); workletReady = true; }
      }
      if (currentEpoch !== epoch) return;
      const attachments = await attachPhoto(currentEpoch);
      if (currentEpoch !== epoch) return;
      turn = uid(); capturePending = capture;
      $('question').textContent = capture ? '' : text; $('answer').textContent = ''; notice('');
      send({type:capture ? 'capture' : 'submit', turn, text, attachments});
      removePhoto(); $('text').value = '';
      state(capture ? 'listening' : 'thinking');
    } catch (error) { if (currentEpoch !== epoch) return; preparingPhoto = false; $('upload-panel').hidden = true; releaseMic(); busy = false; controls(); state('error'); notice(error.message); }
  }
  function startMic() {
    if (!capturePending || !stream) return;
    capturePending = false; recording = true;
    const captureTurn = turn;
    captureNode = new AudioWorkletNode(audioContext, 'lingxi-recorder');
    inputNode = audioContext.createMediaStreamSource(stream); muteNode = audioContext.createGain(); muteNode.gain.value = 0;
    inputNode.connect(captureNode); captureNode.connect(muteNode); muteNode.connect(audioContext.destination);
    captureNode.port.onmessage = ({data}) => {
      if (captureTurn !== turn) return;
      if (data === 'finished') { releaseMic(); send({type:'capture_end', turn:captureTurn}); state('transcribing'); }
      else if (ws?.readyState === 1) {
        if (ws.bufferedAmount > 1024 * 1024) { stop(); notice('网络发送不畅，录音已停止。'); return; }
        ws.send(data);
      }
    };
    controls(); captureTimer = setTimeout(finishCapture, 60000);
  }
  function finishCapture() {
    if (!captureNode) return;
    $('mic').disabled = true; captureNode.port.postMessage('finish');
    finishTimer = setTimeout(() => { stop(); notice('录音结束超时，未自动重发。'); }, 3000);
  }
  async function play(bytes) {
    const audioTurn = new TextDecoder().decode(bytes.slice(0,32));
    if (audioTurn !== turn || !audioMeta || document.hidden) return;
    if (muted) { send({type:'played',turn:audioTurn,sequence:audioMeta.sequence}); audioMeta = null; return; }
    const sequence = audioMeta.sequence, currentEpoch = epoch, currentAudioEpoch = audioEpoch; audioMeta = null;
    try {
      await unlockAudio();
      const decoded = await audioContext.decodeAudioData(bytes.slice(32));
      if (currentEpoch !== epoch || currentAudioEpoch !== audioEpoch || audioTurn !== turn || document.hidden || muted) return;
      source = audioContext.createBufferSource(); source.buffer = decoded; source.connect(gain);
      source.onended = () => { source = null; if (audioTurn === turn) send({type:'played', turn:audioTurn, sequence}); };
      source.start(); state('speaking');
    } catch { stop(); notice('手机音频未能播放，文字已保留。请点击后再试。'); }
  }
  function options(select, rows, getValue, getLabel) {
    select.replaceChildren(...rows.map(row => { const o = document.createElement('option'); o.value = getValue(row); o.textContent = getLabel(row); return o; }));
  }
  function sessions() { $('sessions').replaceChildren(); bindingHint(); const value = $('agents').value; if (value) { const [host, agent] = JSON.parse(value); send({type:'sessions', host, agent}); } }
  function scheduleReconnect() {
    if (!reconnectAllowed || document.hidden || reconnectTimer || reconnecting) return;
    if (reconnectAttempt >= 6) { notice('暂时无法连接，请检查网络后点“重新连接”。照片和问题仍保留，不会重发旧消息。'); return; }
    reconnectTimer = setTimeout(() => { reconnectTimer = null; void recoverConnection(); }, Math.min(1000 * 2 ** reconnectAttempt, 10000));
  }
  function disconnected(socket) {
    if (socket !== ws) return;
    clearTimeout(socketTimer); socketTimer = null;
    localStop(); busy = stopping = bindingPending = false; bindingVerified = false;
    controls(); state('offline', document.hidden ? '返回页面后恢复连接' : '连接中断，正在恢复…');
    $('reconnect').hidden = false;
    notice('照片和问题已保留；连接恢复后请手动发送，不会重发旧消息。');
    scheduleReconnect();
  }
  function watchSocket(socket) {
    clearTimeout(socketTimer);
    socketTimer = setTimeout(() => {
      socketTimer = null;
      if (socket !== ws || !reconnectAllowed) return;
      ws = undefined; socket.close();
      localStop(); busy = stopping = bindingPending = false; bindingVerified = false;
      controls(); state('offline'); $('reconnect').hidden = false; scheduleReconnect();
    }, 10000);
  }
  async function recoverConnection() {
    if (!reconnectAllowed || document.hidden || reconnecting) return;
    if (ws?.readyState === 0) return;
    if (ws?.readyState === 1) {
      if (socketTimer) return;
      bindingVerified = false; controls(); watchSocket(ws);
      try { send({type:'binding'}); } catch { ws.close(); }
      return;
    }
    if (reconnectAttempt >= 6) return;
    reconnecting = true; reconnectAttempt++;
    clearTimeout(reconnectTimer); reconnectTimer = null;
    state('offline', '正在恢复连接…');
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const result = await api('/api/session', {signal:controller.signal});
      if (!reconnectAllowed || document.hidden) return;
      csrf = result.csrf; connect();
    } catch (error) {
      if (!reconnectAllowed) return;
      if (error.status === 401) {
        reconnectAllowed = false; $('login').hidden = false;
        $('login-error').textContent = '登录已过期，请重新连接。照片和问题仍保留。';
      } else notice('正在等待网络恢复，照片和问题仍保留。');
    } finally {
      clearTimeout(timeout); reconnecting = false;
      if (!ws || ws.readyState >= 2) scheduleReconnect();
    }
  }
  function connect() {
    if (!reconnectAllowed || (ws && ws.readyState < 2)) return;
    const socket = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/api/socket`);
    ws = socket; socket.binaryType = 'arraybuffer'; bindingVerified = false; watchSocket(socket);
    socket.onopen = () => { if (socket !== ws) return; busy = stopping = bindingPending = false; bindingHint(); send({type:'binding'}); send({type:'agents'}); };
    socket.onclose = () => disconnected(socket);
    socket.onmessage = ({data}) => {
      if (socket !== ws) return;
      if (data instanceof ArrayBuffer) { void play(data); return; }
      const msg = JSON.parse(data);
      if (msg.turn && msg.turn !== turn) return;
      switch (msg.type) {
        case 'ready': audioPreferenceSupported = msg.audio_preference_supported === true; if (audioPreferenceSupported) { muted = msg.audio_enabled === false; showAudioPreference(); } if (msg.busy) { busy = true; notice('原回合尚未结束，请等待或点停止。'); } controls(); break;
        case 'audio_preference': if (muted !== !msg.enabled) audioEpoch++; muted = !msg.enabled; if (muted) stopAudio(); showAudioPreference(); notice(muted ? '仅文字模式：保留字幕，不生成语音；智能体继续回答。' : '语音已开启，已跳过的声音不会补播。'); break;
        case 'capture_ready': startMic(); break;
        case 'state': state(msg.state); break;
        case 'question': $('question').textContent = msg.text; break;
        case 'answer': $('answer').textContent = msg.text; break;
        case 'audio': audioMeta = msg; break;
        case 'notice': notice(msg.message); break;
        case 'error': bindingPending = false; if (busy && !stopping) stop(); else { localStop(); busy = false; bindingHint(); } notice(msg.message); break;
        case 'done': state('idle', '还想问点什么？'); followTimer = setTimeout(() => state('idle'), 12000); break;
        case 'idle': busy = false; controls(); break;
        case 'stopping': localStop(); busy = stopping = true; controls(); state('idle', '正在停止…'); break;
        case 'stopped': busy = stopping = false; controls(); state('idle'); if ($('notice').textContent.startsWith('播放已停止，仍在等待原回合结束')) notice('已停止，可以继续提问。'); break;
        case 'binding': {
          const b = msg.binding;
          clearTimeout(socketTimer); socketTimer = null; reconnectAttempt = 0;
          const recovered = !bindingVerified; bindingVerified = true; $('reconnect').hidden = true;
          const requested = bindingPending; bindingPending = false; binding = b;
          $('binding-label').textContent = b.bound ? `已连接会话：${b.host} / ${b.profile} · ${b.title || b.stored_session_id}` : '尚未连接会话，消息不会发送';
          bindingHint();
          if (recovered) notice('连接已恢复。照片和问题已保留，请确认会话后手动发送。');
          if (requested && bindingReady()) { notice('会话已连接，可以发送问题了。'); $('settings').open = false; }
          break;
        }
        case 'agents': options($('agents'),msg.agents,r => JSON.stringify([r.host,r.id]),r => `${r.host} / ${r.name}${r.available === false ? '（不可用）' : ''}`); if (binding?.bound) $('agents').value = JSON.stringify([binding.host,binding.profile]); sessions(); break;
        case 'sessions': {
          if ($('agents').value !== JSON.stringify([msg.host,msg.agent])) return;
          options($('sessions'),msg.sessions,r => r.id,r => r.title || r.id);
          if (binding?.bound && binding.host === msg.host && binding.profile === msg.agent && msg.sessions.some(r => r.id === binding.stored_session_id)) $('sessions').value = binding.stored_session_id;
          bindingHint();
          if (!msg.sessions.length) notice('此智能体没有已有会话，请先在电脑端创建。');
          break;
        }
      }
    };
  }
  $('login').onsubmit = async event => {
    event.preventDefault(); $('login-error').textContent = '';
    try {
      await unlockAudio();
      let terminal = localStorage.getItem('lingxi-mobile-terminal');
      if (!/^[a-f0-9]{32}$/.test(terminal || '')) { terminal = uid(); localStorage.setItem('lingxi-mobile-terminal',terminal); }
      const result = await api('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({terminal,password:$('password').value})});
      csrf = result.csrf; reconnectAllowed = true; reconnectAttempt = 0; $('password').value = ''; $('login').hidden = true; $('app').hidden = false; connect();
    } catch (error) { $('login-error').textContent = error.message; }
  };
  $('send').onclick = () => submit(false); $('mic').onclick = () => recording ? finishCapture() : submit(true); $('stop').onclick = stop;
  $('camera').onclick = () => $('file').click(); $('remove-photo').onclick = removePhoto;
  $('cancel-upload').onclick = stop;
  $('file').onchange = () => {
    if (busy) return;
    const selected = $('file').files[0]; if (!selected) return;
    if (selected.size > 50 * 1024 * 1024) { notice('单张照片最多50 MiB；Hermes最多25 MiB。'); return; }
    removePhoto(); file = selected; photoUrl = URL.createObjectURL(file); $('preview').src = photoUrl; $('photo-preview').hidden = false;
    void recoverConnection();
  };
  $('volume').oninput = () => { if (gain) gain.gain.value = Number($('volume').value); };
  $('mute').onclick = () => {
    if (!audioPreferenceSupported || ws?.readyState !== 1) return;
    muted = !muted;
    audioEpoch++;
    if (muted) stopAudio();
    showAudioPreference();
    send({type:'audio_preference',enabled:!muted});
  };
  $('agents').onchange = sessions; $('refresh').onclick = () => send({type:'agents'});
  $('sessions').onchange = bindingHint;
  $('bind').onclick = () => { if (!$('sessions').value || busy || bindingPending) return; const [host,agent] = JSON.parse($('agents').value); removePhoto(); try { send({type:'bind',host,agent,session_id:$('sessions').value}); bindingPending = true; bindingHint(); } catch (error) { bindingPending = false; bindingHint(); notice(error.message); } };
  $('fullscreen').onclick = () => document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen().catch(() => notice('浏览器不支持全屏。'));
  $('reconnect').onclick = () => { reconnectAttempt = 0; void recoverConnection(); };
  $('logout').onclick = async () => { reconnectAllowed = false; clearTimeout(reconnectTimer); clearTimeout(socketTimer); stop(); await api('/api/logout',{method:'POST'}); csrf = ''; location.reload(); };
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { clearTimeout(reconnectTimer); reconnectTimer = null; clearTimeout(socketTimer); socketTimer = null; if (busy || recording) stop(); else localStop(); }
    else { reconnectAttempt = 0; void recoverConnection(); }
  });
  window.addEventListener('online', () => { reconnectAttempt = 0; void recoverConnection(); });
  window.addEventListener('pagehide', () => { reconnectAllowed = false; clearTimeout(reconnectTimer); reconnectTimer = null; clearTimeout(socketTimer); socketTimer = null; localStop(); ws?.close(); });
  window.addEventListener('pageshow', () => { if (csrf) { reconnectAllowed = true; void recoverConnection(); } });
  api('/api/session').then(result => { csrf = result.csrf; reconnectAllowed = true; $('login').hidden = true; $('app').hidden = false; connect(); }).catch(() => {});
})();

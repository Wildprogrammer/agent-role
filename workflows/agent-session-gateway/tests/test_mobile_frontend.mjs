import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {randomUUID} from 'node:crypto';

// Exercise the shipped browser controller without any real Agent writes.
const root = new URL('../../../src/agent_workflow_hub/xiaozhi_hermes_bridge/static/', import.meta.url);
const html = fs.readFileSync(new URL('index-mobile.html', root), 'utf8');
class Element {
  constructor() { this.value = ''; this.textContent = ''; this.dataset = {}; this.disabled = false; }
  replaceChildren(...rows) { this.rows = rows; this.value = rows[0]?.value || ''; }
  removeAttribute() {}
  setAttribute(name, value) { this[name] = value; }
}
const elements = Object.fromEntries([...html.matchAll(/id="([^"]+)"/g)].map(m => [m[1], new Element()]));
elements.volume.value = '0.8';
const sockets = [], sent = [];
const uploads = [], listeners = {}, windowListeners = {}, canvases = [], timers = new Map();
let timerId = 0, sessionStatus = 200, sessionRequests = 0;
const setTimer = (callback, milliseconds) => { const id = ++timerId; timers.set(id, {callback, milliseconds}); return id; };
const clearTimer = id => timers.delete(id);
const fireTimer = id => { const timer = timers.get(id); timers.delete(id); timer.callback(); };
let bitmapFactory;
class Upload {
  upload = {}; headers = {};
  constructor() { uploads.push(this); }
  open(method, path) { assert.equal(method, 'POST'); assert.equal(path, '/api/photo'); }
  setRequestHeader(name, value) { this.headers[name] = value; }
  send(payload) { this.payload = payload; }
  abort() { this.aborted = true; this.onabort(); }
  complete(status, value) { this.status = status; this.responseText = JSON.stringify(value); this.onload(); }
}
function createElement(tag) {
  if (tag !== 'canvas') return new Element();
  const canvas = {getContext: () => ({fillRect() {}, drawImage() {}}),
    toBlob(callback, type, quality) {
      assert.ok(this.width <= 1600 && this.height <= 1600);
      assert.equal(type, 'image/jpeg'); assert.equal(quality, 0.82);
      callback(new Blob(['compressed'], {type}));
    }};
  canvases.push(canvas); return canvas;
}
class Socket {
  readyState = 1;
  constructor() { sockets.push(this); }
  send(text) { sent.push(JSON.parse(text)); }
  close() { this.readyState = 3; this.onclose?.(); }
}
const context = vm.createContext({
  document: {getElementById: id => elements[id], createElement, addEventListener(name, callback) { listeners[name] = callback; }},
  window: {addEventListener(name, callback) { windowListeners[name] = callback; }}, location: {protocol: 'https:', host: 'phone.test'},
  fetch: async () => { sessionRequests++; return {ok:sessionStatus === 200, status:sessionStatus, json:async () => ({csrf:'test-csrf'})}; }, WebSocket:Socket,
  setTimeout:setTimer, clearTimeout:clearTimer, setInterval, clearInterval, ArrayBuffer, TextDecoder, AbortController,
  XMLHttpRequest:Upload, crypto:{randomUUID}, File, URL:{createObjectURL: () => 'blob:test', revokeObjectURL() {}},
  createImageBitmap: file => bitmapFactory(file),
});
vm.runInContext(fs.readFileSync(new URL('mobile.js', root), 'utf8'), context);
await new Promise(resolve => setImmediate(resolve));
const socket = sockets[0];
socket.onopen();
const receive = value => socket.onmessage({data:JSON.stringify(value)});
const agent = JSON.stringify(['deepseek-harness','test-agent']);
receive({type:'binding',binding:{bound:false}});
receive({type:'agents',agents:[{host:'deepseek-harness',id:'test-agent',name:'Test'}]});
receive({type:'sessions',host:'deepseek-harness',agent:'test-agent',sessions:[{id:'one',title:'One'},{id:'two',title:'Two'}]});
assert.equal(elements.settings.open, true);
assert.equal(elements.send.disabled, true);
assert.equal(elements.mic.disabled, true);
elements.text.value = 'Keep this question';
await elements.send.onclick();
assert.equal(elements.text.value, 'Keep this question');
assert.equal(sent.some(m => m.type === 'submit'), false);
elements.bind.onclick();
assert.equal(sent.at(-1).type, 'bind');
assert.equal(elements.send.disabled, true);
assert.equal(elements.agents.disabled, true);
elements.bind.onclick();
assert.equal(sent.filter(m => m.type === 'bind').length, 1);
const binding = {bound:true,host:'deepseek-harness',profile:'test-agent',stored_session_id:'one',title:'One'};
receive({type:'binding',binding});
assert.equal(elements.send.disabled, false);
assert.equal(elements.mic.disabled, false);
assert.match(elements['binding-label'].textContent, /已连接会话/);
assert.equal(elements.text.value, 'Keep this question');
elements.sessions.value = 'two';
elements.sessions.onchange();
assert.equal(elements.send.disabled, true, 'A pending selection must not send to the old binding');
assert.equal(elements.settings.open, true);
elements.bind.onclick();
receive({type:'error',message:'Binding failed'});
assert.equal(elements.send.disabled, true);
assert.equal(elements.bind.disabled, false);
elements.sessions.value = 'one';
elements.sessions.onchange();
assert.equal(elements.send.disabled, false, 'Returning to the confirmed binding is safe');
assert.equal(elements.agents.value, agent);
assert.equal(elements.mute.disabled, true, 'Do not send an unsupported command to the old server');
receive({type:'ready',audio_preference_supported:true,audio_enabled:true});
assert.equal(elements.mute.disabled, false);
const beforeMute = sent.length;
elements.mute.onclick();
assert.deepEqual(sent.at(-1), {type:'audio_preference',enabled:false});
receive({type:'audio_preference',enabled:false});
assert.equal(elements.mute['aria-pressed'], 'true');
assert.equal(sent.slice(beforeMute).some(m => m.type === 'stop'), false);
elements.mute.onclick();
assert.deepEqual(sent.at(-1), {type:'audio_preference',enabled:true});
console.log('PASS: unbound input retained; binding ACK required; pending/failed switch cannot send to old session.');
console.log('PASS: mute negotiates server support and changes TTS preference without cancelling the Agent.');

// Uploads are staged first: no Agent message before a confirmed upload response.
receive({type:'audio_preference',enabled:false});
const tick = () => new Promise(resolve => setImmediate(resolve));
const submissions = () => sent.filter(m => m.type === 'submit').length;
const stops = () => sent.filter(m => m.type === 'stop').length;
const choose = (size = 2048) => {
  elements.file.files = [new File([new Uint8Array(size)], 'camera.png', {type:'image/png'})];
  elements.file.onchange(); elements.text.value = 'Describe my photo';
  receive({type:'binding',binding});
};
elements['compress-photo'].checked = false;
choose();
let pending = elements.send.onclick(); await tick();
let upload = uploads.at(-1);
assert.equal(upload.headers['X-CSRF'], 'test-csrf');
upload.upload.onprogress({loaded:1024});
assert.equal(elements['upload-progress'].value, 50);
assert.equal(submissions(), 0);
upload.complete(408, {error:'上传超时'}); await pending;
assert.equal(elements.text.value, 'Describe my photo');
assert.equal(elements['photo-preview'].hidden, false);
assert.equal(elements.send.disabled, false);
assert.equal(submissions(), 0);

pending = elements.send.onclick(); await tick(); upload = uploads.at(-1);
const beforeStop = stops();
elements['cancel-upload'].onclick(); await pending;
assert.equal(upload.aborted, true);
assert.equal(stops(), beforeStop, 'Cancelling an upload must not stop the Agent');
assert.equal(elements.text.value, 'Describe my photo');
upload.complete(200, {attachment_id:'late-cancelled'}); await tick();
assert.equal(submissions(), 0, 'Late upload acknowledgement must not dispatch');

// Cancelling while decoding a large photo must also reject its late result.
elements['compress-photo'].checked = true;
choose(2 * 1024 * 1024);
let finishBitmap, closed = 0;
bitmapFactory = () => new Promise(resolve => { finishBitmap = resolve; });
const beforeDecode = uploads.length;
pending = elements.send.onclick(); await tick();
elements['cancel-upload'].onclick();
finishBitmap({width:4000,height:3000,close() { closed++; }}); await pending;
assert.equal(uploads.length, beforeDecode);
assert.equal(closed, 1);
assert.equal(submissions(), 0);
assert.equal(stops(), beforeStop);
assert.equal(elements['upload-panel'].hidden, true);

bitmapFactory = async () => ({width:4000,height:3000,close() { closed++; }});
pending = elements.send.onclick(); await tick(); upload = uploads.at(-1);
assert.equal(upload.payload.type, 'image/jpeg');
assert.equal(upload.payload.name, 'camera.jpg');
assert.ok(upload.payload.size < 1024);
assert.equal(canvases.at(-1).width, 0, 'Release the canvas backing store after encoding');
upload.complete(200, {attachment_id:'compressed-ok'}); await pending;
assert.equal(submissions(), 1);
assert.deepEqual(sent.at(-1).attachments, ['compressed-ok']);
assert.equal(elements.text.value, '');
assert.equal(elements['photo-preview'].hidden, true);
receive({type:'idle'});

// Backgrounding and losing the socket abort uploads without replaying them.
for (const action of ['background', 'disconnect']) {
  choose(); pending = elements.send.onclick(); await tick(); upload = uploads.at(-1);
  if (action === 'background') { context.document.hidden = true; listeners.visibilitychange(); }
  else { socket.readyState = 3; socket.onclose(); }
  await pending;
  upload.complete(200, {attachment_id:'late-offline'}); await tick();
  assert.equal(upload.aborted, true);
  assert.equal(submissions(), 1);
  assert.equal(elements.text.value, 'Describe my photo');
  context.document.hidden = false;
}
console.log('PASS: upload progress, failure retention, cancellation, compression, late completion and disconnect guards.');

// Camera return: recover only the transport, retain draft/photo, never replay a turn.
const requestsBeforeRecovery = sessionRequests;
listeners.visibilitychange(); windowListeners.online(); listeners.visibilitychange();
await tick();
assert.equal(sessionRequests, requestsBeforeRecovery + 1, 'Recovery must be single-flight');
assert.equal(sockets.length, 2);
const recoveredSocket = sockets.at(-1);
recoveredSocket.onopen();
assert.equal(elements.send.disabled, true, 'Do not send before fresh binding verification');
const deliver = value => recoveredSocket.onmessage({data:JSON.stringify(value)});
deliver({type:'ready',audio_preference_supported:true,audio_enabled:false});
deliver({type:'binding',binding});
deliver({type:'agents',agents:[{host:'deepseek-harness',id:'test-agent',name:'Test'}]});
deliver({type:'sessions',host:'deepseek-harness',agent:'test-agent',sessions:[{id:'one',title:'One'}]});
assert.equal(elements.send.disabled, false);
assert.equal(elements['reconnect'].hidden, true);
assert.equal(elements.text.value, 'Describe my photo');
assert.equal(elements['photo-preview'].hidden, false);
assert.equal(submissions(), 1, 'Recovery must not replay the last submit');
socket.onclose(); receive({type:'answer',text:'stale reply'});
assert.notEqual(elements.answer.textContent, 'stale reply');
assert.equal(elements.send.disabled, false, 'Old close must not disable the new connection');

// A half-open connection is probed on camera return and retired after a deadline.
context.document.hidden = true; listeners.visibilitychange();
context.document.hidden = false; listeners.visibilitychange();
assert.equal(elements.send.disabled, true);
const probe = [...timers].find(([, timer]) => timer.milliseconds === 10000);
assert.ok(probe); fireTimer(probe[0]);
assert.equal(recoveredSocket.readyState, 3);
assert.equal(elements.text.value, 'Describe my photo');
const retry = [...timers].find(([, timer]) => timer.milliseconds === 1000);
assert.ok(retry); fireTimer(retry[0]); await tick();
assert.equal(sockets.length, 3);
assert.equal(submissions(), 1);

// Repeated failures have a bounded retry budget; hidden pages do not reconnect.
context.document.hidden = true;
sockets.at(-1).close(); listeners.visibilitychange();
const backgroundRequests = sessionRequests;
windowListeners.online(); await tick();
assert.equal(sessionRequests, backgroundRequests);
context.document.hidden = false; listeners.visibilitychange(); await tick();
for (let failures = 0; failures < 6; failures++) {
  sockets.at(-1).close();
  const next = [...timers].find(([, timer]) => timer.milliseconds <= 10000);
  if (!next) break;
  fireTimer(next[0]); await tick();
}
assert.equal(timers.size, 0, 'Retries must stop after the bounded attempt budget');
assert.match(elements.notice.textContent, /重新连接/);
assert.equal(submissions(), 1);

// Expired authentication stops retrying and keeps the selected photo for re-login.
sessionStatus = 401; sockets.at(-1).close();
elements.reconnect.onclick(); await tick();
assert.equal(elements.login.hidden, false);
assert.match(elements['login-error'].textContent, /登录已过期/);
assert.equal(elements['photo-preview'].hidden, false);
const expiredCount = sessionRequests;
windowListeners.online(); await tick();
assert.equal(sessionRequests, expiredCount);
windowListeners.pagehide();
assert.equal(timers.size, 0);
console.log('PASS: foreground recovery, single-flight reconnect, draft retention, stale sockets, half-open timeout and expired login.');

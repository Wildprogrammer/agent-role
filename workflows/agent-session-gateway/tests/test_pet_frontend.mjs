import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../../../src/agent_workflow_hub/xiaozhi_hermes_bridge/static/pet.js', import.meta.url), 'utf8');
async function setup(assetsInstalled = true) {
  const timers = new Map(); let nextTimer = 0;
  class Element {
    constructor() {
      this.dataset = {}; this.attributes = {}; this.handlers = {}; this.children = []; this.paused = true; this.plays = 0; this.readyState = 0;
      const classes = new Set();
      this.classList = {add:x=>classes.add(x), remove:x=>classes.delete(x), contains:x=>classes.has(x), toggle:(x,on)=>on?classes.add(x):classes.delete(x)};
    }
    setAttribute(k,v) {this.attributes[k]=v;}
    removeAttribute(k) {delete this.attributes[k]; if (k === "src") delete this.src;}
    addEventListener(k,v) {this.handlers[k]=v;}
    removeEventListener(k) {delete this.handlers[k];}
    append(...items) {for (const node of items) {node.parent = this; this.children.push(node);}}
    pause() {this.paused=true;}
    play() {this.paused=false;this.plays++;return Promise.resolve();}
    load() {}
    remove() {this.parent.children = this.parent.children.filter(v => v !== this);}
  }
  const root = new Element();
  const motion = new Element(); motion.matches = false;
  const document = new Element(); document.hidden = false; document.createElement = ()=>new Element();
  const window = {matchMedia:()=>motion, setTimeout:(fn,ms)=>{const id=++nextTimer;timers.set(id,{fn,ms});return id;},clearTimeout:id=>timers.delete(id)};
  window.fetch = async () => ({ok: assetsInstalled, json: async () => ({version: '0.4.7'})});
  vm.runInNewContext(source,{window,document,Object});
  const pet = window.LingxiPet.mount(root);
  await new Promise(resolve => setImmediate(resolve));
  function fire(ms) {const entry=[...timers].find(([,v])=>v.ms===ms);assert.ok(entry,`timer ${ms}`);timers.delete(entry[0]);entry[1].fn();}
  function loaded() {const v=root.children.at(-1); v.readyState=2; v.onplaying(); return v;}
  return {pet,root,motion,document,timers,fire,loaded};
}

{
  const t=await setup();
  t.pet.setState('neutral'); assert.equal(t.root.children.some(v=>v.src),false,'login must not fetch or play media');
  t.pet.setSuspended(false); const idle=t.loaded(); assert.equal(idle.paused,false);
  t.root.handlers.click(); assert.equal(t.root.dataset.clip,'wave'); const pending=t.root.children.find(v=>v.src?.includes('-wave.webm?'));
  const stale=pending.onplaying;
  t.pet.setState('speaking'); assert.equal(idle.paused,true,'old action stops before new media arrives');
  assert.equal(t.root.classList.contains('pet-ready'),false,'current-state fallback must replace old frame immediately');
  stale(); assert.equal(pending.paused,true,'late load cannot resume old interaction');
  assert.equal(t.root.classList.contains('pet-ready'),false,'stale playback must not reveal another clip');
  const talk=t.loaded(); assert.equal(talk.paused,false);
  t.root.handlers.click(); assert.equal(t.root.dataset.clip,'talk','click must not replace live speaking');
  t.pet.setState('thinking'); assert.equal(talk.paused,true); t.loaded();
  t.pet.setState('error'); t.loaded(); assert.equal(t.root.dataset.state,'error');
  assert.equal([...t.timers.values()].some(v=>v.ms===24000),false,'errors cannot turn into random idle/success');
  t.pet.setState('privacy'); t.loaded(); assert.equal(t.root.dataset.clip,'sleep');
  t.pet.setState('offline'); t.loaded(); assert.equal(t.root.dataset.clip,'wait');
  assert.equal(t.root.dataset.state,'offline','animation does not invent online state');
  console.log('PASS state priority, immediate pause, stale load and no invented success');
}
{
  const t=await setup(); t.pet.setSuspended(false);t.pet.setState('neutral');t.loaded();t.fire(24000);t.loaded();
  assert.equal(t.root.dataset.clip,'look'); t.fire(12000);t.loaded();assert.equal(t.root.dataset.clip,'idle');
  t.document.hidden=true;t.document.handlers.visibilitychange();assert.ok(t.root.children.every(v=>v.paused));assert.equal(t.timers.size,0);
  t.document.hidden=false;t.document.handlers.visibilitychange();t.loaded();
  t.motion.matches=true;t.motion.handlers.change();assert.equal(t.root.children.length,0);assert.equal(t.timers.size,0);
  t.root.handlers.click();assert.equal(t.root.dataset.clip,'idle');
  t.pet.setSuspended(true);assert.ok(t.root.children.every(v=>v.paused));
  console.log('PASS bounded idle interactions, hidden/login pause, reduced motion');
}
{
  const t=await setup();t.pet.setSuspended(false);t.fire(7000);assert.equal(t.root.dataset.media,'fallback');
  assert.equal(t.root.classList.contains('pet-ready'),false);assert.ok(t.root.children.every(v=>v.paused));
  t.pet.setState('thinking');const v=t.loaded();
  v.onwaiting(); t.fire(450); assert.equal(t.root.classList.contains('pet-ready'),false);
  v.onplaying(); assert.equal(t.root.classList.contains('pet-ready'),true);
  v.onerror();assert.equal(t.root.dataset.media,'fallback');
  t.pet.dispose();assert.equal(t.timers.size,0);assert.equal(Object.keys(t.document.handlers).length,0);
  console.log('PASS load timeout / decoder failure fallback and disposal');
}

{
  const t = await setup(false);
  t.pet.setSuspended(false);
  t.pet.setState('neutral');
  t.root.handlers.click();
  t.pet.setState('speaking');
  assert.equal(t.root.dataset.state, 'speaking');
  assert.equal(t.root.dataset.media, 'fallback');
  assert.equal(t.root.children.length, 0, 'missing optional pack must not request any video');
  assert.equal(t.timers.size, 0);
}

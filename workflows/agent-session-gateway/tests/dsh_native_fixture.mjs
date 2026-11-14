// Opt-in protocol test: genuine installed DSH transport and argument schemas,
// in-memory credentials and synthetic sessions. Never load a user profile.
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {createServer} from 'node:http';
import {readFileSync} from 'node:fs';
import assert from 'node:assert/strict';

const require = createRequire(process.argv[2]);
const load = name => import(pathToFileURL(require.resolve('@deepseek-ai/' + name)));
for (const name of ['dsh', 'dsh-client-connection', 'dsh-api-session-controller', 'dsh-agent-preset-registry']) {
  const path = require.resolve('@deepseek-ai/' + name + '/package.json');
  assert.equal(JSON.parse(readFileSync(path, 'utf8')).version, '0.2.2-rc.1');
}
const {Context} = await load('cordis');
const Connection = await load('dsh-client-connection');
const schemas = [];
for (const name of ['dsh-api-session-controller', 'dsh-agent-preset-registry']) {
  const module = await import(new URL('./typert.host.js', pathToFileURL(require.resolve('@deepseek-ai/' + name))));
  schemas.push(...module.TYPERT.invocations);
}
const ctx = new Context();
ctx.provide('credentials', {modifyRecord: async (_key, change) => change(undefined)});
await Connection.apply(ctx, {});
let promptId = '', promptCount = 0;
ctx.connection.rpc.intercept('/api', () => true, async (method, payload) => {
  const descriptor = schemas.find(row => row.namespace + '/' + row.method === method);
  assert.ok(descriptor, 'Missing native descriptor: ' + method);
  assert.deepEqual(Object.keys(payload), ['args']);
  const args = payload.args;
  assert.deepEqual(Object.keys(args).sort(), descriptor.parameters.map(row => row.wire).sort());
  for (const param of descriptor.parameters) param.strictCodec.create().parse(args[param.wire]);
  let value;
  switch (method) {
    case 'agentPresets/list': value = {presets:[{id:'assistant-agent', name:'Synthetic agent', isDefault:true}]}; break;
    case 'session/list': value = {items:[{sessionId:'synthetic-session', running:false, blank:false,
      projections:{asOfSeq:3, values:{agentPreset:'assistant-agent', title:'Synthetic conversation'}}}]}; break;
    case 'session/prompt': promptId = args.request.requestId; promptCount++; value = {accepted:true}; break;
    case 'session/projections': value = {kind:'sequenced', asOfSeq:3, values:{}}; break;
    case 'session/events':
      assert.equal(promptCount, 1);
      value = {cursor:3, hasMore:false, records:[
        {type:'event', event:{type:'user/message', seq:1, time:1, data:{source:{kind:'user',rpcId:promptId}}}},
        {type:'event', event:{type:'assistant/message', seq:2, time:2, data:{message:{content:[{type:'text',text:'Synthetic native transport answer'}]}}}},
        {type:'event', event:{type:'turn/end', seq:3, time:3, data:{reason:{kind:'completed'}}}},
      ]}; break;
    case 'session/cancel': value = {accepted:true}; break;
    case 'session/create': value = {sessionId:args.request.sessionId}; break;
    default: throw Error('Unexpected fixture method');
  }
  return {ok:true, value};
});
const handler = ctx.connection.createSharedFetchHandler('/api');
const server = createServer(async (req, res) => {
  try {
    if (req.url.startsWith('/?')) { ctx.connection.authorizeIndex(req, res); return; }
    const admission = ctx.connection.admit(req);
    if ('rejection' in admission) { res.writeHead(admission.rejection); res.end(); return; }
    let body = '';
    for await (const chunk of req) { body += chunk; assert.ok(body.length < 65536); }
    const response = await handler.fetch(new Request('http://127.0.0.1' + req.url,
      {method:req.method, headers:req.headers, body:body || undefined}), admission.peer);
    res.writeHead(response.status, Object.fromEntries(response.headers));
    res.end(Buffer.from(await response.arrayBuffer()));
  } catch { res.writeHead(500); res.end('Native fixture rejected request'); }
});
server.listen(0, '127.0.0.1', () => {
  const origin = `http://127.0.0.1:${server.address().port}`;
  const url = new URL(ctx.connection.authenticatedUrl(origin));
  // Consumed by test parent only; these are ephemeral synthetic credentials.
  process.stdout.write(JSON.stringify({origin, token:url.searchParams.get('token')}) + '\n');
});

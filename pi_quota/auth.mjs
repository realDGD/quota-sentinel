import {readFile,stat} from 'node:fs/promises';
import {resolve,join} from 'node:path';
import {pathToFileURL} from 'node:url';
import {createRequire} from 'node:module';
import {createHash} from 'node:crypto';
import {MetadataError,withinDeadline,metadataRequest} from './network.mjs';

export async function loadSelectedSdk(options) {
  try {
    const root=resolve(options.sdk_path); const manifest=JSON.parse(await readFile(join(root,'package.json'),'utf8'));
    if (manifest.name !== '@earendil-works/pi-coding-agent') throw new Error();
    // Direct auth/model module import avoids loading session factories/resources.
    return await import(pathToFileURL(join(root,'dist/core/model-runtime.js')).href);
  } catch { throw new MetadataError('unsupported_pi_sdk'); }
}
export async function loadSelectedPlugin(options, relative) {
  try {
    const root=resolve(options.plugin_path);const manifest=JSON.parse(await readFile(join(root,'package.json'),'utf8'));
    if (manifest.name !== 'pi-antigravity') throw new Error();
    const require=createRequire(join(resolve(options.sdk_path),'package.json'));
    const {createJiti}=require('jiti');
    const loader=createJiti(import.meta.url,{moduleCache:false,interopDefault:false});
    return await loader.import(join(root,relative));
  } catch { throw new MetadataError('unsupported_pi_plugin'); }
}
function scope(provider, value) {
  return 'pi:'+provider+':'+createHash('sha256').update(value).digest('hex');
}
function tokenAccount(token) {
  try {
    const decoded=JSON.parse(Buffer.from(token.split('.')[1],'base64url').toString('utf8'));
    return decoded['https://api.openai.com/auth']?.chatgpt_account_id;
  } catch { return undefined; }
}
async function refreshAntigravity(runtime,stored,plugin,owner,deadline) {
  if (stored.type!=='oauth' || !Number.isFinite(stored.expires)) throw new MetadataError('auth_unavailable');
  if (stored.expires>deadline) return stored;
  if (owner!=='pi') throw new MetadataError('auth_expired');
  if (typeof runtime.credentials.modify!=='function' || plugin.TOKEN_URL!=='https://oauth2.googleapis.com/token'
      || typeof plugin.CLIENT_ID!=='string' || typeof plugin.CLIENT_SECRET!=='string') throw new MetadataError('unsupported_pi_plugin');
  const signal=AbortSignal.timeout(Math.max(1,deadline-Date.now()));
  return runtime.credentials.modify('antigravity',async current=>{
    if (current?.type!=='oauth' || typeof current.refresh!=='string' || !current.refresh) throw new MetadataError('auth_unavailable');
    if (current.expires>deadline) return undefined;
    const raw=await metadataRequest(plugin.TOKEN_URL,{method:'POST',signal,
      headers:{'Content-Type':'application/x-www-form-urlencoded'},
      body:new URLSearchParams({client_id:plugin.CLIENT_ID,client_secret:plugin.CLIENT_SECRET,
        refresh_token:current.refresh,grant_type:'refresh_token'}).toString()},deadline);
    if (typeof raw.access_token!=='string' || !raw.access_token || typeof raw.expires_in!=='number'
        || !Number.isFinite(raw.expires_in) || raw.expires_in<=300 || raw.expires_in>86400) throw new MetadataError('auth_unavailable');
    if (raw.refresh_token!==undefined && (typeof raw.refresh_token!=='string' || !raw.refresh_token)) throw new MetadataError('auth_unavailable');
    return {...current,access:raw.access_token,refresh:raw.refresh_token??current.refresh,
      expires:Date.now()+(raw.expires_in-300)*1000};
  },{signal});
}
export async function resolvePiAuth(provider,options,deadline) {
  if (!['codex','antigravity','opencode'].includes(provider)) throw new MetadataError('unsupported_provider');
  return withinDeadline(async () => {
    if (!options.loadSdk) {
      const file=await stat(options.auth_path).catch(()=>null);
      if (!file?.isFile() || file.size>1048576) throw new MetadataError('auth_unavailable');
      let record;
      try { record=JSON.parse(await readFile(options.auth_path,'utf8')); }
      catch { throw new MetadataError('auth_unavailable'); }
      const id={codex:'openai-codex',antigravity:'antigravity',opencode:'opencode-go'}[provider];
      const credential=record[id];
      if (!credential || (credential.type==='api_key' && typeof credential.key==='string' && credential.key.trimStart().startsWith('!'))) {
        throw new MetadataError('auth_unavailable');
      }
    }
    const sdk=await (options.loadSdk??loadSelectedSdk)(options);
    if (typeof sdk?.ModelRuntime?.create!=='function') throw new MetadataError('unsupported_pi_sdk');
    const signal=AbortSignal.timeout(Math.max(1,deadline-Date.now()));
    const runtime=await sdk.ModelRuntime.create({authPath:options.auth_path,modelsPath:null,
      allowModelNetwork:false,refreshOnCreate:false,signal});
    if (typeof runtime?.credentials?.read!=='function' || typeof runtime.getAuth!=='function') throw new MetadataError('unsupported_pi_sdk');
    const id={codex:'openai-codex',antigravity:'antigravity',opencode:'opencode-go'}[provider];
    const stored=await runtime.credentials.read(id,{signal});
    if (!stored || !['oauth','api_key'].includes(stored.type)) throw new MetadataError('auth_unavailable');
    const owner=options.owner==='foreign-readonly'?'foreign-readonly':'pi';
    if (provider==='antigravity') {
      let plugin;
      try { plugin=await (options.loadPlugin??loadSelectedPlugin)(options,'src/auth/index.ts'); }
      catch { throw new MetadataError('unsupported_pi_plugin'); }
      if (typeof plugin?.getApiKey!=='function') throw new MetadataError('unsupported_pi_plugin');
      // Use the selected plugin's verified owner constants and Pi store lock;
      // its independent Undici refresh function never bypasses our guard.
      const active=await refreshAntigravity(runtime,stored,plugin,owner,deadline);
      const raw=plugin.getApiKey(active);let key;
      try { key=JSON.parse(raw); } catch { throw new MetadataError('auth_unavailable'); }
      if (!key?.token || typeof key.projectId!=='string' || !key.projectId) throw new MetadataError('auth_unavailable');
      return {provider,kind:'oauth',owner,apiKey:raw,accessToken:key.token,projectId:key.projectId,
        accountScope:scope(provider,String(active.email??key.projectId))};
    }
    let key;
    if (owner==='foreign-readonly') {
      if (stored.type==='oauth' && (!Number.isFinite(stored.expires) || stored.expires<=deadline)) throw new MetadataError('auth_expired');
      key=stored.type==='oauth'?stored.access:stored.key;
    } else key=(await runtime.getAuth(id,{signal}))?.auth?.apiKey;
    if (typeof key!=='string' || !key) throw new MetadataError('auth_unavailable');
    if (provider==='opencode') {
      if (stored.type!=='api_key') throw new MetadataError('auth_unavailable');
      return {provider,kind:'api_key',owner,apiKey:key,accountScope:scope(provider,key)};
    }
    const decoded=tokenAccount(key);const accountId=decoded??stored.accountId;
    if (stored.type!=='oauth' || typeof accountId!=='string' || !/^[a-zA-Z0-9_-]{1,128}$/.test(accountId)) throw new MetadataError('auth_scope_unavailable');
    if (decoded && stored.accountId && decoded!==stored.accountId) throw new MetadataError('account_scope_mismatch');
    return {provider,kind:'oauth',owner,accessToken:key,accountId,accountScope:scope(provider,accountId)};
  },deadline,'auth_timeout');
}

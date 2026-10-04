import test from 'node:test';
import assert from 'node:assert/strict';
import {resolvePiAuth} from '../pi_quota/auth.mjs';
import {metadataRequest, installMetadataGuard} from '../pi_quota/network.mjs';
import {handleRequest} from '../pi_quota_query.mjs';
import {queryProviderUsage} from '../pi_quota/providers.mjs';
import {mkdtemp,writeFile,readFile,rm,mkdir} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join,resolve} from 'node:path';
import {spawnSync} from 'node:child_process';

const token='fixture-access-private';
const credential={type:'oauth',access:token,refresh:'fixture-refresh-private',expires:Date.now()+3600000,accountId:'account-one'};
const deadline=()=>Date.now()+1000;
const request={protocol_version:1,request_id:'0123456789abcdef0123456789abcdef',provider:'codex',sdk_path:'/fake',auth_path:'/fake/auth.json',timeout_seconds:1};
const throwing=()=>{throw new Error('model must never run');};
function fakeSdk(read=async()=>credential){return {ModelRuntime:{create:async options=>{
 assert.equal(options.allowModelNetwork,false);assert.equal(options.refreshOnCreate,false);assert.equal(options.modelsPath,null);
 return {credentials:{read},getAuth:async()=>({auth:{apiKey:token},source:'OAuth'}),prompt:throwing,stream:throwing,complete:throwing};}},createAgentSession:throwing};}
test('testNoModelCallsOnImportOrAuth',async()=>{
 const result=await resolvePiAuth('codex',{auth_path:'/fake/auth.json',sdk_path:'/fake',loadSdk:async()=>fakeSdk()},deadline());
 assert.equal(result.accessToken,token);assert.equal(result.owner,'pi');assert.equal(result.accountId,'account-one');
});
test('testUnsupportedSdkFailsClosed',async()=>{
 await assert.rejects(resolvePiAuth('codex',{loadSdk:async()=>({createAgentSession:throwing})},deadline()),{code:'unsupported_pi_sdk'});
});
test('testMissingPluginNeverPrompts',async()=>{
 await assert.rejects(resolvePiAuth('antigravity',{loadSdk:async()=>fakeSdk(),loadPlugin:async()=>{throw new Error('missing');}},deadline()),{code:'unsupported_pi_plugin'});
});
test('testAuthTimeout',async()=>{
 await assert.rejects(resolvePiAuth('codex',{loadSdk:async()=>fakeSdk(()=>new Promise(()=>{}))},Date.now()+20),{code:'auth_timeout'});
});
test('testNoSecretOutput',async()=>{
 const response=await handleRequest(request,{resolveAuth:async()=>{throw new Error(token+' fixture-refresh-private');}});
 assert.equal(response.status,'error');assert.equal(response.error_code,'auth_unavailable');assert.ok(!JSON.stringify(response).includes(token));
});
test('testInvalidRequestDoesNotEchoUntrustedValues',async()=>{
 const result=await handleRequest({...request,provider:token,request_id:'fixture-refresh-private'});
 assert.equal(result.status,'error');assert.ok(!JSON.stringify(result).includes(token));assert.ok(!JSON.stringify(result).includes('fixture-refresh-private'));
});
test('testModelEndpointRejected',async()=>{
 let calls=0;
 await assert.rejects(metadataRequest('https://chatgpt.com/backend-api/codex/responses',{transport:async()=>{calls++;}},deadline()),{code:'metadata_route_denied'});assert.equal(calls,0);
});
test('testImportGuardPrecedesSdkLoading',async()=>{
 let calls=0;const restore=installMetadataGuard(deadline(),async()=>{calls++;throw new Error('network must not run');});
 try{
  await assert.rejects(resolvePiAuth('codex',{loadSdk:async()=>{await fetch('https://api.openai.com/v1/responses');return fakeSdk();}},deadline()));assert.equal(calls,0);
 }finally{restore();}
});
test('testForeignCredentialsCannotRefresh',async()=>{
 let calls=0;const sdk=fakeSdk(async()=>({...credential,expires:1}));sdk.ModelRuntime.create=async()=>({credentials:{read:async()=>({...credential,expires:1})},getAuth:async()=>{calls++;throwing();}});
 await assert.rejects(resolvePiAuth('codex',{owner:'foreign-readonly',loadSdk:async()=>sdk},deadline()),{code:'auth_expired'});assert.equal(calls,0);
});
test('testSelectedSdkReadsOnlyFakePiAuth',async()=>{
 const tmp=await mkdtemp(join(tmpdir(),'qs-pi-auth-'));
 try {
  const auth=join(tmp,'auth.json');const official=join(tmp,'official-auth.json');
  await writeFile(auth,JSON.stringify({'openai-codex':credential}));await writeFile(official,'unchanged');
  const result=await resolvePiAuth('codex',{auth_path:auth,sdk_path:resolve('tests/fixtures/pi-live/fake-sdk')},deadline());
  assert.equal(result.accountId,'account-one');assert.equal(await readFile(official,'utf8'),'unchanged');
 }finally{await rm(tmp,{recursive:true,force:true});}
});
test('testCliDropsImportDiagnosticsAndCommandAuth',async()=>{
 const tmp=await mkdtemp(join(tmpdir(),'qs-pi-logs-'));
 try {
  await mkdir(join(tmp,'dist/core'),{recursive:true});await writeFile(join(tmp,'package.json'),'\{"name":"@earendil-works/pi-coding-agent","type":"module"\}');
  await writeFile(join(tmp,'dist/core/model-runtime.js'),`console.log('${token}');process.stderr.write('fixture-refresh-private');throw new Error('${token}');`);
  const auth=join(tmp,'auth.json');await writeFile(auth,JSON.stringify({'openai-codex':credential}));
  const child=spawnSync(process.execPath,['pi_quota_query.mjs'],{input:JSON.stringify({...request,sdk_path:tmp,auth_path:auth}),encoding:'utf8',timeout:3000});
  assert.equal(child.status,0);assert.equal(JSON.parse(child.stdout).error_code,'unsupported_pi_sdk');assert.ok(!child.stdout.includes(token));assert.equal(child.stderr,'');
  await writeFile(auth,JSON.stringify({'opencode-go':{type:'api_key',key:'!pi prompt forbidden'}}));
  await assert.rejects(resolvePiAuth('opencode',{auth_path:auth,sdk_path:tmp},deadline()),{code:'auth_unavailable'});
 }finally{await rm(tmp,{recursive:true,force:true});}
});
const codexUsage={rate_limit:{primary_window:{used_percent:19,reset_at:1790978000,limit_window_seconds:18000},secondary_window:{used_percent:8,reset_at:1791560000,limit_window_seconds:604800}}};
test('testCodexAccountScope',async()=>{
 const calls=[];const auth={provider:'codex',accessToken:token,accountId:'account-one',accountScope:'pi:codex:scope'};
 const payload=await queryProviderUsage('codex',auth,{request:async(url,init)=>{calls.push([url,init]);return {...codexUsage,account_id:'account-one'};}},deadline());
 assert.deepEqual(payload,codexUsage);assert.equal(calls.length,1);assert.equal(calls[0][1].headers['ChatGPT-Account-Id'],'account-one');
});
test('testScopeMismatchRejected',async()=>{
 await assert.rejects(queryProviderUsage('codex',{accountId:'account-one',accessToken:token},{request:async()=>({...codexUsage,account_id:'account-two'})},deadline()),{code:'account_scope_mismatch'});
});
test('testOpenCodeUsageWithoutAgentEnd',async()=>{
 const payload={usage:{rolling:{percent:20,resetsAt:'2026-10-03T05:00:00Z'},weekly:{percent:10,resetsAt:'2026-10-09T05:00:00Z'},monthly:{percent:5,resetsAt:'2026-11-01T00:00:00Z'}}};let calls=0;
 assert.deepEqual(await queryProviderUsage('opencode',{apiKey:token},{request:async(url)=>{calls++;assert.equal(url,'https://opencode.ai/zen/go/v1/usage');return payload;}},deadline()),payload);assert.equal(calls,1);
});
test('testAntigravityUsageWithoutAgentEnd',async()=>{
 const groups=[{displayName:'Gemini',buckets:[{bucketId:'gemini-5h',window:'5h',remainingFraction:.8,resetTime:'2026-10-03T05:00:00Z'},{bucketId:'gemini-week',window:'weekly',remainingFraction:.9,resetTime:'2026-10-09T05:00:00Z'}]}];let calls=0;
 const transport=async(url)=>{assert.ok(url.endsWith(':retrieveUserQuotaSummary'));calls++;return new Response(JSON.stringify({groups}),{status:200});};
 const restore=installMetadataGuard(deadline(),transport);
 try{
  const payload=await queryProviderUsage('antigravity',{apiKey:token,projectId:'project-one'},{loadPlugin:async()=>({fetchAccountUsage:async()=>{const raw=await (await fetch('https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary',{method:'POST'})).json();return {projectId:'project-one',groups:raw.groups};}})},deadline());
  assert.deepEqual(payload,{groups});assert.equal(calls,1);
 }finally{restore();}
});
test('testPluginCannotInventQuotaFromMissingRawFraction',async()=>{
 const restore=installMetadataGuard(deadline(),async()=>new Response(JSON.stringify({groups:[{displayName:'Gemini',buckets:[{bucketId:'gemini-5h',window:'5h'}]}]})));
 try{await assert.rejects(queryProviderUsage('antigravity',{apiKey:token,projectId:'p'},{loadPlugin:async()=>({fetchAccountUsage:async()=>{await fetch('https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary',{method:'POST'});return {projectId:'p',groups:[]};}})},deadline()),{code:'invalid_usage'});}finally{restore();}
});
test('testOwnedAntigravityRefreshUsesOnlyGuardedAuthEndpoint',async()=>{
 let current={...credential,expires:1,projectId:'project-one',email:'fixture@example.invalid'};let writes=0;const calls=[];
 const sdk={ModelRuntime:{create:async()=>({credentials:{read:async()=>current,modify:async(id,fn)=>{assert.equal(id,'antigravity');current=(await fn(current))??current;writes++;return current;}},getAuth:throwing})}};
 const plugin={getApiKey:c=>JSON.stringify({token:c.access,projectId:c.projectId}),TOKEN_URL:'https://oauth2.googleapis.com/token',CLIENT_ID:'fixture-client-id',CLIENT_SECRET:'fixture-client-secret',refreshAntigravityToken:throwing};
 const restore=installMetadataGuard(deadline(),async(url,init)=>{calls.push(url);assert.ok(init.body.includes('grant_type=refresh_token'));return new Response(JSON.stringify({access_token:'fixture-refreshed-private',expires_in:3600}));});
 try{
  const result=await resolvePiAuth('antigravity',{loadSdk:async()=>sdk,loadPlugin:async()=>plugin},deadline());
  assert.equal(result.accessToken,'fixture-refreshed-private');assert.equal(writes,1);assert.deepEqual(calls,['https://oauth2.googleapis.com/token']);
 }finally{restore();}
});
test('testAntigravityForeignOwnerNeverWrites',async()=>{
 let writes=0;const sdk={ModelRuntime:{create:async()=>({credentials:{read:async()=>({...credential,expires:1}),modify:async()=>{writes++;throwing();}},getAuth:throwing})}};
 await assert.rejects(resolvePiAuth('antigravity',{owner:'foreign-readonly',loadSdk:async()=>sdk,loadPlugin:async()=>({getApiKey:throwing})},deadline()),{code:'auth_expired'});assert.equal(writes,0);
});

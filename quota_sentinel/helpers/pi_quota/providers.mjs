import {metadataRequest,MetadataError,withinDeadline} from './network.mjs';
import {loadSelectedPlugin} from './auth.mjs';

function invalid() { throw new MetadataError('invalid_usage'); }
function number(value, maximum) { return typeof value==='number' && Number.isFinite(value) && value>=0 && value<=maximum; }
function codexWindow(value, seconds) {
  if (!value || !number(value.used_percent,100) || !Number.isSafeInteger(value.reset_at)
      || value.reset_at<=0 || value.limit_window_seconds!==seconds) invalid();
  return {used_percent:value.used_percent,reset_at:value.reset_at,limit_window_seconds:seconds};
}
function apiWindow(value) {
  if (!value || !number(value.percent,100) || typeof value.resetsAt!=='string' || !Number.isFinite(Date.parse(value.resetsAt))) invalid();
  return {percent:value.percent,resetsAt:value.resetsAt};
}
function rawGroups(raw) {
  if (!Array.isArray(raw?.groups) || !raw.groups.length) invalid();
  return {groups:raw.groups.map(group=>{
    if (typeof group.displayName!=='string' || !Array.isArray(group.buckets)) invalid();
    return {displayName:group.displayName,buckets:group.buckets.map(bucket=>{
      // The plugin fills missing fractions with zero and clamps bad values.
      // Trust the intercepted server fields before those transformations.
      if (!number(bucket.remainingFraction,1) || typeof bucket.window!=='string'
          || typeof bucket.resetTime!=='string' || typeof bucket.bucketId!=='string') invalid();
      return {bucketId:bucket.bucketId,window:bucket.window,
        remainingFraction:bucket.remainingFraction,resetTime:bucket.resetTime};
    })};
  })};
}
export async function queryProviderUsage(provider,auth,options,deadline) {
  if (auth.provider && auth.provider!==provider) throw new MetadataError('account_scope_mismatch');
  const request=options.request??metadataRequest;
  if (provider==='codex') {
    const raw=await request('https://chatgpt.com/backend-api/wham/usage',{
      method:'GET',headers:{Authorization:'Bearer '+auth.accessToken,'ChatGPT-Account-Id':auth.accountId}},deadline);
    if (raw.account_id!==undefined && raw.account_id!==auth.accountId) throw new MetadataError('account_scope_mismatch');
    return {rate_limit:{primary_window:codexWindow(raw.rate_limit?.primary_window,18000),
      secondary_window:codexWindow(raw.rate_limit?.secondary_window,604800)}};
  }
  if (provider==='opencode') {
    const raw=await request('https://opencode.ai/zen/go/v1/usage',{
      method:'GET',headers:{Authorization:'Bearer '+auth.apiKey}},deadline);
    const usage=raw?.usage;
    if (!usage) invalid();
    const result={rolling:apiWindow(usage.rolling),weekly:apiWindow(usage.weekly)};
    if (usage.monthly!==undefined) result.monthly=apiWindow(usage.monthly);
    return {usage:result};
  }
  if (provider==='antigravity') {
    const originalFetch=globalThis.fetch;
    const previousKeepalive=process.env.ANTIGRAVITY_NO_KEEPALIVE;
    const previousPrewarm=process.env.ANTIGRAVITY_NO_PREWARM;
    process.env.ANTIGRAVITY_NO_KEEPALIVE='1';process.env.ANTIGRAVITY_NO_PREWARM='1';
    let summary;
    globalThis.fetch=async (input,init)=>{
      const response=await originalFetch(input,init);
      const url=new URL(input instanceof Request?input.url:String(input));
      if (url.pathname==='/v1internal:retrieveUserQuotaSummary' && response.ok) {
        summary=await response.clone().json();
      }
      return response;
    };
    try {
      const plugin=await (options.loadPlugin??loadSelectedPlugin)(options,'src/usage/usage.ts');
      if (typeof plugin?.fetchAccountUsage!=='function') throw new MetadataError('unsupported_pi_plugin');
      const usage=await withinDeadline(()=>plugin.fetchAccountUsage(auth.apiKey),deadline);
      if (typeof usage?.projectId!=='string' || usage.projectId!==auth.projectId) throw new MetadataError('account_scope_mismatch');
      return rawGroups(summary);
    } finally {
      globalThis.fetch=originalFetch;
      for (const [key,value] of [['ANTIGRAVITY_NO_KEEPALIVE',previousKeepalive],['ANTIGRAVITY_NO_PREWARM',previousPrewarm]]) {
        if (value===undefined) delete process.env[key];else process.env[key]=value;
      }
    }
  }
  throw new MetadataError('unsupported_provider');
}

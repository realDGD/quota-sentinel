import {pathToFileURL} from 'node:url';
import {resolvePiAuth} from './pi_quota/auth.mjs';
import {installMetadataGuard,MetadataError,withinDeadline} from './pi_quota/network.mjs';

const codes=new Set(['unsupported_pi_sdk','unsupported_pi_plugin','unsupported_provider','auth_unavailable',
 'auth_expired','auth_scope_unavailable','account_scope_mismatch','auth_timeout','metadata_timeout',
 'metadata_route_denied','metadata_redirect_denied','metadata_http_error','metadata_invalid_response',
 'metadata_invalid_json','metadata_oversized','invalid_request','invalid_usage']);
export async function handleRequest(request, dependencies={}) {
  const base={protocol_version:1,request_id:request?.request_id??null,provider:request?.provider??null};
  let restore;
  try {
    if (request?.protocol_version!==1 || !/^[a-f0-9-]{16,80}$/.test(request.request_id)
      || !['codex','antigravity','opencode'].includes(request.provider)
      || typeof request.timeout_seconds!=='number' || !Number.isFinite(request.timeout_seconds)
      || request.timeout_seconds<=0 || request.timeout_seconds>3600) throw new MetadataError('invalid_request');
    const deadline=Date.now()+Math.floor(request.timeout_seconds*1000);
    restore=installMetadataGuard(deadline,dependencies.transport??globalThis.fetch);
    const auth=await (dependencies.resolveAuth??resolvePiAuth)(request.provider,request,deadline);
    const query=dependencies.queryUsage??(await import('./pi_quota/providers.mjs')).queryProviderUsage;
    const payload=await withinDeadline(()=>query(request.provider,auth,request,deadline),deadline);
    return {...base,status:'ok',account_scope:auth.accountScope,queried_at:Math.floor(Date.now()/1000),payload};
  } catch(error) {
    return {...base,status:'error',error_code:codes.has(error?.code)?error.code:'auth_unavailable'};
  } finally { if (!dependencies.keepGuard) restore?.(); }
}

if (process.argv[1] && import.meta.url===pathToFileURL(process.argv[1]).href) {
  // Imported SDK/plugin diagnostics cannot print bearer tokens to either pipe.
  const output=process.stdout.write.bind(process.stdout);
  process.stdout.write=()=>true;process.stderr.write=()=>true;
  console.log=console.error=console.warn=()=>{};
  let raw='';
  try {
    for await (const chunk of process.stdin) {
      raw+=chunk.toString('utf8');if(Buffer.byteLength(raw)>65536) throw new Error();
    }
    const response=await handleRequest(JSON.parse(raw),{keepGuard:true});
    const encoded=JSON.stringify(response);
    if (Buffer.byteLength(encoded)>1048576) throw new Error();
    output(encoded+'\n',()=>process.exit(0));
  } catch { output(JSON.stringify({protocol_version:1,request_id:null,provider:null,status:'error',error_code:'invalid_request'})+'\n'); }
}

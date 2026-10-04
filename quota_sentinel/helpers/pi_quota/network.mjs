// Every imported provider sees this guard before it can make a request.
export class MetadataError extends Error {
  constructor(code) { super(code); this.code = code; }
}
const MAX_BYTES = 1048576;
const routes = new Map([
  ['https://chatgpt.com/backend-api/wham/usage', 'GET'],
  ['https://opencode.ai/zen/go/v1/usage', 'GET'],
  ['https://auth.openai.com/oauth/token', 'POST'],
  ['https://oauth2.googleapis.com/token', 'POST'],
]);
for (const host of ['daily-cloudcode-pa.googleapis.com', 'daily-cloudcode-pa.sandbox.googleapis.com', 'cloudcode-pa.googleapis.com']) {
  for (const method of ['loadCodeAssist', 'retrieveUserQuotaSummary', 'fetchAvailableModels']) {
    routes.set(`https://${host}/v1internal:${method}`, 'POST');
  }
}
export async function withinDeadline(work, deadline, code = 'metadata_timeout') {
  const remaining = deadline - Date.now();
  if (!Number.isFinite(remaining) || remaining <= 0) throw new MetadataError(code);
  let timer;
  try {
    return await Promise.race([Promise.resolve().then(work), new Promise((_, reject) => {
      timer = setTimeout(() => reject(new MetadataError(code)), remaining);
    })]);
  } finally { clearTimeout(timer); }
}
function checkRoute(input, options) {
  let url;
  try { url = new URL(input instanceof Request ? input.url : String(input)); }
  catch { throw new MetadataError('metadata_route_denied'); }
  const method = String(options?.method ?? (input instanceof Request ? input.method : 'GET')).toUpperCase();
  if (url.username || url.password || url.search || url.hash || routes.get(url.href) !== method) {
    throw new MetadataError('metadata_route_denied');
  }
  return url;
}
async function checkedFetch(input, options, deadline, transport) {
  const url = checkRoute(input, options);
  return withinDeadline(async () => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), Math.max(1, deadline - Date.now()));
    const external = options?.signal;
    const abort = () => controller.abort();
    if (external?.aborted) abort();
    else external?.addEventListener('abort', abort, {once:true});
    try {
      const response = await transport(url.href, {...options, redirect:'manual', signal:controller.signal});
      if (response.status >= 300 && response.status < 400) throw new MetadataError('metadata_redirect_denied');
      if (!response.body?.getReader) throw new MetadataError('metadata_invalid_response');
      const reader = response.body.getReader(); let size=0; const chunks=[];
      try {
        while (true) {
          const {done,value} = await reader.read(); if (done) break;
          size += value.byteLength;
          if (size > MAX_BYTES) throw new MetadataError('metadata_oversized');
          chunks.push(value);
        }
      } catch (error) { await reader.cancel().catch(()=>{}); throw error; }
      const body = Buffer.concat(chunks);
      return new Response(body, {status:response.status, headers:response.headers});
    } finally {
      clearTimeout(timer); external?.removeEventListener('abort',abort);
    }
  }, deadline);
}
export async function metadataRequest(input, options = {}, deadline) {
  const {transport=globalThis.fetch, ...init} = options;
  const response = await checkedFetch(input, init, deadline, transport);
  if (!response.ok) throw new MetadataError('metadata_http_error');
  try { return await response.json(); }
  catch { throw new MetadataError('metadata_invalid_json'); }
}
export function installMetadataGuard(deadline, transport=globalThis.fetch) {
  const previous = globalThis.fetch;
  globalThis.fetch = (input, options={}) => checkedFetch(input,options,deadline,transport);
  return () => { globalThis.fetch=previous; };
}

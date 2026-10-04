import {readFile} from 'node:fs/promises';
const forbidden=()=>{throw new Error('fixture model operation forbidden');};
export const createAgentSession=forbidden;
export class ModelRuntime {
 static async create(options) {
  if(options.modelsPath!==null || options.refreshOnCreate!==false || options.allowModelNetwork!==false) throw new Error('unsafe SDK options');
  const records=JSON.parse(await readFile(options.authPath,'utf8'));
  return {credentials:{read:async id=>records[id]},getAuth:async id=>({auth:{apiKey:records[id]?.access??records[id]?.key}}),
    prompt:forbidden,stream:forbidden,complete:forbidden};
 }
}

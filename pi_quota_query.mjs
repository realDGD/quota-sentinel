export * from './quota_sentinel/helpers/pi_quota_query.mjs';
import { main } from './quota_sentinel/helpers/pi_quota_query.mjs';
import {pathToFileURL} from 'node:url';
if (process.argv[1] && import.meta.url===pathToFileURL(process.argv[1]).href) await main();

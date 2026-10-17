import { dailyBackup } from '../../serverless/jobs.mjs';
export default async () => { await dailyBackup(); return new Response(null,{status:204}); };
export const config = { schedule: '30 2 * * *' };

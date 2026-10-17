import { runJobs } from '../../serverless/jobs.mjs';
export default async () => { await runJobs(); return new Response(null,{status:204}); };
export const config = { schedule: '*/15 * * * *' };

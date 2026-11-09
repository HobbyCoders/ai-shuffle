// Real UI sends and real Monitor wake; never a hand-seeded model note.
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { join } from 'node:path';
import { runJourney, devloopStatusPath } from './runner.mjs';
import { openNewChat, typeAndSend, waitTurnSettled, expect, uniq, chatIdByToken, PROJECT, pickAccess } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
const PY = process.env.TIME_PROBE_PYTHON || 'python';
function stage(status, args) {
 const result = spawnSync(PY, [join(status.checkout, 'scripts/verify_time_context_devloop.py'), '--status', devloopStatusPath(), ...args], {encoding:'utf8', timeout:10000});
 if (result.status !== 0) throw new Error(`private DB staging failed: ${result.stderr || result.stdout}`);
}
function requests(status) {
 return readFileSync(status.fake_ai.request_log,'utf8').trim().split('\n').filter(Boolean).map(JSON.parse).filter(r=>r.body).map(r => typeof r.body === 'string' ? JSON.parse(r.body) : r.body);
}
function userTail(body) {
 const rows = body.messages || body.input || [];
 const row = rows.filter(m => m.role === 'user').at(-1);
 if (!row) return '';
 return typeof row.content === 'string' ? row.content : (row.content || []).map(b => b.text || '').join('\n');
}
function note(text) {
 const blocks = text.match(/<system.reminder>[\s\S]*?<\/system.reminder>/g) || [];
 return blocks.findLast(block => block.includes('Current time:')) || text;
}
async function capture(status, predicate, since=0) {
 const end = Date.now() + 30000;
 while (Date.now() < end) {
  const found = requests(status).slice(since).map(userTail).findLast(predicate);
  if (found) return note(found);
  await new Promise(resolve => setTimeout(resolve,250));
 }
 throw new Error('no matching real model request after 30 seconds');
}
await runJourney({
 name:'time-awareness', allow:DEV_INSTANCE_NOISE,
 // This is a prompt-producing backend change, not a responsive layout change.
 viewports:['desktop'], throttles:['fast'],
 cases:[{name:'owner-wake-and-utc',
  async setup(ctx) {
   await openNewChat(ctx, {project: PROJECT});
   await pickAccess(ctx.page, "Bypass");
   const token=`time-awareness-${uniq()}`;
   ctx.page.timeToken=token;
   await typeAndSend(ctx.page, `${token} We will test GamingPC tomorrow. [script:default]`);
   await waitTurnSettled(ctx.page,{text:'Fake AI here'});
   const sid=await chatIdByToken(ctx.page,token);
   ctx.page.timeSession=sid;
   stage(ctx.status,['--zone','America/New_York','--session',sid]);
  },
  async act({page,status,log,mark}) {
   const token=page.timeToken, sid=page.timeSession;
   await mark('owner turn after the prior evening');
   await typeAndSend(page,`${token}-owner [script:default]`);
   await waitTurnSettled(page,{text:'Fake AI here'});
   const owner=await capture(status,t=>t.includes(`${token}-owner`) && t.includes("Owner's previous message:"));
   expect(owner.includes('EST (America/New_York)'), 'owner saved timezone, not UTC day');
   await mark('real background task completion starts a new turn');
   const wakeStart = requests(status).length;
   await typeAndSend(page,`${token}-monitor [script:time-awareness-wake]`);
   await waitTurnSettled(page,{text:'Waiting for the time probe.'});
   stage(status,['--session',sid]);
   const wake=await capture(status,t=>t.includes('Current time:') && t.includes("Owner's previous message:") && (t.includes('task-notification') || t.includes('Background task')), wakeStart);
   expect(wake.includes('EST (America/New_York)'), 'the wake kept the actual owner gap');
   await mark('unavailable owner zone uses labelled UTC');
   stage(status,['--zone','unset']);
   await typeAndSend(page,`${token}-utc [script:default]`);
   await waitTurnSettled(page,{text:'Fake AI here'});
   const utc=await capture(status,t=>t.includes(`${token}-utc`) && t.includes('UTC; fallback'));
   const out=process.env.TIME_EVIDENCE_DIR;
   if(out) { mkdirSync(out,{recursive:true}); writeFileSync(join(out,'producing-path-notes.json'),JSON.stringify({owner,wake,utc},null,2)); }
   log(JSON.stringify({owner,wake,utc}));
  }
 }]
});

import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { verify } from 'node:crypto';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
const script=fileURLToPath(new URL('../scripts/publish-source.mjs',import.meta.url));
const preload=new URL('./helpers/publish-source-preload.mjs',import.meta.url).href;
for(const scenario of ['new','reuse','moved','corrupt'])test(`publisher ${scenario}: immutable GitHub bytes and signed D1 publication`,async t=>{
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'shuffle-publish-test-'));t.after(()=>fs.rm(root,{recursive:true,force:true}));
 const result=spawnSync(process.execPath,['--import',preload,script],{encoding:'utf8',env:{...process.env,RUNNER_TEMP:root,GITHUB_SHA:'a'.repeat(40),GH_TOKEN:'fixture',ACCOUNT_HUB_DATABASE_ID:'11111111-1111-1111-1111-111111111111',PUBLISH_TEST_CASE:scenario}});
 assert.equal(result.status,scenario==='corrupt'?1:0,result.stderr);
 const events=JSON.parse(await fs.readFile(path.join(root,'events.json'),'utf8'));
 if(scenario==='moved'||scenario==='corrupt'){
  assert(!events.includes('publish'));await assert.rejects(fs.stat(path.join(root,'record.json')),{code:'ENOENT'});
 }else{
  const row=JSON.parse(await fs.readFile(path.join(root,'record.json'),'utf8')),signed=JSON.parse(row.manifest);
  assert.equal(row.asset_id,123);assert(verify(null,Buffer.from(signed.payload,'base64'),await fs.readFile(path.join(root,'public.pem'),'utf8'),Buffer.from(signed.signature,'base64')));
  const manifest=JSON.parse(Buffer.from(signed.payload,'base64'));assert.equal(manifest.revision,'a'.repeat(40));assert.deepEqual(manifest.container,{sequence:42,runtime:'b'.repeat(64)});assert(events.includes('publish'));
 }
 if(scenario==='reuse'){assert(events.includes('reuse'));assert(!events.some(url=>url.startsWith('https://uploads.github.com/')||url.includes('/zipball/')));}
 for(const name of ['desktop-source.sql','desktop-source-d1.json','desktop-source-readback.sql','a'.repeat(40)+'.zip'])await assert.rejects(fs.stat(path.join(root,name)),{code:'ENOENT'});
});

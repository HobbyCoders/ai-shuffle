import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { verify } from 'node:crypto';
import { promises as fs, readFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
const script=fileURLToPath(new URL('../scripts/publish-source.mjs',import.meta.url));
const preload=new URL('./helpers/publish-source-preload.mjs',import.meta.url).href;
const stable=scenario=>['stable','offmain','older','prerelease'].includes(scenario);
for(const scenario of ['new','reuse','moved','corrupt','stable','offmain','older','prerelease'])test(`publisher ${scenario}: immutable runtime archive and signed D1 publication`,async t=>{
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'shuffle-publish-test-'));t.after(()=>fs.rm(root,{recursive:true,force:true}));
 const env={...process.env,RUNNER_TEMP:root,GITHUB_SHA:'a'.repeat(40),GH_TOKEN:'fixture',ACCOUNT_HUB_DATABASE_ID:'11111111-1111-1111-1111-111111111111',PUBLISH_TEST_CASE:scenario};
 if(stable(scenario))Object.assign(env,{SOURCE_CHANNEL:'stable',RELEASE_TAG:'v1.2.3',GITHUB_SHA:'f'.repeat(40)});
 if(scenario==='prerelease')env.RELEASE_TAG='v1.2.3-rc.1';
 const result=spawnSync(process.execPath,['--import',preload,script],{encoding:'utf8',env});
 const fails=['corrupt','offmain','prerelease'].includes(scenario);
 assert.equal(result.status,fails?1:0,result.stderr);
 const events=JSON.parse(await fs.readFile(path.join(root,'events.json'),'utf8'));
 if(scenario==='offmain')assert.match(result.stderr,/is not on main/);
 if(scenario==='prerelease')assert.match(result.stderr,/must be a final/);
 if(scenario==='older')assert(!events.includes('archive')&&!events.some(url=>String(url).startsWith('https://')));
 if(scenario==='moved'||scenario==='older'||fails){
  assert(!events.includes('publish'));await assert.rejects(fs.stat(path.join(root,'record.json')),{code:'ENOENT'});
 }else{
  const row=JSON.parse(await fs.readFile(path.join(root,'record.json'),'utf8')),signed=JSON.parse(row.manifest);
  assert.equal(row.asset_id,123);assert(verify(null,Buffer.from(signed.payload,'base64'),await fs.readFile(path.join(root,'public.pem'),'utf8'),Buffer.from(signed.signature,'base64')));
  const manifest=JSON.parse(Buffer.from(signed.payload,'base64'));assert.equal(manifest.format,2);assert.equal(manifest.revision,'a'.repeat(40));assert.deepEqual(manifest.container,{sequence:42,runtime:'b'.repeat(64),requires:['base-1'],recommends:[]});
  // #1821 D5: the minimum launcher from desktop/launcher.json is signed with the release.
  assert.deepEqual(manifest.desktop,{launcher:JSON.parse(readFileSync(new URL('../launcher.json',import.meta.url),'utf8')).minimum});assert(events.includes('publish'));
  assert.equal(manifest.tree,'c'.repeat(64));assert(events.includes('archive'));
  if(stable(scenario)){
   assert.equal(row.channel,'stable');assert.equal(manifest.channel,'stable');assert.equal(manifest.version,'1.2.3');assert.equal(manifest.notes,'Faster startup.');
   assert(events.includes('on-main'));assert(!events.some(url=>String(url).endsWith('/commits/main')),'a stable publish is pinned to its tag, not to main');
  }else{
   assert.equal(row.channel,'edge');assert.equal(manifest.channel,'edge');assert.equal(manifest.version,'0.8.18-dev.4+gaaaaaaa');assert.equal(manifest.notes,'');
  }
 }
 assert(!events.some(url=>String(url).includes('/zipball/')),'the full-repository zipball is never published');
 if(scenario==='reuse'){assert(events.includes('reuse'));assert(!events.some(url=>String(url).startsWith('https://uploads.github.com/')));}
 if(scenario==='reuse'){
  const before=await fs.readFile(path.join(root,'record.json'),'utf8');
  // Drive the actual publisher four more times, with the record produced by the
  // first publication, like separate serialized CI-completion jobs.
  for(let i=0;i<4;i++){
   const duplicate=spawnSync(process.execPath,['--import',preload,script],{encoding:'utf8',env});
   assert.equal(duplicate.status,0,duplicate.stderr);assert.match(duplicate.stdout,/Already published/);
   assert.equal(await fs.readFile(path.join(root,'record.json'),'utf8'),before);
   const repeated=JSON.parse(await fs.readFile(path.join(root,'events.json'),'utf8'));
   assert(repeated.includes('read-publication'));assert(!repeated.includes('archive'));assert(!repeated.includes('publish'));
  }
  const row=JSON.parse(before);row.manifest=JSON.stringify({payload:Buffer.from('{}').toString('base64'),signature:''});
  await fs.writeFile(path.join(root,'record.json'),JSON.stringify(row));
  const invalid=spawnSync(process.execPath,['--import',preload,script],{encoding:'utf8',env});
  assert.equal(invalid.status,1);assert.match(invalid.stderr,/Existing source publication is invalid/);
 }
 for(const name of ['desktop-source-d1.json','a'.repeat(40)+'-runtime.zip'])await assert.rejects(fs.stat(path.join(root,name)),{code:'ENOENT'});
});

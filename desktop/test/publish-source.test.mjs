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
const stable=scenario=>['stable','stable-reuse','offmain','older','prerelease'].includes(scenario);
for(const scenario of ['new','nightly','edge','reuse','stable-reuse','clock-regression','moved','newer-attested','stale-feed','pointer-race','corrupt','stable','offmain','older','prerelease','overlays'])test(`publisher ${scenario}: immutable runtime archive and signed D1 publication`,async t=>{
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'shuffle-publish-test-'));t.after(()=>fs.rm(root,{recursive:true,force:true}));
 const env={...process.env,RUNNER_TEMP:root,GITHUB_SHA:'a'.repeat(40),GH_TOKEN:'fixture',RELEASE_GITHUB_REPO_ID:'42',ACCOUNT_HUB_DATABASE_ID:'11111111-1111-1111-1111-111111111111',PUBLISH_TEST_CASE:scenario};
 delete env.SOURCE_CHANNEL;
 if(scenario==='nightly'||scenario==='edge')env.SOURCE_CHANNEL=scenario;
 if(stable(scenario))Object.assign(env,{SOURCE_CHANNEL:'stable',RELEASE_TAG:'v1.2.3',GITHUB_SHA:'f'.repeat(40)});
 if(scenario==='prerelease')env.RELEASE_TAG='v1.2.3-rc.1';
 const result=spawnSync(process.execPath,['--import',preload,script],{encoding:'utf8',env});
 const fails=['clock-regression','corrupt','offmain','prerelease','stale-feed','pointer-race'].includes(scenario);
 assert.equal(result.status,fails?1:0,result.stderr);
 const events=JSON.parse(await fs.readFile(path.join(root,'events.json'),'utf8'));
 if(scenario==='offmain')assert.match(result.stderr,/is not on main/);
 if(scenario==='prerelease')assert.match(result.stderr,/must be a final/);
 if(scenario==='older')assert(!events.includes('archive')&&!events.some(url=>String(url).includes('/releases')));
 if(scenario==='clock-regression')assert.match(result.stderr,/signing clock/);
 if(scenario==='newer-attested'||scenario==='older'||fails){
  assert(!events.includes('publish'));await assert.rejects(fs.stat(path.join(root,'record.json')),{code:'ENOENT'});
 }else{
  const row=JSON.parse(await fs.readFile(path.join(root,'record.json'),'utf8')),signed=JSON.parse(row.manifest);
  assert.equal(row.asset_id,123);assert(verify(null,Buffer.from(signed.payload,'base64'),await fs.readFile(path.join(root,'public.pem'),'utf8'),Buffer.from(signed.signature,'base64')));
  const manifest=JSON.parse(Buffer.from(signed.payload,'base64'));assert.equal(manifest.format,2);assert.equal(manifest.revision,'a'.repeat(40));assert.deepEqual(manifest.container,{sequence:42,runtime:'b'.repeat(64),requires:scenario==='overlays'?['base-1','node-24','runtime-overlays-v1']:['base-1'],recommends:[],...(scenario==='overlays'?{overlays:[{kind:'node',capability:'node-24',platform:'linux-x86_64',url:'https://runtimes.example/node.tar.gz',sha256:'d'.repeat(64),size:1024,unpacked_size:2048,executable:'node/bin/node'}]}:{})});
  // #1821 D5: the minimum launcher from desktop/launcher.json is signed with the release.
  assert.deepEqual(manifest.desktop,{launcher:JSON.parse(readFileSync(new URL('../launcher.json',import.meta.url),'utf8')).minimum});assert(events.includes('publish'));
  assert.equal(manifest.tree,'c'.repeat(64));assert(events.includes('archive'));
  if(stable(scenario)){
   assert.equal(row.channel,'stable');assert.equal(manifest.channel,'stable');assert.equal(manifest.version,'1.2.3');assert.equal(manifest.notes,'Faster startup.');
   assert(events.includes('on-main'));assert(!events.some(url=>String(url).endsWith('/commits/main')),'a stable publish is pinned to its tag, not to main');
  }else{
   assert(events.filter(event=>event==='ancestor-gate').length===2);
   const sql=await fs.readFile(path.join(root,'insert.sql'),'utf8');
   assert(sql.includes("AND (SELECT revision FROM (SELECT revision,sequence"));
   assert(sql.includes("AND (SELECT sequence FROM (SELECT revision,sequence"));
   assert.equal(row.channel,'edge');assert.equal(manifest.channel,scenario==='nightly'?'nightly':'edge');assert.equal(manifest.version,'0.8.18-dev.4+gaaaaaaa');assert.equal(manifest.notes,'');
  }
 }
 assert(!events.some(url=>String(url).includes('/zipball/')),'the full-repository zipball is never published');
 if(scenario==='reuse'){assert(events.includes('reuse'));assert(!events.some(url=>String(url).startsWith('https://uploads.github.com/')));}
 if(scenario==='reuse'||scenario==='stable-reuse'){
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

test('whole-release staging signs source bytes but never writes a D1 pointer', async t => {
 const root = await fs.mkdtemp(path.join(os.tmpdir(), 'shuffle-stage-source-'));
 t.after(() => fs.rm(root, { recursive: true, force: true }));
 const stage = path.join(root, 'source-evidence.json');
 const env = { ...process.env, RUNNER_TEMP: root, GITHUB_SHA: 'a'.repeat(40), GH_TOKEN: 'fixture',
  RELEASE_GITHUB_REPO_ID: '42', ACCOUNT_HUB_DATABASE_ID: '11111111-1111-1111-1111-111111111111',
  PUBLISH_TEST_CASE: 'stable', SOURCE_CHANNEL: 'stable', RELEASE_TAG: 'v1.2.3', SOURCE_STAGE_FILE: stage };
 const result = spawnSync(process.execPath, ['--import', preload, script], { encoding: 'utf8', env });
 assert.equal(result.status, 0, result.stderr);
 const evidence = JSON.parse(await fs.readFile(stage, 'utf8'));
 const signed = JSON.parse(evidence.manifest);
 const payload = Buffer.from(signed.payload, 'base64');
 assert(verify(null, payload, await fs.readFile(path.join(root, 'public.pem')), Buffer.from(signed.signature, 'base64')));
 const manifest = JSON.parse(payload);
 assert.equal(manifest.revision, 'a'.repeat(40));
 assert.equal(manifest.channel, 'stable');
 assert.equal(evidence.asset.id, 123);
 assert.equal(evidence.asset.digest, `sha256:${manifest.sha256}`);
 const events = JSON.parse(await fs.readFile(path.join(root, 'events.json')));
 assert(events.includes('archive'));
 assert(!events.includes('publish'));
 await assert.rejects(fs.stat(path.join(root, 'record.json')), { code: 'ENOENT' });
});

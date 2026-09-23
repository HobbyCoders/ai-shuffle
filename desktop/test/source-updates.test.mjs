import assert from 'node:assert/strict';
import { generateKeyPairSync, createHash, sign } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
const require = createRequire(import.meta.url);
const { SourceUpdates, verifySourceManifest, treeDigest } = require('../dist/main/source-updates.js');
const { buildEnvironment, removeSourceDirectory } = require('../dist/main/source-build.js');
const extractor = fileURLToPath(new URL('./helpers/source-archive.py', import.meta.url));
const repoScripts = fileURLToPath(new URL('../../scripts', import.meta.url));
// The publisher's own digest (scripts/source_archive.py), so the desktop check is tested against it, not against itself.
const publishedTree = archive => execFileSync(process.env.PYTHON||'python',['-c','import sys; sys.path.insert(0, sys.argv[1]); import source_archive as s; print(s.tree_digest(s.archive_files(sys.argv[2])))',repoScripts,archive],{encoding:'utf8'}).trim();
const A='a'.repeat(40), B='b'.repeat(40), C='c'.repeat(40), KEY='d'.repeat(64);
test('browser download verification and launcher share one public trust key',async()=>{
 const pem=await fs.readFile(new URL('../build/application-update-key.pem',import.meta.url),'utf8');
 const source=await fs.readFile(new URL('../../android/hub/src/desktop-updates.ts',import.meta.url),'utf8');
 const key=pem.split(/\r?\n/).filter(line=>line&&!line.startsWith('---')).join('');
 assert(source.includes(`const signingPublicKey='${key}'`));
});
async function fixture(t) {
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'shuffle-source-test-'));t.after(()=>fs.rm(root,{recursive:true,force:true}));
 const {privateKey,publicKey:key}=generateKeyPairSync('ed25519');const publicKey=key.export({type:'spki',format:'pem'});
 const bodies=new Map();const requests=[];let signed;let sequence=100;let builds=0;let fail=false;
 const options={root,publicKey,bootstrapNode:process.execPath,fetch:async url=>{requests.push(url);const feed=url.endsWith('/manifest')||url.endsWith('/manifest/edge');return new Response(feed?signed:bodies.get(url)||'missing',{status:feed||bodies.has(url)?200:404});},build:async(source,stage,revision)=>{
  builds++;if(fail)throw Error('Local build failed');
  for(const name of ['server/app/main.py','server/app/static/index.html','source/desktop/dist/main/managed-entry.js']){await fs.mkdir(path.dirname(path.join(stage,name)),{recursive:true});await fs.writeFile(path.join(stage,name),revision);}
  const electron=path.join(root,'dependencies',KEY,'electron.exe');
  for(const name of [electron,path.join(root,'runtimes',KEY,'worker/python/python.exe')]){await fs.mkdir(path.dirname(name),{recursive:true});await fs.writeFile(name,'runtime');}
  return {revision,pythonKey:null,electron,runtimeKey:KEY};
 }};
 async function release(revision,hostile,fields={}) {
  const source=path.join(root,'input',revision,`ai-shuffle-${revision}`);await fs.mkdir(path.join(source,'app'),{recursive:true});await fs.writeFile(path.join(source,'source.txt'),'real archive content');await fs.writeFile(path.join(source,'app','main.py'),'print("ok")');
  const archive=path.join(root,revision+'.zip');await fs.rm(archive,{force:true});execFileSync(process.env.PYTHON||'python',[extractor,'pack',path.dirname(source),archive]);
  const tree=publishedTree(archive);
  if(hostile)execFileSync(process.env.PYTHON||'python',['-c','import sys,zipfile; z=zipfile.ZipFile(sys.argv[1],"a"); i=zipfile.ZipInfo("placeholder"); i.filename=sys.argv[2]; z.writestr(i,"unsafe"); z.close()',archive,hostile]);
  const bytes=await fs.readFile(archive);const manifest={format:2,revision,sequence:sequence++,sha256:createHash('sha256').update(bytes).digest('hex'),size:bytes.length,channel:'stable',version:'0.9.'+(sequence-100),notes:'Notes for '+revision.slice(0,4),tree,...fields};
  for(const [key,value] of Object.entries(manifest))if(value===undefined)delete manifest[key];
  const payload=Buffer.from(JSON.stringify(manifest));signed=JSON.stringify({payload:payload.toString('base64'),signature:sign(null,payload,privateKey).toString('base64')});
  const url='https://hobbycoders.com/v1/desktop/sources/'+revision;bodies.set(url,bytes);return {manifest,signed,url};
 }
 return {root,options,create:()=>new SourceUpdates(options),release,bodies,requests,get builds(){return builds;},set fail(value){fail=value;},set signed(value){signed=value;}};
}
test('source builds stage without replacing running files and activate on restart; offline launches work',async t=>{
 const f=await fixture(t);await f.release(A);let u=f.create();await u.load();assert.equal((await u.download()).kind,'ready');const old=await u.select();await u.healthy();
 await fs.writeFile(path.join(f.root,'data-sentinel'),'keep');await f.release(B);await u.download();assert.equal(u.version,A);assert.equal(await fs.readFile(path.join(old.serverDir,'app/main.py'),'utf8'),A);
 u=f.create();await u.load();const current=await u.select();await u.healthy();assert.equal(current.revision,B);assert.equal(await fs.readFile(path.join(f.root,'data-sentinel'),'utf8'),'keep');
 assert.equal((await u.download()).kind,'current');assert.equal(f.builds,2);assert.equal(await fs.readFile(path.join(f.root,'node-path.txt'),'utf8'),current.node);
 const offline=new SourceUpdates({...f.options,fetch:async()=>{throw Error('offline');}});await offline.load();assert.equal((await offline.select()).revision,B);
});
test('build failures preserve the active revision; failed first boots block unsafe rollback but accept newer recovery',async t=>{
 const f=await fixture(t);await f.release(A);let u=f.create();await u.load();await u.download();await u.select();await u.healthy();
 await f.release(B);f.fail=true;await assert.rejects(u.download(),/Local build failed/);assert.equal(u.version,A);f.fail=false;await u.download();
 u=f.create();await u.load();await u.select();u=f.create();await u.load();await assert.rejects(u.select(),/No healthy/);await assert.rejects(u.download(),/failed to start/);
 await f.release(C);await u.download();await u.select();await u.healthy();assert.equal(u.version,C);
});
test('signature, checksum, traversal and Windows alias failures are rejected before builds',async t=>{
 const f=await fixture(t);const r=await f.release(A);const u=f.create();await u.load();
 const envelope=JSON.parse(r.signed);envelope.signature=Buffer.alloc(64).toString('base64');f.signed=JSON.stringify(envelope);await assert.rejects(u.download(),/signature/);f.signed=r.signed;
 f.bodies.set(r.url,Buffer.alloc(r.manifest.size));await assert.rejects(u.download(),/checksum/);assert.equal(f.builds,0);
 for(const name of ['../escape','C:/escape','folder/CON.txt','folder/file:stream','folder/file.','folder\\file']){
  const g=await fixture(t);await g.release(A,name);const v=g.create();await v.load();await assert.rejects(v.download());assert.equal(g.builds,0,name);
 }
});
test('older signed manifests cannot roll back a healthy revision; corrupt state fails closed',async t=>{
 const f=await fixture(t);const a=await f.release(A);await f.release(B);const u=f.create();await u.load();await u.download();await u.select();await u.healthy();f.signed=a.signed;assert.equal((await u.check()).kind,'current');assert.equal(u.version,B);
 assert.throws(()=>verifySourceManifest('{}',f.options.publicKey),/Unsigned/);await fs.writeFile(path.join(f.root,'state.json'),JSON.stringify({active:'../elsewhere'}));await assert.rejects(f.create().load(),/Invalid/);
});
test('cleanup detaches dependency junctions; builds do not inherit credentials',async t=>{
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'shuffle-cache-test-'));t.after(()=>fs.rm(root,{recursive:true,force:true}));
 const cache=path.join(root,'cache');await fs.mkdir(cache);await fs.writeFile(path.join(cache,'sentinel'),'keep');
 const version=path.join(root,'version');await fs.mkdir(path.join(version,'source/frontend'),{recursive:true});await fs.symlink(cache,path.join(version,'source/frontend/node_modules'),process.platform==='win32'?'junction':'dir');
 await removeSourceDirectory(version);assert.equal(await fs.readFile(path.join(cache,'sentinel'),'utf8'),'keep');
 process.env.SHUFFLE_TEST_SECRET='not-for-builds';try{assert.equal(buildEnvironment(process.execPath,'python').SHUFFLE_TEST_SECRET,undefined);}finally{delete process.env.SHUFFLE_TEST_SECRET;}
});
test('bootstrap first launch still downloads and selects an application; older source managers remain supported',async t=>{
 const bootstrap=(await fs.readFile(new URL('../scripts/bootstrap.mjs',import.meta.url),'utf8')).replaceAll('\r\n','\n');
 const start=bootstrap.indexOf('  await updates.load();');const end=bootstrap.indexOf('\n}\nconsole.log(`Starting AI Shuffle',start);
 assert(start>=0&&end>start);
 const body=bootstrap.slice(start,end);
 const run=updates=>vm.runInNewContext(`(async()=>{let selected;${body};return selected;})()`,{updates,console:{log(){}}});
 const f=await fixture(t);await f.release(A);const u=f.create();
 assert.equal((await run(u)).revision,A);assert.equal(f.builds,1);
 assert.deepEqual(f.requests,['https://hobbycoders.com/v1/desktop/manifest','https://hobbycoders.com/v1/desktop/sources/'+A]);
 let legacyCalls=0;
 const legacy={load:async()=>{},select:async()=>{if(!legacyCalls)throw Error('No application');return {revision:A};},check:async(_progress,interactive)=>{assert.equal(interactive,true);legacyCalls++;}};
 assert.equal((await run(legacy)).revision,A);assert.equal(legacyCalls,1);
});
test('checks verify metadata only; explicit download stages the signed release and pending survives checks',async t=>{
 const f=await fixture(t);await f.release(A);const u=f.create();await u.load();
 await u.download();await u.select();await u.healthy();const old=await f.release(B);
 f.requests.length=0;const builds=f.builds;const progress=[];
 assert.deepEqual(await u.check((...args)=>progress.push(args)),{kind:'available',version:B.slice(0,12),release:old.manifest.version,notes:old.manifest.notes});
 assert.deepEqual(f.requests,['https://hobbycoders.com/v1/desktop/manifest']);assert.equal(f.builds,builds);assert.deepEqual(progress,[]);
 assert.equal(u.pendingVersion,null);await assert.rejects(fs.stat(path.join(f.root,'staging')), {code:'ENOENT'});
 assert.deepEqual(await u.download(),{kind:'ready',version:B.slice(0,12),release:old.manifest.version,notes:old.manifest.notes});
 assert.equal(f.requests.filter(url=>url===old.url).length,1);assert.equal(f.builds,builds+1);
 assert.equal(await fs.readFile(path.join(f.root,'versions',B,'release.json'),'utf8'),old.signed);
 await f.release(C);f.requests.length=0;
 assert.equal((await u.check()).version,B.slice(0,12));assert.equal((await u.download()).version,B.slice(0,12));
 const restarted=f.create();await restarted.load();assert.equal((await restarted.check()).kind,'ready');
 assert.deepEqual(f.requests,[]);assert.equal(f.builds,builds+1);
});
test('invalid check metadata fails before any archive request and can be retried',async t=>{
 const f=await fixture(t);const release=await f.release(A);const u=f.create();await u.load();
 f.signed='{}';await assert.rejects(u.check(),/Unsigned/);assert.equal(f.builds,0);
 assert.deepEqual(f.requests,['https://hobbycoders.com/v1/desktop/manifest']);
 f.signed=release.signed;assert.equal((await u.check()).kind,'available');assert.equal(f.builds,0);
});
test('source commands guard concurrent checks and downloads and release the guard after failure',async t=>{
 const f=await fixture(t);await f.release(A);let unblock;let entered;
 const started=new Promise(resolve=>{entered=resolve;});const blocked=new Promise(resolve=>{unblock=resolve;});
 const fetch=f.options.fetch;let first=true;
 f.options.fetch=async(...args)=>{if(first){first=false;entered();await blocked;throw Error('Network interrupted');}return fetch(...args);};
 const u=f.create();await u.load();const checking=u.check();await started;
 await assert.rejects(u.check(),/already running/);await assert.rejects(u.download(),/already running/);
 unblock();await assert.rejects(checking,/Network interrupted/);assert.equal((await u.check()).kind,'available');
 let finishBuild;let buildEntered;const building=new Promise(resolve=>{buildEntered=resolve;});
 const build=f.options.build;f.options.build=async(...args)=>{buildEntered();await new Promise(resolve=>{finishBuild=resolve;});return build(...args);};
 const downloading=u.download();await building;
 await assert.rejects(u.check(),/already running/);await assert.rejects(u.download(),/already running/);
 finishBuild();assert.equal((await downloading).kind,'ready');assert.equal(f.builds,1);
});
test('automatic checks and downloads use the public source transport without account state',async t=>{
 const f=await fixture(t);await f.release(A);const requests=[];
 const module={exports:{}};const localRequire=createRequire(new URL('../dist/main/source-updates.js',import.meta.url));
 vm.runInNewContext(await fs.readFile(new URL('../dist/main/source-updates.js',import.meta.url),'utf8'),{
  exports:module.exports,module,Buffer,AbortSignal,process,
  require:id=>id==='./source-auth'?{SOURCE_ORIGIN:'https://hobbycoders.com',publicSourceFetch:async(...args)=>{requests.push(args);return f.options.fetch(args[0]);}}:localRequire(id)
 });
 const u=new module.exports.SourceUpdates({...f.options,fetch:undefined});await u.load();
 await u.check();assert.deepEqual(requests,[['https://hobbycoders.com/v1/desktop/manifest']]);
 await u.download();assert.equal(requests.length,3);assert(requests.every(args=>args.length===1));await u.select();await u.healthy();await f.release(B);
 await u.check();assert.equal(requests.length,4);assert.equal(f.builds,1);
});

test('installer baseline checks main without a source directory and transitions to a signed source update',async t=>{
 const f=await fixture(t);const baseline={revision:A,sequence:99};
 let u=new SourceUpdates({...f.options,baseline});await u.load();
 assert.equal(u.version,A);assert.equal(u.hasSourceState,false);
 await f.release(A);assert.equal((await u.check()).kind,'current');assert.equal(f.builds,0);
 await f.release(B);assert.equal((await u.check()).kind,'available');await u.download();
 assert.equal(u.version,A);assert.equal(u.hasSourceState,true);
 u=new SourceUpdates({...f.options,baseline});await u.load();assert.equal((await u.select()).revision,B);await u.healthy();
 assert.equal(u.version,B);assert.equal(u.pendingVersion,null);
 const old=await f.release(A);f.signed=old.signed;
 // The baseline cannot be activated by the launcher after a failed source boot.
 await f.release(C);await u.download();await u.select();
 u=new SourceUpdates({...f.options,baseline});await u.load();assert.equal(u.hasSourceState,true);await assert.rejects(u.select(),/No healthy/);
});

test('installer baseline rejects source metadata older than its bundled build',async t=>{
 const f=await fixture(t);await f.release(B);
 const u=new SourceUpdates({...f.options,baseline:{revision:A,sequence:200}});await u.load();
 assert.equal((await u.check()).kind,'current');assert.equal(f.builds,0);
});

test('compact Windows native and Python dependency caches resolve after activation; legacy layouts stay readable',async t=>{
 const f=await fixture(t);const build=f.options.build;const pythonKey='e'.repeat(64);
 f.options.build=async(...args)=>{
  const built=await build(...args);
  await fs.rename(path.join(f.root,'runtimes',KEY),path.join(f.root,'runtimes',KEY.slice(0,24)));
  const python=path.join(f.root,'dependencies',KEY.slice(0,24),'python',pythonKey.slice(0,24),'Scripts/python.exe');
  await fs.mkdir(path.dirname(python),{recursive:true});await fs.writeFile(python,'python runtime');
  return {...built,layout:2,pythonKey};
 };
 await f.release(A);const u=f.create();await u.load();await u.download();
 const app=await u.select();assert.equal(app.resources,path.join(f.root,'runtimes',KEY.slice(0,24)));
 assert.equal(app.python,path.join(f.root,'dependencies',KEY.slice(0,24),'python',pythonKey.slice(0,24),'Scripts/python.exe'));
 await u.healthy();assert.equal((await u.current()).revision,A);
});

test('a newer milestone installer supersedes older healthy source code but never rolls back failed boot state',async t=>{
 const f=await fixture(t);await f.release(A);let u=f.create();await u.load();await u.download();await u.select();await u.healthy();
 u=new SourceUpdates({...f.options,baseline:{revision:B,sequence:200}});await u.load();await u.useNewerBaseline();
 assert.equal(u.version,B);assert.equal(u.hasSourceState,false);assert(await fs.stat(path.join(f.root,'versions',A)));
 // A failed revision (sequence 100) newer than the installer (sequence 50) keeps the block: never roll back over its database.
 await fs.writeFile(path.join(f.root,'state.json'),JSON.stringify({active:A,blocked:true,failed:[A]}));
 u=new SourceUpdates({...f.options,baseline:{revision:B,sequence:50}});await u.load();await u.useNewerBaseline();
 assert.equal(u.hasSourceState,true);assert.equal(u.blocked,true);await assert.rejects(u.select(),/No healthy/);
 // Unreadable failed-revision metadata keeps the block too.
 await fs.writeFile(path.join(f.root,'state.json'),JSON.stringify({blocked:true,failed:[C]}));
 u=new SourceUpdates({...f.options,baseline:{revision:B,sequence:200}});await u.load();await u.useNewerBaseline();
 assert.equal(u.blocked,true);
});
test('DESK-02: a failed first boot stays blocked until a newer download or installer supersedes it',async t=>{
 const f=await fixture(t);await f.release(A);let u=f.create();await u.load();await u.download();await u.select();
 // Crash during the first boot of A: the next launch records the failure.
 u=f.create();await u.load();assert.equal(u.hasSourceState,true);assert.equal(u.blocked,true);assert.equal(u.pendingVersion,null);
 await assert.rejects(u.select(),/No healthy/);assert.deepEqual(JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8')),{failed:[A],blocked:true});
 // The installer baseline is older than the failed revision: the block prevents unsafe fallback.
 u=new SourceUpdates({...f.options,baseline:{revision:B,sequence:50}});await u.load();await u.useNewerBaseline();
 assert.equal(u.blocked,true);assert.equal(u.hasSourceState,true);assert.equal(u.version,B);await assert.rejects(u.select(),/No healthy/);
 // A retry of the failed revision is refused; a newer main-branch revision downloads, is selected and clears the block.
 await assert.rejects(u.download(),/failed to start/);
 await f.release(C);assert.equal((await u.download()).kind,'ready');assert.equal(u.blocked,false,'a pending build may run over a block');
 assert.equal(u.hasSourceState,true);await u.select();await u.healthy();assert.equal(u.version,C);
 assert.equal(JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8')).blocked,undefined);
 // A milestone installer newer than every failed revision clears the block on its own.
 await fs.writeFile(path.join(f.root,'state.json'),JSON.stringify({blocked:true,failed:[A]}));
 u=new SourceUpdates({...f.options,baseline:{revision:B,sequence:101}});await u.load();await u.useNewerBaseline();
 assert.equal(u.blocked,false);assert.equal(u.hasSourceState,false);
 const saved=JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8'));assert.equal(saved.blocked,undefined);assert.deepEqual(saved.failed,[A]);
 await f.release(A);await assert.rejects(u.download(),/failed to start/,'the failed revision stays refused after the block clears');
});
test('UI downloads stay staged even with a legacy launcher until explicit install is approved',async t=>{
 const f=await fixture(t);await f.release(A);let u=f.create();await u.load();await u.download();await u.select();await u.healthy();
 u.stageDownloads();await f.release(B);await u.download();
 let saved=JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8'));
 assert.equal(saved.staged,B);assert.equal(saved.pending,undefined);
 u=f.create();await u.load();u.stageDownloads();assert.equal((await u.select()).revision,A);assert.equal(u.pendingVersion,B);
 await fs.writeFile(path.join(f.root,'allow-updates.json'),JSON.stringify({allow_updates:false}));
 await assert.rejects(u.prepareInstall(),/disabled/);
 await fs.writeFile(path.join(f.root,'allow-updates.json'),JSON.stringify({allow_updates:true}));
 await u.prepareInstall();await u.cancelInstall();
 saved=JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8'));assert.equal(saved.staged,B);assert.equal(saved.pending,undefined);
 await u.prepareInstall();u=f.create();await u.load();assert.equal((await u.select()).revision,B);
});
test('disabled account policy blocks downloads and cancels staged activation on offline restart',async t=>{
 const f=await fixture(t);await f.release(A);let u=f.create();await u.load();await u.download();await u.select();await u.healthy();
 await f.release(B);await u.download();
 await fs.writeFile(path.join(f.root,'allow-updates.json'),JSON.stringify({allow_updates:false}));
 await assert.rejects(u.download(),/disabled/);
 u=f.create();await u.load();assert.equal(u.pendingVersion,null);assert.equal((await u.select()).revision,A);
 await fs.writeFile(path.join(f.root,'allow-updates.json'),JSON.stringify({allow_updates:true}));
 assert.equal((await u.download()).kind,'ready');
});
test('public source requests omit credentials and reject off-origin URLs and redirects',async()=>{
 const module={exports:{}};const requests=[];
 vm.runInNewContext(await fs.readFile(new URL('../dist/main/source-auth.js',import.meta.url),'utf8'),{
  exports:module.exports,module,URL,AbortSignal,fetch:async(...args)=>{requests.push(args);return new Response('ok');}
 });
 const request=module.exports.publicSourceFetch;
 await request('https://hobbycoders.com/v1/desktop/manifest');
 assert.equal(requests[0][1].credentials,'omit');assert.equal(requests[0][1].redirect,'error');assert.equal(requests[0][1].headers,undefined);
 await request('https://hobbycoders.com/v1/desktop/manifest/edge');
 for(const url of ['https://evil.example/v1/desktop/manifest','https://user:secret@hobbycoders.com/v1/desktop/manifest','https://hobbycoders.com/v1/desktop/manifest?token=secret','https://hobbycoders.com/v1/desktop/manifest/beta'])await assert.rejects(request(url),/Invalid source/);
 assert.equal(requests.length,2);
});

test('only signed stable releases are accepted by default; edge and unlabelled manifests are refused before any download',async t=>{
 const f=await fixture(t);const u=f.create();await u.load();
 for(const [fields,message] of [[{channel:'edge'},/did not return a signed stable release/],[{channel:undefined},/did not return a signed stable release/],[{tree:undefined},/no signed source tree/]]){
  f.requests.length=0;await f.release(A,undefined,fields);
  await assert.rejects(u.check(),message);await assert.rejects(u.download(),message);
  assert.deepEqual(f.requests,Array(2).fill('https://hobbycoders.com/v1/desktop/manifest'));assert.equal(f.builds,0);assert.equal(u.pendingVersion,null);
 }
 await f.release(A);assert.equal((await u.download()).kind,'ready');
});
test('the owner can opt one install into edge; the edge feed must be signed edge',async t=>{
 const f=await fixture(t);const u=f.create();await u.load();
 await fs.writeFile(path.join(f.root,'update-channel.json'),JSON.stringify({channel:'edge'}));
 assert.equal(await u.channel(),'edge');
 await f.release(A);await assert.rejects(u.check(),/did not return a signed edge release/);
 assert.deepEqual(f.requests,['https://hobbycoders.com/v1/desktop/manifest/edge']);
 await f.release(A,undefined,{channel:'edge',version:'0.9.1-dev.3+gabc1234'});assert.equal((await u.download()).kind,'ready');
 for(const text of ['{"channel":"beta"}','not json','{}']){
  await fs.writeFile(path.join(f.root,'update-channel.json'),text);await assert.rejects(u.channel(),/update-channel.json must contain/);
 }
 await fs.rm(path.join(f.root,'update-channel.json'));assert.equal(await u.channel(),'stable');
 assert.equal(await new SourceUpdates({...f.options,channel:'edge'}).channel(),'edge');
});
test('a tree digest that does not match the extracted files stops the update before the build',async t=>{
 const f=await fixture(t);await f.release(A);let u=f.create();await u.load();await u.download();await u.select();await u.healthy();
 await f.release(B,undefined,{tree:'f'.repeat(64)});const builds=f.builds;
 await assert.rejects(u.download(),/Source files do not match the signed release\. The update was not built/);
 assert.equal(f.builds,builds);assert.equal(u.version,A);assert.equal(u.pendingVersion,null);
 await assert.rejects(fs.stat(path.join(f.root,'staging')),{code:'ENOENT'});await assert.rejects(fs.stat(path.join(f.root,'versions',B)),{code:'ENOENT'});
 // A correctly signed tree for the same revision then installs.
 await f.release(B);assert.equal((await u.download()).kind,'ready');
});
test('the desktop tree digest matches the publisher for ordering and non-ASCII paths',async t=>{
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'shuffle-tree-test-'));t.after(()=>fs.rm(root,{recursive:true,force:true}));
 const top=path.join(root,'pack','ai-shuffle-'+A);const names=['z.txt','a-b/x','a.b','a/b','A/B','é/ü.md','日本/語.txt','a/b c','requirements.lock'];
 const files=[];
 for(const name of names){await fs.mkdir(path.dirname(path.join(top,name)),{recursive:true});await fs.writeFile(path.join(top,name),'content of '+name);files.push([name,createHash('sha256').update('content of '+name).digest('hex')]);}
 const archive=path.join(root,'tree.zip');execFileSync(process.env.PYTHON||'python',[extractor,'pack',path.join(root,'pack'),archive]);
 assert.equal(treeDigest(files),publishedTree(archive));assert.equal(treeDigest([...files].reverse()),treeDigest(files));
});
test('release fields are validated when present and older installed manifests without them stay readable',async t=>{
 const f=await fixture(t);const {privateKey,publicKey}=generateKeyPairSync('ed25519');
 const signed=fields=>{const payload=Buffer.from(JSON.stringify({format:2,revision:A,sequence:5,sha256:KEY,size:10,...fields}));return JSON.stringify({payload:payload.toString('base64'),signature:sign(null,payload,privateKey).toString('base64')});};
 assert.equal(verifySourceManifest(signed({}),publicKey).channel,undefined);
 assert.equal(verifySourceManifest(signed({channel:'edge',version:'1.2.3',notes:'x'.repeat(4000),tree:KEY}),publicKey).notes.length,4000);
 for(const fields of [{channel:'beta'},{tree:'abc'},{version:'latest'},{version:7},{notes:'x'.repeat(4001)},{notes:null}])assert.throws(()=>verifySourceManifest(signed(fields),publicKey),/release fields/,JSON.stringify(fields));
 // An install whose release.json predates channel and tree still orders against new releases.
 await f.release(B);let u=f.create();await u.load();await u.download();await u.select();await u.healthy();
 const legacy=await f.release(B,undefined,{channel:undefined,tree:undefined,version:undefined,notes:undefined});
 await fs.writeFile(path.join(f.root,'versions',B,'release.json'),legacy.signed);
 u=f.create();await u.load();await u.select();await f.release(C);assert.equal((await u.check()).kind,'available');
});
test('Python dependencies install only from the hash-pinned locks, like the container updater',async t=>{
 const {pythonInstallSteps,pythonDependencyKey,buildSource}=require('../dist/main/source-build.js');
 const steps=pythonInstallSteps();
 assert.deepEqual(steps.map(args=>args.slice(-3)),[['--require-hashes','-r','requirements.lock'],['--require-hashes','-r','requirements-native.lock'],['--no-deps','--no-build-isolation','./fluid']]);
 for(const args of steps){assert.deepEqual(args.slice(0,3),['-m','pip','install']);assert(!args.includes('requirements.txt')&&!args.includes('requirements-native.txt'));}
 // The bundled runtime is reused only when it was installed from the same locks.
 const {pythonDependencies}=await import('../scripts/runtime-contract.mjs');
 const repo=fileURLToPath(new URL('../..',import.meta.url));
 assert.equal(await pythonDependencyKey(repo),await pythonDependencies(repo));
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'shuffle-lock-test-'));t.after(()=>fs.rm(root,{recursive:true,force:true}));
 const source=path.join(root,'source');await fs.mkdir(path.join(source,'fluid'),{recursive:true});
 for(const name of ['requirements.txt','requirements-native.txt','requirements.lock','fluid/pyproject.toml'])await fs.writeFile(path.join(source,name),'x');
 const log=path.join(root,'build.log');const runtime={root,python:'python-not-run',node:'node-not-run',npm:'npm',pythonDependencies:''};
 await assert.rejects(buildSource({source,server:path.join(root,'server'),cache:path.join(root,'cache'),runtime,revision:A,log,progress(){}}),/no hash-pinned requirements-native\.lock/);
 await assert.rejects(fs.stat(log),{code:'ENOENT'},'nothing ran before the refusal');
});
test('checks and downloads report the signed release name and notes; a staged update keeps them across restarts',async t=>{
 const f=await fixture(t);await f.release(A);let u=f.create();await u.load();await u.download();await u.select();await u.healthy();
 const notes='Faster startup.\nFixes the sidebar.';
 await f.release(B,undefined,{version:'0.9.0',notes});
 assert.deepEqual(await u.check(),{kind:'available',version:B.slice(0,12),release:'0.9.0',notes});
 assert.deepEqual(await u.download(),{kind:'ready',version:B.slice(0,12),release:'0.9.0',notes});
 u=f.create();await u.load();
 assert.deepEqual(await u.check(),{kind:'ready',version:B.slice(0,12),release:'0.9.0',notes});
 assert.deepEqual(await u.releaseInfo(B),{release:'0.9.0',notes});
 assert.deepEqual(await u.releaseInfo(C),{});
 // Notes are optional: a release without them offers none.
 await fs.writeFile(path.join(f.root,'state.json'),JSON.stringify({active:B}));
 await f.release(C,undefined,{notes:undefined,version:undefined});
 const w=f.create();await w.load();assert.deepEqual(await w.check(),{kind:'available',version:C.slice(0,12)});
});

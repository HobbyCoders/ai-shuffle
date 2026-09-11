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
const { SourceUpdates, verifySourceManifest } = require('../dist/main/source-updates.js');
const { buildEnvironment, removeSourceDirectory } = require('../dist/main/source-build.js');
const extractor = fileURLToPath(new URL('./helpers/source-archive.py', import.meta.url));
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
 const options={root,publicKey,bootstrapNode:process.execPath,fetch:async url=>{requests.push(url);return new Response(url.endsWith('/manifest')?signed:bodies.get(url)||'missing',{status:url.endsWith('/manifest')||bodies.has(url)?200:404});},build:async(source,stage,revision)=>{
  builds++;if(fail)throw Error('Local build failed');
  for(const name of ['server/app/main.py','server/app/static/index.html','source/desktop/dist/main/managed-entry.js']){await fs.mkdir(path.dirname(path.join(stage,name)),{recursive:true});await fs.writeFile(path.join(stage,name),revision);}
  const electron=path.join(root,'dependencies',KEY,'electron.exe');
  for(const name of [electron,path.join(root,'runtimes',KEY,'worker/python/python.exe')]){await fs.mkdir(path.dirname(name),{recursive:true});await fs.writeFile(name,'runtime');}
  return {revision,pythonKey:null,electron,runtimeKey:KEY};
 }};
 async function release(revision,hostile) {
  const source=path.join(root,'input',revision,`ai-shuffle-${revision}`);await fs.mkdir(source,{recursive:true});await fs.writeFile(path.join(source,'source.txt'),'real archive content');
  const archive=path.join(root,revision+'.zip');execFileSync(process.env.PYTHON||'python',[extractor,'pack',path.dirname(source),archive]);
  if(hostile)execFileSync(process.env.PYTHON||'python',['-c','import sys,zipfile; z=zipfile.ZipFile(sys.argv[1],"a"); i=zipfile.ZipInfo("placeholder"); i.filename=sys.argv[2]; z.writestr(i,"unsafe"); z.close()',archive,hostile]);
  const bytes=await fs.readFile(archive);const manifest={format:2,revision,sequence:sequence++,sha256:createHash('sha256').update(bytes).digest('hex'),size:bytes.length};
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
 assert.deepEqual(await u.check((...args)=>progress.push(args)),{kind:'available',version:B.slice(0,12)});
 assert.deepEqual(f.requests,['https://hobbycoders.com/v1/desktop/manifest']);assert.equal(f.builds,builds);assert.deepEqual(progress,[]);
 assert.equal(u.pendingVersion,null);await assert.rejects(fs.stat(path.join(f.root,'staging')), {code:'ENOENT'});
 assert.deepEqual(await u.download(),{kind:'ready',version:B.slice(0,12)});
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
test('public source requests omit credentials and reject off-origin URLs and redirects',async()=>{
 const module={exports:{}};const requests=[];
 vm.runInNewContext(await fs.readFile(new URL('../dist/main/source-auth.js',import.meta.url),'utf8'),{
  exports:module.exports,module,URL,AbortSignal,fetch:async(...args)=>{requests.push(args);return new Response('ok');}
 });
 const request=module.exports.publicSourceFetch;
 await request('https://hobbycoders.com/v1/desktop/manifest');
 assert.equal(requests[0][1].credentials,'omit');assert.equal(requests[0][1].redirect,'error');assert.equal(requests[0][1].headers,undefined);
 for(const url of ['https://evil.example/v1/desktop/manifest','https://user:secret@hobbycoders.com/v1/desktop/manifest','https://hobbycoders.com/v1/desktop/manifest?token=secret'])await assert.rejects(request(url),/Invalid source/);
 assert.equal(requests.length,1);
});

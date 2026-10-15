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
import { afterRename } from './helpers/crash-rename.mjs';
const require = createRequire(import.meta.url);
const { SourceUpdates, verifySourceManifest, treeDigest, requiredLauncher } = require('../dist/main/source-updates.js');
const { buildEnvironment, removeSourceDirectory } = require('../dist/main/source-build.js');
const extractor = fileURLToPath(new URL('./helpers/source-archive.py', import.meta.url));
const repoScripts = fileURLToPath(new URL('../../scripts', import.meta.url));
// The publisher's own digest (scripts/source_archive.py), so the desktop check is tested against it, not against itself.
const publishedTree = archive => execFileSync(process.env.PYTHON||'python',['-c','import sys; sys.path.insert(0, sys.argv[1]); import source_archive as s; print(s.tree_digest(s.archive_files(sys.argv[2])))',repoScripts,archive],{encoding:'utf8'}).trim();
// Unmodified origin/main producer at 802f93bf20bb96f5f091c0fde895bf879d197a70.
// Keep a fixture so shallow CI checkouts exercise the real legacy writes too.
const legacyModule={exports:{}};
vm.runInNewContext(require('typescript').transpileModule(await fs.readFile(new URL('./helpers/legacy-source-updates.ts',import.meta.url),'utf8'),{compilerOptions:{module:require('typescript').ModuleKind.CommonJS,target:require('typescript').ScriptTarget.ES2022,esModuleInterop:true}}).outputText,{module:legacyModule,exports:legacyModule.exports,require:createRequire(new URL('../dist/main/source-updates.js',import.meta.url)),process,Buffer,console,Response,AbortSignal,setTimeout,clearTimeout});
// Keep the legacy state producer unchanged, but resolve its test runtime with
// the current application's platform-aware paths (the snapshot is Windows-only).
class LegacyUpdates extends legacyModule.exports.SourceUpdates {
 application(revision){return SourceUpdates.prototype.application.call(this,revision);}
}
const A='a'.repeat(40), B='b'.repeat(40), C='c'.repeat(40), KEY='d'.repeat(64);
async function fixture(t) {
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'shuffle-source-test-'));t.after(()=>fs.rm(root,{recursive:true,force:true}));
 const {privateKey,publicKey:key}=generateKeyPairSync('ed25519');const publicKey=key.export({type:'spki',format:'pem'});
 const bodies=new Map();const requests=[];let signed;let sequence=100;let builds=0;let fail=false;
 const options={root,publicKey,bootstrapNode:process.execPath,fetch:async url=>{requests.push(url);const feed=url.endsWith('/manifest')||url.endsWith('/manifest/nightly');return new Response(feed?signed:bodies.get(url)||'missing',{status:feed||bodies.has(url)?200:404});},build:async(source,stage,revision)=>{
  builds++;if(fail)throw Error('Local build failed');
  for(const name of ['server/app/main.py','server/app/static/index.html','source/desktop/dist/main/managed-entry.js']){await fs.mkdir(path.dirname(path.join(stage,name)),{recursive:true});await fs.writeFile(path.join(stage,name),revision);}
  const electron=path.join(root,'dependencies',KEY,process.platform==='win32'?'electron.exe':'electron');
  for(const name of [electron,path.join(root,'runtimes',KEY,process.platform==='win32'?'worker/python/python.exe':'worker/python/bin/python3')]){await fs.mkdir(path.dirname(name),{recursive:true});await fs.writeFile(name,'runtime');}
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
async function installedLauncher(f, rejectRevision = null, {baseline={revision:A,sequence:50},answer=1} = {}) {
 let manager;const events=[];const dialogs=[];const module={exports:{}};
 class InstalledUpdates extends SourceUpdates{constructor(options){super({...f.options,baseline:options.baseline});manager=this;}}
 const app={requestSingleInstanceLock:()=>true,getPath:()=>f.root,getName:()=> 'test',getAppPath:()=>f.root,whenReady:async()=>{},quit:()=>events.push('quit'),commandLine:{getSwitchValue:()=>''}};
 class InstallerRequired extends Error{}
 vm.runInNewContext(await fs.readFile(new URL('../dist/main/installed-entry.js',import.meta.url),'utf8'),{module,exports:module.exports,process:{platform:'linux',env:{},versions:{electron:'44.2.0'},resourcesPath:f.root,argv:[]},require:id=>{
  if(id==='electron')return {app,dialog:{showMessageBox:async options=>{events.push(options.title);dialogs.push(options);return {response:answer};}}};
  if(id==='node:path')return path;
  if(id==='node:fs')return {promises:{...fs,readFile:async file=>file.endsWith('desktop-build.json')?JSON.stringify(baseline):file.endsWith('application-update-key.pem')?f.options.publicKey:fs.readFile(file,'utf8')}};
  if(id==='./source-updates')return {SourceUpdates:InstalledUpdates};
  if(id==='./postgres')return {resolvePostgresBinaries:()=>({}),PostgresSupervisor:class {updateRestorePoint(){return {validate:async()=>{},snapshot:async()=>events.push('snapshot'),restore:async()=>events.push('restore'),discard:async()=>events.push('discard')};}}};
  if(id==='./worker')return {quiesceRecordedWorker:async()=>events.push('quiesce')};
  if(id==='./launcher-swap')return require('../dist/main/launcher-swap.js');
  if(id==='./installed-host')return {InstallerRequired,installedExecutable:()=>'/test/AI Shuffle.AppImage',saveInstalledHost:async()=>{},requireInstalledElectron:async()=>{}};
  if(id==='./index'){events.push('bundled');return {};}
  if(id.endsWith(path.join('dist','main','managed-entry.js'))){
   if(rejectRevision && id.includes(rejectRevision))return createRequire(import.meta.url)(id);
   return {startManagedSource:async(_root,revision)=>events.push(revision)};
  }
  throw Error('Unexpected module '+id);
 }});
 return {start:()=>module.exports.startInstalled(),events,dialogs,get manager(){return manager;}};
}
async function rollbackFixture(t, { baseline = false } = {}) {
 const f = await fixture(t);
 if (baseline) f.options.baseline = { revision: A, sequence: 50 };
 const u = f.create(); await u.load();
 if (!baseline) { await f.release(A); await u.download(); await u.select(); await u.databaseStarting(); await u.healthy(); }
 else await u.databaseStarting();
 await f.release(B); await u.download();
 const phases = [], calls = [];
 const journal = async () => JSON.parse(await fs.readFile(path.join(f.root, 'restore-point.json'), 'utf8'));
 const db = {
  validate: async () => {},
  snapshot: async () => { phases.push((await journal()).phase); calls.push('snapshot'); },
  restore: async () => { phases.push((await journal()).phase); calls.push('restore'); },
  discard: async () => { phases.push((await journal()).phase); calls.push('discard'); },
 };
 return { f, u, db, calls, phases, journal, factory: async previous => { assert.equal(previous, A); return db; } };
}
test('restore journal: signed download to snapshot to migrated crash to previous source and floor', async t => {
 const { f, u, db, calls, phases, journal, factory } = await rollbackFixture(t);
 await u.prepareRollback(factory); assert.equal((await journal()).phase, 'ready');
 await u.select(); const child=f.create(); await child.attach(B); await child.requireRestorePoint(); await child.databaseStarting();
 const restart=f.create(); await restart.load(); assert.equal(restart.blocked,true);
 await restart.recoverRollback(factory);
 assert.deepEqual(calls,['snapshot','restore','discard']); assert.deepEqual(phases,['preparing','restoring','committed']);
 assert.equal((await restart.select()).revision,A); assert.equal(restart.hasMigrationRisk,false);
 assert.equal(JSON.parse(await fs.readFile(path.join(f.root,'database-floor.json'))).revision,A);
 assert.match(await restart.rollbackNotice(), /0.9.2.*went back to 0.9.1/);
 await assert.rejects(restart.check(), /This revision failed to start/);
 const again=f.create(); await again.load(); assert.equal((await again.select()).revision,A);
});
test('restore journal: no unprotected first database start, including an older-launcher bundled handoff',async t=>{
 for(const baseline of [false,true]){
  const {f,u}=await rollbackFixture(t,{baseline});await u.select();
  const child=f.create();await child.attach(B);
  await assert.rejects(child.requireRestorePoint(),/No verified database restore point/);
  assert.equal(JSON.parse(await fs.readFile(path.join(f.root,'database-floor.json'))).revision,A);
 }
});
test('restore journal: legacy same-build retry retains its migration fence without inventing an old restore point',async t=>{
 const {f,u}=await rollbackFixture(t);await u.select();await u.databaseStarting();await u.failed();
 await u.retryFailed();await u.select();const child=f.create();await child.attach(B);
 await child.requireRestorePoint();assert.equal(child.hasMigrationRisk,true);
});
test('restore journal: snapshot errors never select the update and retain a clear failure reason', async t => {
 const { f, u, db, calls, factory } = await rollbackFixture(t);
 db.snapshot=async()=>{throw Error('disk full');};
 await u.prepareRollback(factory);
 assert.equal((await u.select()).revision,A); assert.deepEqual(calls,['discard']);
 assert.match(await u.rollbackNotice(),/restore point failed: disk full/);
 await assert.rejects(u.check(), /This revision failed to start/);
});
test('restore journal: compatibility rejection discards an unused copy without blacklisting the staged update',async t=>{
 const {f,u,factory,calls}=await rollbackFixture(t);await u.prepareRollback(factory);
 await u.cancelPreparedRollback(factory);u.stageDownloads();await u.cancelInstall();
 const restart=f.create();await restart.load();assert.equal(await restart.recoverRollback(factory),false);
 assert.deepEqual(calls,['snapshot','discard']);assert.equal((await restart.select()).revision,A);
 assert.equal((await restart.check()).kind,'ready');
});
test('restore journal: a copy cannot be discarded as a preflight rejection after selection',async t=>{
 const {u,factory}=await rollbackFixture(t);await u.prepareRollback(factory);await u.select();
 await assert.rejects(u.cancelPreparedRollback(factory),/after the update started/);
});
test('restore journal: interrupted snapshot cancels before first start, bundled baseline remains selectable', async t => {
 const { f, u, db, factory } = await rollbackFixture(t,{baseline:true});
 let entered; const started=new Promise(resolve=>entered=resolve);
 db.snapshot=async()=>{entered(); await new Promise(()=>{});};
 void u.prepareRollback(factory); await started; // process died with the preparing journal durable
 const restart=f.create(); await restart.load(); await restart.recoverRollback(factory);
 assert.equal(restart.version,A); assert.equal(restart.hasSourceState,false); assert.equal(restart.hasMigrationRisk,false);
 assert.match(await restart.rollbackNotice(),/interrupted/);
});
test('restore journal: ready before selection can resume; a crash during handoff restores even before databaseStarting', async t => {
 const { f, u, calls, factory } = await rollbackFixture(t);
 await u.prepareRollback(factory);
 const restart=f.create(); await restart.load(); assert.equal(await restart.recoverRollback(factory),false);
 await restart.prepareRollback(factory); assert.deepEqual(calls,['snapshot']);
 await restart.select();
 const crash=f.create(); await crash.load(); await crash.recoverRollback(factory);
 assert.equal((await crash.select()).revision,A); assert.deepEqual(calls,['snapshot','restore','discard']);
});
for(const boundary of ['restoring','restored','floor','state','committed']) test(`restore journal resumes power loss at ${boundary} without lowering the fence early`,async t=>{
 const { f,u,db,factory,journal }=await rollbackFixture(t);
 await u.prepareRollback(factory); await u.select(); await u.databaseStarting(); await u.failed();
 let crashed=false;
 const undo=afterRename(async(from,to)=>{
  const phase=to.endsWith('restore-point.json') ? JSON.parse(await fs.readFile(to,'utf8')).phase : null;
  const match=phase===boundary || (boundary==='floor'&&to.endsWith('database-floor.json')) || (boundary==='state'&&to.endsWith('state.json'));
  if(match&&!crashed){crashed=true;throw Error('power loss');}
 });
 try { await assert.rejects(u.recoverRollback(factory),/power loss/); } finally { undo(); }
 assert.equal(crashed,true);
 if(boundary==='restoring') assert.equal(JSON.parse(await fs.readFile(path.join(f.root,'database-floor.json'))).revision,B);
 const restart=f.create(); await restart.load(); await restart.recoverRollback(factory);
 assert.equal((await restart.select()).revision,A); assert.equal(restart.hasMigrationRisk,false);
 assert.equal(JSON.parse(await fs.readFile(path.join(f.root,'database-floor.json'))).revision,A);
});
test('restore journal: restore failure retains snapshot and fence; explicit Go back retries it',async t=>{
 const {f,u,db,factory,journal}=await rollbackFixture(t);
 await u.prepareRollback(factory); await u.select(); await u.databaseStarting(); await u.failed();
 const restore=db.restore; db.restore=async()=>{throw Error('file locked');};
 await assert.rejects(u.recoverRollback(factory),/file locked/);
 assert.equal((await journal()).phase,'restoring'); assert.equal(u.blocked,true);
 await assert.rejects(u.select(),/healthy/); assert.equal(await u.rollbackVersion(),'0.9.1');
 db.restore=restore; await u.recoverRollback(factory,true); assert.equal((await u.select()).revision,A);
});
test('restore journal: healthy promotion and cleanup interruption keep the new build, never restore old data',async t=>{
 const {f,u,db,factory,calls,journal}=await rollbackFixture(t);
 await u.prepareRollback(factory); await u.select(); await u.databaseStarting(); await u.healthy();
 db.discard=async()=>{throw Error('file locked');};
 await assert.rejects(u.recoverRollback(factory),/file locked/); assert.equal((await journal()).phase,'committed');
 db.discard=async()=>calls.push('discard');
 const restart=f.create(); await restart.load(); await restart.recoverRollback(factory);
 assert.equal((await restart.select()).revision,B); assert.deepEqual(calls,['snapshot','discard']);
});
test('restore journal: a corrupt journal or missing previous build never clears the migration fence',async t=>{
 const {f,u,factory}=await rollbackFixture(t);
 await u.prepareRollback(factory); await u.select(); await u.databaseStarting(); await u.failed();
 await fs.rm(path.join(f.root,'versions',A,'build.json'));
 await assert.rejects(u.recoverRollback(factory)); assert.equal(u.hasMigrationRisk,true);
 await fs.writeFile(path.join(f.root,'restore-point.json'),'{}');
 await assert.rejects(u.recoverRollback(factory),/Invalid database restore journal/); assert.equal(u.hasMigrationRisk,true);
});
test('restore journal: bundled-baseline first-start failure returns to the bundled code, never a fake source directory',async t=>{
 const {f,u,factory}=await rollbackFixture(t,{baseline:true});
 await u.prepareRollback(factory); await u.select(); await u.databaseStarting();
 const restart=f.create(); await restart.load(); await restart.recoverRollback(factory);
 assert.equal(restart.version,A); assert.equal(restart.hasSourceState,false); assert.equal(restart.hasMigrationRisk,false);
 const launcher=await installedLauncher(f); await launcher.start(); assert.ok(launcher.events.includes('bundled')); assert.ok(!launcher.events.includes(B));
});
test('Linux startup drops only incomplete unselected downloads, validates healthy code and permits a real re-download',async t=>{
 for(const active of [false,true])for(const missing of ['build.json','release.json']){
  const f=await fixture(t);const seed=f.create();await seed.load();
  if(active){await f.release(A);await seed.download();await seed.select();await seed.healthy();}
  seed.stageDownloads();await f.release(B);await seed.download();await fs.unlink(path.join(f.root,'versions',B,missing));
  const launch=await installedLauncher(f);await launch.start();const {manager,events}=launch;assert.deepEqual(events,[active?A:'bundled']);assert.equal(manager.pendingVersion,null);
  assert.equal((await manager.check()).kind,'available');manager.stageDownloads();await manager.download();assert.equal((await manager.application(B)).revision,B);
 }
});
test('discardStaged never forgets active, previous, pending or possibly migrated failed code',async t=>{
 const f=await fixture(t);
 for(const protectedState of [{active:B},{previous:B},{pending:B},{failed:[B]},{booting:B},{blocked:true}]){
  await fs.mkdir(path.join(f.root,'versions'),{recursive:true});await fs.writeFile(path.join(f.root,'state.json'),JSON.stringify({staged:B,...protectedState}));
  const loader=f.create();await loader.load();assert.equal(await loader.discardStaged(B),false);assert.equal(loader.pendingVersion,B);
 }
});
test('Linux retry API rejection never forgets a migrated revision or launches older source/bundled code',async t=>{
 const f=await fixture(t);const u=f.create();await u.load();await f.release(A);await u.download();await u.select();await u.healthy();
 u.stageDownloads();const {manifest}=await f.release(B);await u.download();await u.prepareInstall();await u.select();await u.attach(B);await u.databaseStarting();await u.failed();
 assert.equal(await u.retryFailed(),true);
 await fs.writeFile(path.join(f.root,'versions',B,'source/desktop/dist/main/managed-entry.js'),'');
 const launch=await installedLauncher(f,B);await assert.rejects(launch.start(),/may have been migrated/);
 assert.deepEqual(launch.events,[],'neither older A nor bundled code ran');
 const state=JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8'));
 assert.deepEqual(state.mayHaveMigrated,{[B]:manifest.sequence});assert.equal(state.active,A);assert.equal(state.staged,B);assert.equal(state.blocked,true);
 assert.equal(await launch.manager.discardStaged(B),false);await assert.rejects(launch.manager.select(),/No healthy/);
});
test('migration fence persists through the transition table: no older selection, attachment or discard',async t=>{
 const f=await fixture(t);let u=f.create();await u.load();await f.release(A);await u.download();await u.select();await u.healthy();
 u.stageDownloads();const {manifest}=await f.release(B);await u.download();await u.prepareInstall();await u.select();await u.attach(B);
 const rows=[
  ['database starting',async()=>u.databaseStarting()],
  ['failed boot',async()=>u.failed()],
  ['retry',async()=>assert.equal(await u.retryFailed(),true)],
  ['cancel retry',async()=>assert.rejects(u.cancelInstall(),/may have been migrated/)],
  ['discard refused',async()=>assert.equal(await u.discardStaged(B),false)],
  ['restart',async()=>{u=f.create();await u.load();}],
  ['retry before database',async()=>{await u.retryFailed();await u.select();await u.attach(B);}],
  ['defer retry boot',async()=>u.deferBoot()],
  ['disabled updates on restart',async()=>{await fs.writeFile(path.join(f.root,'allow-updates.json'),JSON.stringify({allow_updates:false}));u=f.create();await u.load();}],
  ['retry then pre-database crash',async()=>{await fs.writeFile(path.join(f.root,'allow-updates.json'),JSON.stringify({allow_updates:true}));await u.retryFailed();await u.select();await u.attach(B);u=f.create();await u.load();}]
 ];
 for(const [name,step] of rows)await t.test(name,async()=>{
  await step();assert.equal(u.hasMigrationRisk,true);
  const readState=async()=>JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8'));
  assert.deepEqual((await readState()).mayHaveMigrated,{[B]:manifest.sequence});
  const stopped=Error('preflight probe: do not actually start another boot');
  await assert.rejects(u.select(async application=>{assert.equal(application.revision,B,'never preflight older A');throw stopped;}),error=>error===stopped||/No healthy|may have been migrated/.test(error.message));
  await assert.rejects(f.create().attach(A),/may have been migrated/);
  assert.deepEqual((await readState()).mayHaveMigrated,{[B]:manifest.sequence});assert.equal(await u.discardStaged(B),false);
 });
 // Recovery by the same revision is allowed; only its healthy transition clears the fence.
 await u.retryFailed();await u.select();await u.attach(B);await u.databaseStarting();await u.healthy();
 assert.equal(u.hasMigrationRisk,false);assert.equal(u.version,B);assert.equal(JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8')).mayHaveMigrated,undefined);
});
test('multiple migration fences require same/newer code and only healthy newer code or a newer installer clears them',async t=>{
 for(const recovery of ['healthy revision','newer installer']){
  const f=await fixture(t);let u=f.create();await u.load();const a=await f.release(A);await u.download();await u.select();await u.attach(A);await u.databaseStarting();await u.failed();
  const b=await f.release(B);await u.download();await u.select();await u.attach(B);await u.databaseStarting();await u.failed();
  const fences={[A]:a.manifest.sequence,[B]:b.manifest.sequence};assert.deepEqual(JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8')).mayHaveMigrated,fences);
  if(recovery==='healthy revision'){
   await f.release(C);await u.download();await u.select();await u.attach(C);await u.databaseStarting();assert.equal(u.hasMigrationRisk,true);await u.healthy();assert.equal(u.version,C);
  }else{
   // Losing B's files must not permit retrying older failed A, nor an equal/older installer.
   await fs.unlink(path.join(f.root,'versions',B,'release.json'));assert.equal(await u.retryFailed(),false);
   const equal=new SourceUpdates({...f.options,baseline:{revision:C,sequence:b.manifest.sequence}});await equal.load();await equal.useNewerBaseline();assert.equal(equal.hasMigrationRisk,true);await assert.rejects(equal.select(),/No healthy/);
   u=new SourceUpdates({...f.options,baseline:{revision:C,sequence:b.manifest.sequence+1}});await u.load();await u.useNewerBaseline();assert.equal(u.hasSourceState,false);assert.equal(u.version,C);
  }
  assert.equal(u.hasMigrationRisk,false);assert.equal(JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8')).mayHaveMigrated,undefined);
 }
});
test('partially upgraded legacy failures are fenced on load and attach, including unreadable revision metadata',async t=>{
 const f=await fixture(t);let u=f.create();await u.load();const a=await f.release(A);await u.download();await u.select();await u.healthy();
 const b=await f.release(B);await u.download();await u.select();await u.attach(B);await u.databaseStarting();await u.failed();
 for(const unreadable of [false,true])for(const entry of ['load','attach']){
  const state={active:A,blocked:true,failed:[A,B],mayHaveMigrated:{[A]:a.manifest.sequence}};
  await fs.writeFile(path.join(f.root,'state.json'),JSON.stringify(state));
  if(unreadable)await fs.writeFile(path.join(f.root,'versions',B,'release.json'),'{}');else await fs.writeFile(path.join(f.root,'versions',B,'release.json'),b.signed);
  u=f.create();if(entry==='attach')await assert.rejects(u.attach(A),/may have been migrated/);else await u.load();
  const persisted=JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8'));
  assert.deepEqual(persisted.mayHaveMigrated,{[A]:a.manifest.sequence,[B]:unreadable?null:b.manifest.sequence});
  assert.equal(await u.retryFailed(),!unreadable,'an unreadable newer marked B never permits older A');
 }
});
// Faithful snapshots of origin/main's producer (before migration fences):
// healthy A -> B databaseStarting -> failed -> retryFailed saves only these
// two pointers; cancelInstall replaces pending with staged, and attach adds
// booting/waiting. In particular, retryFailed deletes both failed and blocked.
const legacyRetriedState = () => ({active:A,pending:B});
async function migratedPair(t) {
 const f=await fixture(t);const u=f.create();await u.load();await f.release(A);await u.download();await u.select();await u.attach(A);await u.databaseStarting();await u.healthy();
 u.stageDownloads();const b=await f.release(B);await u.download();await u.prepareInstall();await u.select();await u.attach(B);await u.databaseStarting();await u.failed();
 return {f,b};
}
async function legacyFailedPair(t,{schemaMarker=false,retry=true}={}){
 const f=await fixture(t);let legacy=new LegacyUpdates(f.options);await legacy.load();await f.release(A);await legacy.download();await legacy.select();await legacy.attach(A);await legacy.databaseStarting();await legacy.healthy();
 if(schemaMarker){const launch=await installedLauncher(f);await launch.start();assert.deepEqual(launch.events,[A]);assert.equal(JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8')).schemaVersion,1);}
 legacy=new LegacyUpdates(f.options);await legacy.load();const b=await f.release(B);await legacy.download();
 if(schemaMarker){const prepared=JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8'));assert.equal(prepared.schemaVersion,1);assert.equal(prepared.mayHaveMigrated,undefined);const launch=await installedLauncher(f);await launch.start();assert.deepEqual(launch.events,[B]);}else await legacy.select();
 legacy=new LegacyUpdates(f.options);await legacy.attach(B);await legacy.databaseStarting();await legacy.failed();if(retry)assert.equal(await legacy.retryFailed(),true);
 return {f,b,legacy};
}
test('real main producer cannot use a sticky schema marker to roll back a migrated retry',async t=>{
 for(const action of ['PR cancel','legacy cancel','disabled restart','attach A'])await t.test(action,async t=>{
  const {f,b,legacy}=await legacyFailedPair(t,{schemaMarker:true});const before=JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8'));
  assert.equal(before.schemaVersion,1);assert.equal(before.pending,B);
  if(action==='legacy cancel'){legacy.stageDownloads();await legacy.cancelInstall();}
  if(action==='disabled restart')await fs.writeFile(path.join(f.root,'allow-updates.json'),JSON.stringify({allow_updates:false}));
  if(action==='attach A')await assert.rejects(f.create().attach(A),/may have been migrated/);
  const u=f.create();await u.load();assert.equal(u.hasMigrationRisk,true);assert.equal(u.recoveryRequired,false);
  if(action==='PR cancel'){u.stageDownloads();await assert.rejects(u.cancelInstall(),/may have been migrated/);}
  assert.deepEqual(JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8')).mayHaveMigrated,{[B]:b.manifest.sequence});
  await assert.rejects(f.create().attach(A),/may have been migrated/);assert.equal(await u.discardStaged(B),false);
  if(action!=='disabled restart')await u.prepareInstall();assert.equal((await u.select()).revision,B);await u.attach(B);await u.databaseStarting();await u.healthy();
  const healthy=f.create();await healthy.load();assert.equal(healthy.hasMigrationRisk,false);assert.equal((await healthy.select()).revision,B);
 });
});
test('real healthy main producer remains unaffected by write-provenance tracking',async t=>{
 const f=await fixture(t);const legacy=new LegacyUpdates(f.options);await legacy.load();await f.release(A);await legacy.download();await legacy.select();await legacy.attach(A);await legacy.databaseStarting();await legacy.healthy();
 for(let i=0;i<2;i++){const launch=await installedLauncher(f);await launch.start();assert.deepEqual(launch.events,[A]);assert.equal(launch.manager.hasMigrationRisk,false);await new LegacyUpdates(f.options).attach(A);}
});
test('real main missing retry metadata cannot recover below any known healthy or migration floor',async t=>{
 for(const evidence of ['healthy manifest','migration entry','database marker'])for(const sequence of (evidence==='healthy manifest'?[50,100]:[50,100,150]))await t.test(`${evidence}, installer=${sequence}`,async t=>{
  const {f}=await legacyFailedPair(t);await fs.rm(path.join(f.root,'versions',B),{recursive:true});
  if(evidence==='migration entry'){const file=path.join(f.root,'state.json');const state=JSON.parse(await fs.readFile(file,'utf8'));state.mayHaveMigrated={[C]:150};await fs.writeFile(file,JSON.stringify(state));}
  if(evidence==='database marker')await fs.writeFile(path.join(f.root,'database-floor.json'),JSON.stringify({schemaVersion:1,revision:C,sequence:150}));
  const baseline={revision:'d'.repeat(40),sequence};
  const launch=await installedLauncher(f,null,{baseline,answer:2});await launch.start();
  assert.deepEqual(launch.events,['AI Shuffle needs an installer','quit']);assert.equal(launch.manager.canConfirmBaselineRecovery,false);
  assert(!launch.dialogs[0].buttons.includes('Use This Installer'));
  for(const consent of [false,true]){await launch.manager.useNewerBaseline(consent);assert.equal(launch.manager.recoveryRequired,true);assert.equal(launch.manager.hasSourceState,true);}
  const state=JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8'));assert.equal(state.active,A);assert.equal(state.mayHaveMigrated[B],null);
  if(evidence==='database marker')assert.equal(JSON.parse(await fs.readFile(path.join(f.root,'database-floor.json'),'utf8')).sequence,150);
  else await assert.rejects(fs.readFile(path.join(f.root,'database-floor.json')),/ENOENT/);
 });
});
test('real main healthy completion reconciles the entry fence without a permanent retry warning',async t=>{
 for(const retry of [false,true])for(const marker of [false,true])await t.test(`retry=${retry}, marker=${marker}`,async t=>{
  const f=await fixture(t);let legacy=new LegacyUpdates(f.options);await legacy.load();await f.release(A);await legacy.download();await legacy.select();await legacy.attach(A);await legacy.databaseStarting();await legacy.healthy();
  if(marker){const current=f.create();await current.attach(A);await current.databaseStarting();}
  legacy=new LegacyUpdates(f.options);await legacy.load();const b=await f.release(B);await legacy.download();
  if(retry){await legacy.select();await legacy.attach(B);await legacy.databaseStarting();await legacy.failed();assert.equal(await legacy.retryFailed(),true);}
  const selected=await installedLauncher(f);await selected.start();assert.deepEqual(selected.events,[B]);
  assert.equal(JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8')).mayHaveMigrated[B],b.manifest.sequence);
  legacy=new LegacyUpdates(f.options);await legacy.attach(B);await legacy.databaseStarting();await legacy.healthy();
  const completed=JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8'));assert.equal(completed.active,B);assert.equal(completed.pending,undefined);assert.equal(completed.booting,undefined);assert.equal(completed.blocked,undefined);assert.equal(completed.mayHaveMigrated[B],b.manifest.sequence);
  for(let i=0;i<2;i++){
   const launch=await installedLauncher(f);await launch.start();assert.deepEqual(launch.events,[B]);assert.equal(launch.manager.hasMigrationRisk,false);assert.equal(launch.manager.blocked,false);assert.equal(launch.dialogs.length,0);
   await assert.rejects(f.create().attach(A),/Unexpected desktop launch revision/);
   legacy=new LegacyUpdates(f.options);await legacy.attach(B);await legacy.databaseStarting();await legacy.healthy();
  }
  const floor=JSON.parse(await fs.readFile(path.join(f.root,'database-floor.json'),'utf8'));assert.equal(floor.revision,B);assert.equal(floor.sequence,b.manifest.sequence);assert.equal(floor.uncertain,undefined);
 });
});
test('real main missing failed or retried metadata offers only owner-confirmed installer recovery',async t=>{
 for(const retry of [false,true])for(const sequence of [200,201])await t.test(`retry=${retry}, installer=${sequence}`,async t=>{
  const {f}=await legacyFailedPair(t,{retry});await fs.rm(path.join(f.root,'versions',B),{recursive:true});
  const baseline={revision:C,sequence};
  for(let i=0;i<2;i++){const declined=await installedLauncher(f,null,{baseline});await declined.start();assert.deepEqual(declined.events,['AI Shuffle needs an installer','quit']);assert.equal(declined.manager.recoveryRequired,true);assert.equal(declined.manager.canConfirmBaselineRecovery,true);assert.equal(await declined.manager.retryFailed(),false);}
  const accepted=await installedLauncher(f,null,{baseline,answer:2});await accepted.start();assert.deepEqual(accepted.events,['AI Shuffle needs an installer','bundled']);assert.equal(accepted.manager.hasSourceState,false);
  const again=await installedLauncher(f,null,{baseline});await again.start();assert.deepEqual(again.events,['bundled']);assert.equal(again.manager.recoveryRequired,false);
 });
});
test('legacy main retry snapshot restores the fence before cancel and permits healthy recovery without an installer',async t=>{
 const {f,b}=await migratedPair(t);await fs.writeFile(path.join(f.root,'state.json'),JSON.stringify(legacyRetriedState()));
 let u=f.create();await u.load();u.stageDownloads();await assert.rejects(u.cancelInstall(),/may have been migrated/);
 assert.deepEqual(JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8')).mayHaveMigrated,{[B]:b.manifest.sequence});
 await assert.rejects(u.select(),/No healthy/);await assert.rejects(f.create().attach(A),/may have been migrated/);assert.equal(await u.discardStaged(B),false);
 u=f.create();await u.load();await u.prepareInstall();assert.equal((await u.select()).revision,B);await u.attach(B);await u.databaseStarting();await u.healthy();
 const healthy=f.create();await healthy.load();assert.equal(healthy.hasMigrationRisk,false);assert.equal((await healthy.select()).revision,B);
});
test('disabled updates cannot discard a legacy retried revision and installed Linux launches B, never older A',async t=>{
 const {f,b}=await migratedPair(t);await fs.writeFile(path.join(f.root,'state.json'),JSON.stringify(legacyRetriedState()));await fs.writeFile(path.join(f.root,'allow-updates.json'),JSON.stringify({allow_updates:false}));
 const launch=await installedLauncher(f);await launch.start();assert.deepEqual(launch.events,[B]);assert.equal(launch.manager.hasMigrationRisk,true);
 assert.deepEqual(JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8')).mayHaveMigrated,{[B]:b.manifest.sequence});
 await assert.rejects(f.create().attach(A),/may have been migrated/);await launch.manager.attach(B);await launch.manager.databaseStarting();await launch.manager.healthy();assert.equal(launch.manager.version,B);assert.equal(launch.manager.hasMigrationRisk,false);
});
test('legacy staged and waiting snapshots fence prior attempts; healthy legacy installs remain unaffected',async t=>{
 const {f,b}=await migratedPair(t);
 for(const state of [{active:A,staged:B},{...legacyRetriedState(),booting:B,waiting:B}]){
  await fs.writeFile(path.join(f.root,'state.json'),JSON.stringify(state));const u=f.create();await u.load();assert.equal(u.hasMigrationRisk,true);
  assert.deepEqual(JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8')).mayHaveMigrated,{[B]:b.manifest.sequence});await assert.rejects(u.select(),/No healthy/);await assert.rejects(f.create().attach(A),/may have been migrated/);
  await u.prepareInstall();await u.select();await u.attach(B);await u.databaseStarting();await u.healthy();assert.equal(u.hasMigrationRisk,false);
 }
 const legacy=await fixture(t);const seed=legacy.create();await seed.load();await legacy.release(A);await seed.download();await seed.select();await seed.healthy();await fs.writeFile(path.join(legacy.root,'state.json'),JSON.stringify({active:A}));const healthy=legacy.create();await healthy.load();assert.equal(healthy.hasMigrationRisk,false);assert.equal((await healthy.select()).revision,A);
});
test('legacy pending with unverifiable signed provenance stays fenced under disabled update policy',async t=>{
 const {f}=await migratedPair(t);await fs.writeFile(path.join(f.root,'state.json'),JSON.stringify(legacyRetriedState()));await fs.unlink(path.join(f.root,'versions',B,'release.json'));await fs.writeFile(path.join(f.root,'allow-updates.json'),JSON.stringify({allow_updates:false}));
 const u=f.create();await u.load();assert.deepEqual(JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8')).mayHaveMigrated,{[B]:null});await assert.rejects(u.select());await assert.rejects(f.create().attach(A),/may have been migrated/);await u.useNewerBaseline();assert.equal(u.hasMigrationRisk,true);
});
test('damaged and unknown-version state is preserved and recovers using the independent database floor',async t=>{
 for(const text of ['[]','{','{"active":17}',JSON.stringify({schemaVersion:999,active:A})]){
  const {f}=await migratedPair(t);
  await fs.writeFile(path.join(f.root,'state.json'),text);const before=new Set(await fs.readdir(f.root));const u=f.create();await u.load();
  const backup=(await fs.readdir(f.root)).find(name=>name.startsWith('state.json.corrupt-')&&!before.has(name));assert(backup);assert.equal(await fs.readFile(path.join(f.root,backup),'utf8'),text);
  assert.equal(u.recoveryRequired,false);assert.equal(u.hasMigrationRisk,true);await assert.rejects(f.create().attach(A),/Unexpected desktop launch revision/);
  const launch=await installedLauncher(f);await launch.start();assert.deepEqual(launch.events,[B]);await launch.manager.attach(B);await launch.manager.databaseStarting();await launch.manager.healthy();assert.equal(launch.manager.hasMigrationRisk,false);
 }
});
test('missing retried B plus corrupted state never delegates to remaining A, including marker-less legacy installs',async t=>{
 for(const legacy of [false,true])for(const text of ['[]','{','{"active":17}','{"schemaVersion":999}']){
  const {f}=await migratedPair(t);const retry=f.create();await retry.load();assert.equal(await retry.retryFailed(),true);
  // Legacy main writes no independent marker, but can have this exact retry state.
  if(legacy)await fs.unlink(path.join(f.root,'database-floor.json'));
  await fs.rm(path.join(f.root,'versions',B),{recursive:true});await fs.writeFile(path.join(f.root,'state.json'),text);
  const launch=await installedLauncher(f);await launch.start();assert.deepEqual(launch.events,['AI Shuffle needs an installer','quit']);
  assert.equal(launch.manager.recoveryRequired,true);await assert.rejects(launch.manager.select());
  const backup=(await fs.readdir(f.root)).find(name=>name.startsWith('state.json.corrupt-'));assert.equal(await fs.readFile(path.join(f.root,backup),'utf8'),text);
 }
});
test('selecting a different producer invalidates a stale floor until databaseStarting records the new one',async t=>{
 const f=await fixture(t);const u=f.create();await u.load();await f.release(A);await u.download();await u.select();await u.databaseStarting();await u.healthy();
 await f.release(B);await u.download();await u.select();
 const marker=JSON.parse(await fs.readFile(path.join(f.root,'database-floor.json'),'utf8'));assert.equal(marker.revision,A);assert.equal(marker.uncertain,true);
 // An older delegated producer can migrate B without rewriting the marker.
 await fs.rm(path.join(f.root,'versions',B),{recursive:true});await fs.writeFile(path.join(f.root,'state.json'),'[]');
 const launch=await installedLauncher(f);await launch.start();assert.deepEqual(launch.events,['AI Shuffle needs an installer','quit']);
});
test('a marker written before the database-state save keeps the floor across a crash',async t=>{
 const f=await fixture(t);const u=f.create();await u.load();await f.release(A);await u.download();await u.select();await u.databaseStarting();await u.healthy();
 await f.release(B);await u.download();await u.select();await u.attach(B);
 const waiting=await fs.readFile(path.join(f.root,'state.json'),'utf8');await u.databaseStarting();await fs.writeFile(path.join(f.root,'state.json'),waiting);
 const restarted=f.create();await restarted.load();assert.equal(restarted.hasMigrationRisk,true);await assert.rejects(restarted.select());await assert.rejects(f.create().attach(A),/may have been migrated/);
});
test('trusted installer at or above the durable floor repairs missing-code recovery without losing quarantine',async t=>{
 for(const delta of [-1,0,1]){
  const {f,b}=await migratedPair(t);await fs.rm(path.join(f.root,'versions',B),{recursive:true});await fs.writeFile(path.join(f.root,'state.json'),'[]');
  const old=await installedLauncher(f);await old.start();assert(old.events.includes('quit'));
  const baseline={revision:C,sequence:b.manifest.sequence+delta};const launch=await installedLauncher(f,null,{baseline,answer:2});await launch.start();
  if(delta<0){assert.deepEqual(launch.events,['AI Shuffle needs an installer','quit']);continue;}
  assert.deepEqual(launch.events,['bundled']);assert.equal(launch.manager.recoveryRequired,false);assert.equal(launch.manager.hasSourceState,false);
  const again=await installedLauncher(f,null,{baseline});await again.start();assert.deepEqual(again.events,['bundled']);
  const backup=(await fs.readdir(f.root)).find(name=>name.startsWith('state.json.corrupt-'));assert.equal(await fs.readFile(path.join(f.root,backup),'utf8'),'[]');
 }
});
test('installer with no provable floor needs explicit recovery-dialog consent, then stays repaired on relaunch',async t=>{
 const f=await fixture(t);await fs.mkdir(path.join(f.root,'versions'),{recursive:true});await fs.writeFile(path.join(f.root,'state.json'),'{');
 const old=await installedLauncher(f);await old.start();assert.deepEqual(old.events,['AI Shuffle needs an installer','quit']);
 const baseline={revision:C,sequence:200};const declined=await installedLauncher(f,null,{baseline});await declined.start();assert.deepEqual(declined.events,['AI Shuffle needs an installer','quit']);
 const accepted=await installedLauncher(f,null,{baseline,answer:2});await accepted.start();assert.deepEqual(accepted.events,['AI Shuffle needs an installer','bundled']);assert.equal(accepted.manager.recoveryRequired,false);
 const again=await installedLauncher(f,null,{baseline});await again.start();assert.deepEqual(again.events,['bundled']);
 const backup=(await fs.readdir(f.root)).find(name=>name.startsWith('state.json.corrupt-'));assert.equal(await fs.readFile(path.join(f.root,backup),'utf8'),'{');
});
test('owner consent cannot bypass a recorded floor in an unsupported marker format',async t=>{
 const f=await fixture(t);await fs.mkdir(path.join(f.root,'versions'),{recursive:true});await fs.writeFile(path.join(f.root,'database-floor.json'),JSON.stringify({schemaVersion:999,revision:B,sequence:300}));await fs.writeFile(path.join(f.root,'state.json'),'[]');
 const launch=await installedLauncher(f,null,{baseline:{revision:C,sequence:200},answer:2});await launch.start();assert.deepEqual(launch.events,['AI Shuffle needs an installer','quit']);assert.equal(launch.manager.canConfirmBaselineRecovery,false);
});
test('a corrupt independent marker fails closed and owner-confirmed repair preserves both quarantines',async t=>{
 const f=await fixture(t);const seed=f.create();await seed.load();await f.release(A);await seed.download();await seed.select();await seed.databaseStarting();await seed.healthy();
 await fs.writeFile(path.join(f.root,'database-floor.json'),'{');await fs.writeFile(path.join(f.root,'state.json'),'[]');
 const baseline={revision:C,sequence:200};const declined=await installedLauncher(f,null,{baseline});await declined.start();assert.deepEqual(declined.events,['AI Shuffle needs an installer','quit']);
 const accepted=await installedLauncher(f,null,{baseline,answer:2});await accepted.start();assert.deepEqual(accepted.events,['AI Shuffle needs an installer','bundled']);
 for(const prefix of ['state.json.corrupt-','database-floor.json.corrupt-']){const file=(await fs.readdir(f.root)).find(name=>name.startsWith(prefix));assert(file);assert.equal(await fs.readFile(path.join(f.root,file),'utf8'),prefix.startsWith('state')?'[]':'{');}
});
test('corrupt state without provable recovery offers an installer on every launch without running any worker',async t=>{
 for(const text of ['[]','{','{"active":17}',JSON.stringify({schemaVersion:999})]){
  const f=await fixture(t);await fs.mkdir(path.join(f.root,'versions'),{recursive:true});await fs.writeFile(path.join(f.root,'state.json'),text);
  for(let launchCount=0;launchCount<2;launchCount++){
   const launch=await installedLauncher(f);await launch.start();assert.deepEqual(launch.events,['AI Shuffle needs an installer','quit']);assert.equal(launch.manager.recoveryRequired,true);await assert.rejects(launch.manager.select(),/No healthy/);
  }
  const backup=(await fs.readdir(f.root)).find(name=>name.startsWith('state.json.corrupt-'));assert.equal(await fs.readFile(path.join(f.root,backup),'utf8'),text);
 }
});
test('corrupt recovery honors retained fence floors and never falls back to an older complete revision',async t=>{
 const {f,b}=await migratedPair(t);
 for(const damage of ['missing highest build','unverifiable highest signature','fence without directory','equal signed sequences']){
  await fs.writeFile(path.join(f.root,'versions',B,'release.json'),b.signed);await fs.writeFile(path.join(f.root,'versions',B,'source/desktop/dist/main/managed-entry.js'),B);
  const state={active:17,mayHaveMigrated:{[B]:b.manifest.sequence}};
  if(damage==='missing highest build')await fs.unlink(path.join(f.root,'versions',B,'source/desktop/dist/main/managed-entry.js'));
  if(damage==='unverifiable highest signature')await fs.writeFile(path.join(f.root,'versions',B,'release.json'),'{}');
  if(damage==='fence without directory')state.mayHaveMigrated[C]=b.manifest.sequence+1;
  if(damage==='equal signed sequences'){const equal=await f.release(A,undefined,{sequence:b.manifest.sequence});await fs.writeFile(path.join(f.root,'versions',A,'release.json'),equal.signed);}
  await fs.writeFile(path.join(f.root,'state.json'),JSON.stringify(state));const launch=await installedLauncher(f);await launch.start();assert.deepEqual(launch.events,['AI Shuffle needs an installer','quit'],damage);assert.equal(launch.manager.recoveryRequired,damage!=='missing highest build');
  if(damage==='missing highest build'){
   const newer=new SourceUpdates({...f.options,baseline:{revision:C,sequence:b.manifest.sequence+1}});await newer.load();await newer.useNewerBaseline();assert.equal(newer.hasSourceState,false,'a verified newer installer can supersede an incomplete build with a known migration floor');
  }
 }
});
test('invalid migration recovery state fails closed on both load and direct attachment',async t=>{
 const f=await fixture(t);
 for(const mayHaveMigrated of [[],null,{bad:100},{[B]:0},{[B]:1.5},{[B]:'100'}]){
  await fs.writeFile(path.join(f.root,'state.json'),JSON.stringify({active:A,mayHaveMigrated}));
  await assert.rejects(f.create().attach(A),/Invalid migration recovery state/);const recovered=f.create();await recovered.load();assert.equal(recovered.recoveryRequired,true);assert.equal(recovered.blocked,true);await assert.rejects(recovered.select(),/No healthy/);
 }
});
test('pre-database failures do not create migration fences, and an equal-sequence different revision never builds over a fence',async t=>{
 const f=await fixture(t);let u=f.create();await u.load();await f.release(A);await u.download();await u.select();await u.healthy();
 const b=await f.release(B);await u.download();await u.select();await u.attach(B);await u.failed();
 u=f.create();await u.load();assert.equal(u.hasMigrationRisk,false);assert.equal(u.blocked,false);assert.equal((await u.select()).revision,A);
 await u.prepareInstall();await u.select();await u.attach(B);await u.databaseStarting();await u.failed();
 u=f.create();await u.load();assert.deepEqual(JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8')).mayHaveMigrated,{[B]:b.manifest.sequence});assert.equal(u.pendingVersion,null);
 const builds=f.builds;await f.release(C,null,{sequence:b.manifest.sequence});await assert.rejects(u.download(),/may have been migrated/);assert.equal(f.builds,builds,'never run older/equal source build scripts');
 await f.release(C);await u.download();await u.select();await u.attach(C);await u.databaseStarting();await u.healthy();
 const recovered=f.create();await recovered.load();assert.equal(recovered.hasMigrationRisk,false);assert.equal(recovered.blocked,false);assert.equal((await recovered.select()).revision,C);
});
test('healthy supersession preserves failed diagnostics without resurrecting old fences or permitting a stale staged downgrade',async t=>{
 const D='d'.repeat(40);const f=await fixture(t);let u=f.create();await u.load();await f.release(A);await u.download();await u.select();await u.healthy();
 await f.release(B);await u.download();await u.select();await u.attach(B);await u.databaseStarting();await u.failed();
 await f.release(C);await u.download();await u.select();await u.attach(C);await u.databaseStarting();await u.healthy();
 let state=JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8'));assert.deepEqual(state.failed,[B]);assert.deepEqual(state.supersededFailed,[B]);assert.equal(state.mayHaveMigrated,undefined);
 await fs.writeFile(path.join(f.root,'state.json'),JSON.stringify({...state,staged:A}));u=f.create();await u.load();await assert.rejects(u.prepareInstall(),/older code/);assert.equal((await u.select()).revision,C);
 await fs.writeFile(path.join(f.root,'state.json'),JSON.stringify({...state,pending:A}));u=f.create();await u.load();await assert.rejects(u.select(),/older code/);
 await fs.writeFile(path.join(f.root,'state.json'),JSON.stringify(state));u=f.create();await u.load();await fs.unlink(path.join(f.root,'versions',B,'release.json'));
 await f.release(D);await u.download();await u.select();await u.attach(D);await u.databaseStarting();await u.failed();
 u=f.create();await u.load();state=JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8'));assert.deepEqual(Object.keys(state.mayHaveMigrated),[D]);assert.equal(await u.retryFailed(),true);assert.equal((await u.select()).revision,D);
});
test('source builds stage without replacing running files and activate on restart; offline launches work',async t=>{
 const f=await fixture(t);await f.release(A);let u=f.create();await u.load();assert.equal((await u.download()).kind,'ready');const old=await u.select();await u.healthy();
 assert.equal(old.node,path.join(old.resources,process.platform==='win32'?'worker/node/node.exe':'worker/node/bin/node'));
 assert.equal(old.python,path.join(old.resources,process.platform==='win32'?'worker/python/python.exe':'worker/python/bin/python3'));
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
test('a first boot quit before its stack started is retried on the next launch, not blocked',async t=>{
 const f=await fixture(t);await f.release(A);let u=f.create();await u.load();await u.download();await u.select();await u.healthy();
 await f.release(B);await u.download();
 u=f.create();await u.load();assert.equal((await u.select()).revision,B);
 // The launched build attaches to its own first boot, then quits at the disclosure.
 const launched=f.create();await launched.attach(B);await launched.deferBoot();
 u=f.create();await u.load();assert.equal(u.blocked,false);assert.equal((await u.select()).revision,B);await u.healthy();assert.equal(u.version,B);
 // Once healthy there is no first boot left to defer.
 await u.deferBoot();u=f.create();await u.load();assert.equal((await u.select()).revision,B);
});
test('a first boot that crashes before its database starts goes back to the previous version with the update still offered',async t=>{
 const f=await fixture(t);await f.release(A);let u=f.create();await u.load();await u.download();await u.select();await u.healthy();
 await f.release(B);await u.download();
 u=f.create();await u.load();await u.select();
 // The launched build attached, then crashed before it started the database.
 await f.create().attach(B);
 u=f.create();await u.load();assert.equal(u.blocked,false);assert.equal(u.pendingVersion,B);
 assert.equal((await u.select()).revision,A,'the previous version runs, not the unstarted update');
 assert.equal((await u.check()).kind,'ready','the update is offered again');
 // Restarting into it and crashing after the database started blocks as before.
 await u.prepareInstall();u=f.create();await u.load();await u.select();
 const launched=f.create();await launched.attach(B);await launched.databaseStarting();
 u=f.create();await u.load();assert.equal(u.blocked,true);await assert.rejects(u.select(),/No healthy/);
});
test('a blocked launch can run the newest failed build again, never an older one',async t=>{
 const f=await fixture(t);await f.release(A);let u=f.create();await u.load();await u.download();await u.select();await u.healthy();
 await f.release(B);await u.download();u=f.create();await u.load();await u.select();
 u=f.create();await u.load();assert.equal(u.blocked,true);
 assert.equal(await u.retryFailed(),true);assert.equal(u.blocked,false);
 assert.equal((await u.select()).revision,B);await u.healthy();assert.equal(u.version,B);
 assert.equal(await u.retryFailed(),false,'nothing is blocked any more');
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
 const f=await fixture(t);const a=await f.release(A);await f.release(B);const u=f.create();await u.load();await u.download();await u.select();await u.databaseStarting();await u.healthy();f.signed=a.signed;assert.equal((await u.check()).kind,'current');assert.equal(u.version,B);
 assert.throws(()=>verifySourceManifest('{}',f.options.publicKey),/Unsigned/);await fs.writeFile(path.join(f.root,'state.json'),JSON.stringify({active:'../elsewhere'}));const recovered=f.create();await recovered.load();assert.equal((await recovered.select()).revision,B);assert.equal(recovered.hasMigrationRisk,true);
});
test('cleanup detaches dependency junctions; builds do not inherit credentials',async t=>{
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'shuffle-cache-test-'));t.after(()=>fs.rm(root,{recursive:true,force:true}));
 const cache=path.join(root,'cache');await fs.mkdir(cache);await fs.writeFile(path.join(cache,'sentinel'),'keep');
 const version=path.join(root,'version');await fs.mkdir(path.join(version,'source/frontend'),{recursive:true});await fs.symlink(cache,path.join(version,'source/frontend/node_modules'),process.platform==='win32'?'junction':'dir');
 await fs.mkdir(path.join(version,'source/mobile'));await fs.symlink(path.join(version,'source/frontend/node_modules'),path.join(version,'source/mobile/node_modules'),process.platform==='win32'?'junction':'dir');
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
  require:id=>id==='./source-auth'?{...localRequire('./source-auth'),publicSourceFetch:async(...args)=>{requests.push(args);return f.options.fetch(args[0]);}}:localRequire(id)
 });
 const u=new module.exports.SourceUpdates({...f.options,fetch:undefined});await u.load();
 await u.check();assert.deepEqual(requests,[['https://hobbycoders.com/v1/desktop/manifest']]);
 await u.download();assert.equal(requests.length,3);assert(requests.every(args=>args.length===1));await u.select();await u.healthy();await f.release(B);
 await u.check();assert.equal(requests.length,4);assert.equal(f.builds,1);
 // With a lease, the public transport receives it as its second argument.
 u.useLease(async()=>'lease-value_1');await u.check();assert.deepEqual(requests.at(-1),['https://hobbycoders.com/v1/desktop/manifest','lease-value_1']);
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

test('compact native and Python dependency caches resolve after activation; legacy layouts stay readable',async t=>{
 const f=await fixture(t);const build=f.options.build;const pythonKey='e'.repeat(64);
 f.options.build=async(...args)=>{
  const built=await build(...args);
  await fs.rename(path.join(f.root,'runtimes',KEY),path.join(f.root,'runtimes',KEY.slice(0,24)));
  const python=path.join(f.root,'dependencies',KEY.slice(0,24),'python',pythonKey.slice(0,24),process.platform==='win32'?'Scripts/python.exe':'bin/python');
  await fs.mkdir(path.dirname(python),{recursive:true});await fs.writeFile(python,'python runtime');
  return {...built,layout:2,pythonKey};
 };
 await f.release(A);const u=f.create();await u.load();await u.download();
 const app=await u.select();assert.equal(app.resources,path.join(f.root,'runtimes',KEY.slice(0,24)));
 assert.equal(app.python,path.join(f.root,'dependencies',KEY.slice(0,24),'python',pythonKey.slice(0,24),process.platform==='win32'?'Scripts/python.exe':'bin/python'));
 await u.healthy();assert.equal(u.version,A);assert.equal((await u.application(A)).revision,A);
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
 await assert.rejects(u.select(),/No healthy/);assert.deepEqual(JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8')),{schemaVersion:1,failed:[A],blocked:true,mayHaveMigrated:{[A]:100}});
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
 await request('https://hobbycoders.com/v1/desktop/manifest/nightly');
 for(const url of ['https://evil.example/v1/desktop/manifest','https://user:secret@hobbycoders.com/v1/desktop/manifest','https://hobbycoders.com/v1/desktop/manifest?token=secret','https://hobbycoders.com/v1/desktop/manifest/beta'])await assert.rejects(request(url),/Invalid source/);
 assert.equal(requests.length,2);
 // The lease header goes only to the hub's update endpoints, never with a redirect or credentials.
 await request('https://hobbycoders.com/v1/desktop/sources/'+A,'lease_1-x');
 assert.deepEqual({...requests[2][1].headers},{'X-Shuffle-Lease':'lease_1-x'});assert.equal(requests[2][1].redirect,'error');assert.equal(requests[2][1].credentials,'omit');
 for(const url of ['https://evil.example/v1/desktop/manifest','https://hobbycoders.com/v1/account','http://hobbycoders.com/v1/desktop/manifest'])await assert.rejects(request(url,'lease_1-x'),/Invalid source/);
 for(const lease of ['','has space','a\r\nX-Other: 1','x'.repeat(4097)])await assert.rejects(request('https://hobbycoders.com/v1/desktop/manifest',lease),/Invalid license lease/);
 assert.equal(requests.length,3);
 // First install: the license key rides the same checks, and a lease wins over it.
 await request('https://hobbycoders.com/v1/desktop/manifest',undefined,'ASH-7K2QD-M4XRT-9PWZC-H3N8B');
 assert.deepEqual({...requests[3][1].headers},{'X-Shuffle-License-Key':'ASH-7K2QD-M4XRT-9PWZC-H3N8B'});assert.equal(requests[3][1].redirect,'error');
 await request('https://hobbycoders.com/v1/desktop/manifest','lease_1-x','ASH-7K2QD-M4XRT-9PWZC-H3N8B');
 assert.deepEqual({...requests[4][1].headers},{'X-Shuffle-Lease':'lease_1-x'});
 await assert.rejects(request('https://evil.example/v1/desktop/manifest',undefined,'ASH-7K2QD-M4XRT-9PWZC-H3N8B'),/Invalid source/);
 for(const key of ['','short','ASH-7K2QD\r\nX-Other: 1-XXXXXXXXXXXX',' ASH-7K2QD-M4XRT-9PWZC-H3N8B','x'.repeat(65)])await assert.rejects(request('https://hobbycoders.com/v1/desktop/manifest',undefined,key),/Invalid license key/);
 assert.equal(requests.length,5);
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
for (const configured of ['nightly','edge']) for (const signed of ['nightly','edge']) {
 test(`${configured} follows nightly and accepts signed ${signed} during the transition`,async t=>{
  const f=await fixture(t);const u=f.create();await u.load();
  await fs.writeFile(path.join(f.root,'update-channel.json'),JSON.stringify({channel:configured}));
  assert.equal(await u.channel(),'nightly');
  await f.release(A);await assert.rejects(u.check(),/did not return a signed nightly release/);
  assert.deepEqual(f.requests,['https://hobbycoders.com/v1/desktop/manifest/nightly']);
  await f.release(A,undefined,{channel:signed,version:'0.9.1-dev.3+gabc1234'});assert.equal((await u.download()).kind,'ready');
  for(const text of ['{"channel":"beta"}','not json','{}']){
   await fs.writeFile(path.join(f.root,'update-channel.json'),text);await assert.rejects(u.channel(),/update-channel.json must contain/);
  }
  await fs.rm(path.join(f.root,'update-channel.json'));assert.equal(await u.channel(),'stable');
  assert.equal(await new SourceUpdates({...f.options,channel:'edge'}).channel(),'nightly');
 });
}
test('channel setter persists the picker choice and refuses a downgrade',async t=>{
 const f=await fixture(t);const u=f.create();await u.load();
 assert.deepEqual(await u.channelConfig(),{channel:'stable',source:'default',editable:true});
 await u.setChannel('nightly');
 assert.deepEqual(JSON.parse(await fs.readFile(path.join(f.root,'update-channel.json'),'utf8')),{channel:'nightly'});
 assert.deepEqual(await f.create().channelConfig(),{channel:'nightly',source:'file',editable:true});
 await assert.rejects(u.setChannel('stable'),/reinstall/);
 await assert.rejects(u.setChannel('beta'),/cannot be changed/);
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
 const names=['z.txt','a-b/x','a.b','a/b','A/B','é/ü.md','日本/語.txt','a/b c','requirements.lock'];
 const files=names.map(name=>[name,createHash('sha256').update('content of '+name).digest('hex')]);
 // ZIP names are case-sensitive even when the test host's filesystem is not.
 const archive=path.join(root,'tree.zip');execFileSync(process.env.PYTHON||'python',['-c','import sys,json,zipfile; z=zipfile.ZipFile(sys.argv[1],"w"); [z.writestr(sys.argv[2]+name,"content of "+name) for name in json.loads(sys.argv[3])]; z.close()',archive,'ai-shuffle-'+A+'/',JSON.stringify(names)]);
 assert.equal(treeDigest(files),publishedTree(archive));assert.equal(treeDigest([...files].reverse()),treeDigest(files));
});
test('release fields are validated when present and older installed manifests without them stay readable',async t=>{
 const f=await fixture(t);const {privateKey,publicKey:key}=generateKeyPairSync('ed25519');const publicKey=key.export({type:'spki',format:'pem'});
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
test('a compatibility preflight failure never writes booting or blocks the next installed launch',async t=>{
 const f=await fixture(t);await f.release(A);const u=f.create();await u.load();await u.download();
 await assert.rejects(u.select(async()=>{throw Error('Installer required');}),/Installer required/);
 let state=JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8'));assert.equal(state.booting,undefined);assert.equal(state.pending,A);
 const next=f.create();await next.load();assert.equal(next.blocked,false);
 next.stageDownloads();await next.cancelInstall();state=JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8'));
 assert.equal(state.pending,undefined);assert.equal(state.staged,A);assert.equal(next.hasSourceState,false);
});
test('#1821 D5: desktop.launcher is validated when present; manifests without it need no launcher and still install',async t=>{
 const f=await fixture(t);const {privateKey,publicKey:key}=generateKeyPairSync('ed25519');const publicKey=key.export({type:'spki',format:'pem'});
 const signed=fields=>{const payload=Buffer.from(JSON.stringify({format:2,revision:A,sequence:5,sha256:KEY,size:10,...fields}));return JSON.stringify({payload:payload.toString('base64'),signature:sign(null,payload,privateKey).toString('base64')});};
 assert.equal(requiredLauncher(verifySourceManifest(signed({}),publicKey)),0);
 assert.equal(requiredLauncher(verifySourceManifest(signed({desktop:{}}),publicKey)),0);
 assert.equal(requiredLauncher(verifySourceManifest(signed({desktop:{launcher:0}}),publicKey)),0);
 assert.equal(requiredLauncher(verifySourceManifest(signed({desktop:{launcher:3}}),publicKey)),3);
 // Later desktop fields are tolerated, like the top-level ones older clients ignore.
 assert.equal(requiredLauncher(verifySourceManifest(signed({desktop:{launcher:2,future:true}}),publicKey)),2);
 for(const desktop of [null,[],'1',1,{launcher:-1},{launcher:1.5},{launcher:'2'},{launcher:null}])
  assert.throws(()=>verifySourceManifest(signed({desktop}),publicKey),/release fields/,JSON.stringify(desktop));
 // A signed release carrying the field downloads, builds and activates like any other: nothing acts on it yet.
 await f.release(A,undefined,{desktop:{launcher:1}});const u=f.create();await u.load();
 assert.equal((await u.download()).kind,'ready');assert.equal((await u.select()).revision,A);
});
test('Python dependencies install only from the hash-pinned locks, like the container updater',async t=>{
 const {pythonInstallSteps,pythonDependencyKey,buildSource}=require('../dist/main/source-build.js');
 const steps=pythonInstallSteps();
 assert(!steps[1].includes('--no-deps'));assert(steps[1].includes('--only-binary=pynput,evdev-binary'));assert(steps[1].includes('desktop/vendor'));
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
test('the license lease goes to the hub manifest and archive requests, and only when one is available',async t=>{
 const f=await fixture(t);await f.release(A);const fetch=f.options.fetch;const seen=[];
 f.options.fetch=async(url,init)=>{seen.push([url,init?.headers?.['X-Shuffle-Lease']]);return fetch(url,init);};
 const lease='eyJmb3JtYXQiOiJzaHVmZmxlLWxlYXNlLXYxIn0';let current=lease;
 let u=f.create();await u.load();
 await u.check();assert.deepEqual(seen,[['https://hobbycoders.com/v1/desktop/manifest',undefined]],'no provider: no header');
 u.useLease(async()=>current);seen.length=0;await u.download();
 assert.deepEqual(seen,[['https://hobbycoders.com/v1/desktop/manifest',lease],['https://hobbycoders.com/v1/desktop/sources/'+A,lease]]);
 await u.select();await u.healthy();await f.release(B);
 for(const value of [null,'',undefined,'has space','a\r\nX-Other: 1','x'.repeat(4097)]){
  current=value;seen.length=0;assert.equal((await u.check()).kind,'available');assert.deepEqual(seen,[['https://hobbycoders.com/v1/desktop/manifest',undefined]],JSON.stringify(value));
 }
 u.useLease(async()=>{throw Error('worker restarting');});seen.length=0;assert.equal((await u.check()).kind,'available');assert.equal(seen[0][1],undefined,'a failing provider never blocks updates');
});
test('a first install sends the license key it was given until a lease exists, then forgets it',async t=>{
 const f=await fixture(t);await f.release(A);const fetch=f.options.fetch;const seen=[];let gate=true;
 // A stand-in for the hub with LICENSE_GATE=on: a lease or the right key opens the feed.
 f.options.fetch=async(url,init)=>{const h=init?.headers??{};seen.push([url,h['X-Shuffle-Lease'],h['X-Shuffle-License-Key']]);
  if(gate&&!h['X-Shuffle-Lease']&&h['X-Shuffle-License-Key']!=='ASH-7K2QD-M4XRT-9PWZC-H3N8B')return new Response('{"code":"license_required"}',{status:402});return fetch(url,init);};
 const u=f.create();await u.load();
 await assert.rejects(u.download(),error=>error.code==='license_required'&&error.keySent===false&&/license key/.test(error.message));
 assert.deepEqual(seen,[['https://hobbycoders.com/v1/desktop/manifest',undefined,undefined]]);
 await assert.rejects(u.saveLicenseKey('not a key'),/does not look like a license key/);
 await u.saveLicenseKey('ASH-00000-00000-00000-00000');seen.length=0;
 await assert.rejects(u.download(),error=>error.code==='license_required'&&error.keySent===true);
 await u.saveLicenseKey(' ASH-7K2QD-M4XRT-9PWZC-H3N8B ');seen.length=0;
 const stat=await fs.stat(path.join(f.root,'license-key.json'));if(process.platform!=='win32')assert.equal(stat.mode&0o777,0o600);
 assert.equal((await u.download()).kind,'ready');
 assert.deepEqual(seen,[['https://hobbycoders.com/v1/desktop/manifest',undefined,'ASH-7K2QD-M4XRT-9PWZC-H3N8B'],['https://hobbycoders.com/v1/desktop/sources/'+A,undefined,'ASH-7K2QD-M4XRT-9PWZC-H3N8B']]);
 await u.select();await u.healthy();await f.release(B);
 // Once the worker has a lease, the lease goes instead of the key, and the key file is removed.
 u.useLease(async()=>'lease_1-x');seen.length=0;assert.equal((await u.check()).kind,'available');
 assert.deepEqual(seen,[['https://hobbycoders.com/v1/desktop/manifest','lease_1-x',undefined]]);
 assert.equal(await u.licenseKey(),null);await assert.rejects(fs.stat(path.join(f.root,'license-key.json')),{code:'ENOENT'});
 // A key file that is not a key is ignored rather than sent.
 await fs.writeFile(path.join(f.root,'license-key.json'),'{"key":"bad\\r\\nX: 1"}');u.useLease(async()=>null);seen.length=0;gate=false;
 await u.check();assert.deepEqual(seen,[['https://hobbycoders.com/v1/desktop/manifest',undefined,undefined]]);
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
test('the update lock: a live holder refuses, a dead holder is reclaimed, fresh garbage asks for attention, stale garbage is taken over, and every outcome releases it',async t=>{
 const f=await fixture(t);const release=await f.release(A);const u=f.create();await u.load();
 const lock=path.join(f.root,'update.lock');
 // Another updater process is running (this test process stands in for it).
 await fs.writeFile(lock,String(process.pid));
 await assert.rejects(u.check(),/already running/);
 assert.equal(await fs.readFile(lock,'utf8'),String(process.pid),'a live holder\'s lock is left alone');
 assert.deepEqual(f.requests,[],'nothing is fetched under another updater\'s lock');
 // A holder that died (a crash mid-update) is reclaimed.
 const dead=execFileSync(process.execPath,['-e','process.stdout.write(String(process.pid))'],{encoding:'utf8'});
 await fs.writeFile(lock,dead);
 assert.equal((await u.check()).kind,'available');
 await assert.rejects(fs.stat(lock),/ENOENT/,'released after a check');
 // A lock without a pid is not guessed at.
 await fs.writeFile(lock,'not a pid');
 await assert.rejects(u.check(),/needs attention/);
 // Old enough, an empty or unparsable lock is a crash leftover: taken over once, with a warning, and released.
 for(const content of ['','not a pid']){
  await fs.writeFile(lock,content);const old=new Date(Date.now()-11*60_000);await fs.utimes(lock,old,old);
  const warnings=[];const warn=console.warn;console.warn=(...args)=>warnings.push(args.join(' '));
  try{assert.equal((await u.check()).kind,'available');}finally{console.warn=warn;}
  assert.equal(warnings.length,1);assert.match(warnings[0],/Taking over an update lock without a process id/);
  await assert.rejects(fs.stat(lock),/ENOENT/);
 }
 // A fresh empty lock may be an updater still writing its pid: left alone.
 await fs.writeFile(lock,'');
 await assert.rejects(u.check(),/needs attention/);
 await fs.rm(lock);
 // A corrupt manifest fails the check, before any archive request, and still releases the lock.
 f.signed='{"payload":';f.requests.length=0;
 await assert.rejects(u.download(),SyntaxError);
 assert.deepEqual(f.requests,['https://hobbycoders.com/v1/desktop/manifest']);
 await assert.rejects(fs.stat(lock),/ENOENT/);
 f.signed=release.signed;assert.equal((await u.download()).kind,'ready');
 await assert.rejects(fs.stat(lock),/ENOENT/);
});

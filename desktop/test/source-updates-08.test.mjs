import { assert, generateKeyPairSync, createHash, sign, execFileSync, fs, os, path, createRequire, vm, fileURLToPath, test, afterRename, require, SourceUpdates, RestorePointError, InstallerRequiredError, verifySourceManifest, treeDigest, requiredLauncher, startLauncher, buildEnvironment, removeSourceDirectory, extractor, repoScripts, publishedTree, LegacyUpdates, OldUpdates, A, B, C, E, KEY, fixture, installedLauncher, rollbackFixture, oldLauncherUpdate, legacyRetriedState, migratedPair, legacyFailedPair, stagedOver } from './helpers/source-updates-fixture.mjs';

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

test('Linux startup drops only incomplete unselected downloads, validates healthy code and permits a real re-download',async t=>{
 for(const active of [false,true])for(const missing of ['build.json','release.json']){
  const f=await fixture(t);const seed=f.create();await seed.load();
  if(active){await f.release(A);await seed.download();await seed.select();await seed.healthy();}
  seed.stageDownloads();await f.release(B);await seed.download();await fs.unlink(path.join(f.root,'versions',B,missing));
  const launch=await installedLauncher(f);await launch.start();const {manager,events}=launch;assert.deepEqual(events,[active?A:'bundled']);assert.equal(manager.pendingVersion,null);
  assert.equal((await manager.check()).kind,'available');manager.stageDownloads();await manager.download();assert.equal((await manager.application(B)).revision,B);
 }
});

test('older launcher: a failed late copy stays closed with a plain reason, and Try again or the next launch recover',async t=>{
 for(const next of ['try again','reopen'])await t.test(next,async t=>{
  const o=await oldLauncherUpdate(t);const snapshot=o.db.snapshot;
  o.db.snapshot=async()=>{throw Error('ENOSPC: no space left on device');};
  const child=await o.child();
  await assert.rejects(child.requireRestorePoint(undefined,o.late),error=>error instanceof RestorePointError &&
   /^Could not prepare the update's database restore point: ENOSPC: no space left on device\. There is not enough free disk space for a copy of your database\. Free some disk space, then choose Try again\. If it keeps failing, close AI Shuffle and open it again: the previous version opens/.test(error.message));
  assert.deepEqual(o.calls,['discard']);await assert.rejects(fs.stat(o.journalFile),{code:'ENOENT'});
  assert.equal((await o.floor()).revision,A,'the database was not handed to B');
  o.db.snapshot=snapshot;
  if(next==='reopen'){
   // Closing the window and opening AI Shuffle again: 0.9.6 keeps A and the update ready.
   const launcher=new OldUpdates(o.f.options);await launcher.load();assert.equal(launcher.pendingVersion,B);
   assert.equal((await launcher.select()).revision,A);
   const a=new OldUpdates(o.f.options);await a.attach(A);await a.databaseStarting();a.stageDownloads();await a.prepareInstall();
   const select=new OldUpdates(o.f.options);await select.load();assert.equal((await select.select()).revision,B);
  }
  const retry=await o.child();await retry.requireRestorePoint(undefined,o.late);
  assert.equal((await o.journal()).phase,'ready');assert.deepEqual(o.calls,['discard','snapshot']);
 });
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

test('unreadable running metadata leaves the verified staged offer ready',async t=>{
 const {f,u}=await stagedOver(t);
 await fs.writeFile(path.join(f.root,'versions',A,'release.json'),'unreadable');
 await f.release(A);f.requests.length=0;
 const result=await u.check();
 assert.equal(result.kind,'ready');assert.equal(result.version,B.slice(0,12));
 assert.equal(u.pendingVersion,B);
 assert.deepEqual(f.requests,['https://hobbycoders.com/v1/desktop/manifest']);
});

test('legacy main retry snapshot restores the fence before cancel and permits healthy recovery without an installer',async t=>{
 const {f,b}=await migratedPair(t);await fs.writeFile(path.join(f.root,'state.json'),JSON.stringify(legacyRetriedState()));
 let u=f.create();await u.load();u.stageDownloads();await assert.rejects(u.cancelInstall(),/may have been migrated/);
 assert.deepEqual(JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8')).mayHaveMigrated,{[B]:b.manifest.sequence});
 await assert.rejects(u.select(),/No healthy/);await assert.rejects(f.create().attach(A),/may have been migrated/);assert.equal(await u.discardStaged(B),false);
 u=f.create();await u.load();await u.prepareInstall();assert.equal((await u.select()).revision,B);await u.attach(B);await u.databaseStarting();await u.healthy();
 const healthy=f.create();await healthy.load();assert.equal(healthy.hasMigrationRisk,false);assert.equal((await healthy.select()).revision,B);
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

test('restore journal: compatibility rejection discards an unused copy without blacklisting the staged update',async t=>{
 const {f,u,factory,calls}=await rollbackFixture(t);await u.prepareRollback(factory);
 await u.cancelPreparedRollback(factory);u.stageDownloads();await u.cancelInstall();
 const restart=f.create();await restart.load();assert.equal(await restart.recoverRollback(factory),false);
 assert.deepEqual(calls,['snapshot','discard']);assert.equal((await restart.select()).revision,A);
 assert.equal((await restart.check()).kind,'ready');
});

test('invalid migration recovery state fails closed on both load and direct attachment',async t=>{
 const f=await fixture(t);
 for(const mayHaveMigrated of [[],null,{bad:100},{[B]:0},{[B]:1.5},{[B]:'100'}]){
  await fs.writeFile(path.join(f.root,'state.json'),JSON.stringify({active:A,mayHaveMigrated}));
  await assert.rejects(f.create().attach(A),/Invalid migration recovery state/);const recovered=f.create();await recovered.load();assert.equal(recovered.recoveryRequired,true);assert.equal(recovered.blocked,true);await assert.rejects(recovered.select(),/No healthy/);
 }
});

test('source downloads ignore old key files and send no lease or license header',async t=>{
 const f=await fixture(t);await f.release(A);const fetch=f.options.fetch;const seen=[];
 await fs.writeFile(path.join(f.root,'license-key.json'),JSON.stringify({key:'obsolete'}));
 f.options.fetch=async(url,init)=>{seen.push([url,init?.headers]);return fetch(url,init);};
 const u=f.create();await u.load();
 assert.equal((await u.download()).kind,'ready');
 assert.equal(seen.length,2);
 for(const [,headers] of seen){assert.equal(headers['X-Shuffle-Lease'],undefined);assert.equal(headers['X-Shuffle-License-Key'],undefined);}
});

test('owner consent cannot bypass a recorded floor in an unsupported marker format',async t=>{
 const f=await fixture(t);await fs.mkdir(path.join(f.root,'versions'),{recursive:true});await fs.writeFile(path.join(f.root,'database-floor.json'),JSON.stringify({schemaVersion:999,revision:B,sequence:300}));await fs.writeFile(path.join(f.root,'state.json'),'[]');
 const launch=await installedLauncher(f,null,{baseline:{revision:C,sequence:200},answer:2});await launch.start();assert.deepEqual(launch.events,['AI Shuffle needs an installer','quit']);assert.equal(launch.manager.canConfirmBaselineRecovery,false);
});

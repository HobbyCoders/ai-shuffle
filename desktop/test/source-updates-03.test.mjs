import { assert, generateKeyPairSync, createHash, sign, execFileSync, fs, os, path, createRequire, vm, fileURLToPath, test, afterRename, require, SourceUpdates, RestorePointError, InstallerRequiredError, verifySourceManifest, treeDigest, requiredLauncher, startLauncher, buildEnvironment, removeSourceDirectory, extractor, repoScripts, publishedTree, LegacyUpdates, OldUpdates, A, B, C, E, KEY, fixture, installedLauncher, rollbackFixture, oldLauncherUpdate, legacyRetriedState, migratedPair, legacyFailedPair, stagedOver } from './helpers/source-updates-fixture.mjs';

test('trusted installer at or above the durable floor repairs missing-code recovery without losing quarantine',async t=>{
 for(const delta of [-1,0,1]){
  const {f,b}=await migratedPair(t);await fs.rm(path.join(f.root,'versions',B),{recursive:true,maxRetries:10,retryDelay:100});await fs.writeFile(path.join(f.root,'state.json'),'[]');
  const old=await installedLauncher(f);await old.start();assert(old.events.includes('quit'));
  const baseline={revision:C,sequence:b.manifest.sequence+delta};const launch=await installedLauncher(f,null,{baseline,answer:2});await launch.start();
  if(delta<0){assert.deepEqual(launch.events,['AI Shuffle needs an installer','quit']);continue;}
  assert.deepEqual(launch.events,['bundled']);assert.equal(launch.manager.recoveryRequired,false);assert.equal(launch.manager.hasSourceState,false);
  const again=await installedLauncher(f,null,{baseline});await again.start();assert.deepEqual(again.events,['bundled']);
  const backup=(await fs.readdir(f.root)).find(name=>name.startsWith('state.json.corrupt-'));assert.equal(await fs.readFile(path.join(f.root,backup),'utf8'),'[]');
 }
});

test('real main missing failed or retried metadata offers only owner-confirmed installer recovery',async t=>{
 for(const retry of [false,true])for(const sequence of [200,201])await t.test(`retry=${retry}, installer=${sequence}`,async t=>{
  const {f}=await legacyFailedPair(t,{retry});await fs.rm(path.join(f.root,'versions',B),{recursive:true,maxRetries:10,retryDelay:100});
  const baseline={revision:C,sequence};
  for(let i=0;i<2;i++){const declined=await installedLauncher(f,null,{baseline});await declined.start();assert.deepEqual(declined.events,['AI Shuffle needs an installer','quit']);assert.equal(declined.manager.recoveryRequired,true);assert.equal(declined.manager.canConfirmBaselineRecovery,true);assert.equal(await declined.manager.retryFailed(),false);}
  const accepted=await installedLauncher(f,null,{baseline,answer:2});await accepted.start();assert.deepEqual(accepted.events,['AI Shuffle needs an installer','bundled']);assert.equal(accepted.manager.hasSourceState,false);
  const again=await installedLauncher(f,null,{baseline});await again.start();assert.deepEqual(again.events,['bundled']);assert.equal(again.manager.recoveryRequired,false);
 });
});

test('#98: unreadable staged metadata stays visible but cannot block a newer verified download',async t=>{
 const {f,u}=await stagedOver(t);
 await fs.writeFile(path.join(f.root,'versions',B,'release.json'),'not json');
 // Neither the running revision nor a different older feed can hide the bad download.
 for(const revision of [A,E]){
  await f.release(revision,undefined,{sequence:1});f.requests.length=0;
  for(const run of [()=>u.check(),()=>u.download()]){
   const result=await run();assert.equal(result.kind,'ready');assert.equal(result.version,B.slice(0,12));
   assert.match(result.notice,/release information could not be verified.*still staged.*download a newer update/);
  }
  assert.equal(u.pendingVersion,B);assert.equal(f.requests.length,2);
 }
 await assert.rejects(u.prepareInstall(),error=>!(error instanceof SyntaxError)&&/downloaded update could not be verified.*download a newer update/.test(error.message));
 assert.equal(u.pendingVersion,B);assert.equal(JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8')).pending,undefined);
 // A check remains metadata-only, but a successful newer download repairs the dead end.
 await f.release(C,undefined,{sequence:999});f.requests.length=0;
 assert.equal((await u.check()).kind,'available');assert.equal(u.pendingVersion,B);
 assert.equal(f.requests.length,1);
 const result=await u.download();assert.equal(result.kind,'ready');assert.equal(result.version,C.slice(0,12));
 assert.equal(u.pendingVersion,C);await assert.rejects(fs.stat(path.join(f.root,'versions',B)),{code:'ENOENT'});
 await u.prepareInstall();assert.equal(JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8')).pending,C);
});

test('a marker written before the database-state save keeps the floor across a crash',async t=>{
 const f=await fixture(t);const u=f.create();await u.load();await f.release(A);await u.download();await u.select();await u.databaseStarting();await u.healthy();
 await f.release(B);await u.download();await u.select();await u.attach(B);
 const waiting=await fs.readFile(path.join(f.root,'state.json'),'utf8');await u.databaseStarting();await fs.writeFile(path.join(f.root,'state.json'),waiting);
 const restarted=f.create();await restarted.load();assert.equal(restarted.hasMigrationRisk,true);await assert.rejects(restarted.select());await assert.rejects(f.create().attach(A),/may have been migrated/);
});

test('older signed manifests cannot roll back a healthy revision; corrupt state fails closed',async t=>{
 const f=await fixture(t);const a=await f.release(A);await f.release(B);const u=f.create();await u.load();await u.download();await u.select();await u.databaseStarting();await u.healthy();f.signed=a.signed;assert.equal((await u.check()).kind,'current');assert.equal(u.version,B);
 assert.throws(()=>verifySourceManifest('{}',f.options.publicKey),/Unsigned/);await fs.writeFile(path.join(f.root,'state.json'),JSON.stringify({active:'../elsewhere'}));const recovered=f.create();await recovered.load();assert.equal((await recovered.select()).revision,B);assert.equal(recovered.hasMigrationRisk,true);
});

test('a first start that never reached attach is put back by the launcher, then offered again and installs',async t=>{
 const {retryableStarts,releaseStarts}=require('../dist/main/failed-start.js');
 const {f,u,calls,factory}=await rollbackFixture(t);
 // The owner installs B: the launcher makes the restore point and selects B (booting, floor uncertain).
 await u.prepareRollback(factory); await u.select();
 // B's process is still loading (a freshly built Electron being scanned) when the app is opened again.
 // That launch sees booting without waiting, marks B failed and puts the database back.
 const next=f.create(); await next.load(); assert.equal(await next.recoverRollback(factory),true);
 assert.deepEqual(calls,['snapshot','restore','discard']);
 const left=JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8'));
 assert.deepEqual([left.active,left.failed,left.supersededFailed,left.blocked,left.mayHaveMigrated],[A,[B],[B],undefined,undefined]);
 assert.equal(JSON.parse(await fs.readFile(path.join(f.root,'database-floor.json'),'utf8')).revision,A,'B never opened the database');
 // Before this fix the feed's B was refused for good, with only an installer to offer.
 await assert.rejects(next.check(),/This revision failed to start/);
 // The previous version (the app now running) offers it again.
 assert.deepEqual(await releaseStarts(f.root,await retryableStarts(f.root)),[B]); await next.load();
 assert.equal((await next.check()).kind,'available');
 // The retry is an ordinary install: a new restore point, then B starts and becomes healthy.
 next.stageDownloads(); await next.download(); await next.prepareInstall();
 await next.prepareRollback(factory); assert.equal((await next.select()).revision,B);
 const app=f.create(); await app.attach(B); await app.databaseStarting(); await app.healthy();
 assert.equal(app.version,B); assert.equal(app.hasMigrationRisk,false);
 assert.deepEqual(calls,['snapshot','restore','discard','snapshot']);
});

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

test('older launcher: a build the 0.9.6 launcher selected makes its own restore point before its first database start',async t=>{
 const o=await oldLauncherUpdate(t);
 assert.equal((await o.floor()).uncertain,true,'0.9.6 select marks the floor uncertain before the hand-over');
 const child=await o.child();await child.requireRestorePoint(undefined,o.late);
 assert.deepEqual(o.calls,['snapshot']);assert.deepEqual(o.phases,['preparing']);assert.equal(o.preparing,1);
 const j=await o.journal();
 assert.equal(j.phase,'ready');assert.equal(j.revision,B);assert.equal(j.previous,A);assert.equal(j.state.active,A);
 assert.deepEqual(j.floor,{revision:A,sequence:100},'the previous build\'s own floor, without the hand-over uncertainty');
 for(const field of ['pending','staged','booting','waiting','blocked','mayHaveMigrated'])assert.equal(j.state[field],undefined,field);
 await child.databaseStarting();await child.healthy();await child.finishRollback(o.late.database);
 assert.deepEqual(o.calls,['snapshot','discard']);await assert.rejects(fs.stat(o.journalFile),{code:'ENOENT'});
 assert.deepEqual(await o.floor(),{schemaVersion:1,revision:B,sequence:101});
 const next=new OldUpdates(o.f.options);await next.load();assert.equal((await next.select()).revision,B);
});

test('older launcher: a failure after the fence says how the old launcher finishes it, not that the previous version opens',async t=>{
 const o=await oldLauncherUpdate(t);await (await o.child()).requireRestorePoint(undefined,o.late);
 await fs.writeFile(o.journalFile,JSON.stringify({...await o.journal(),phase:'preparing'}));
 o.db.validate=async()=>{throw Error('Database restore point failed validation');};o.db.snapshot=async()=>{throw Error('Database changed while taking its restore point');};
 await assert.rejects((await o.child()).requireRestorePoint(undefined,o.late),error=>error instanceof RestorePointError &&
  /The copy did not match your database\. Choose Try again to make a new copy\. If you close AI Shuffle instead, it will say the last update did not finish starting: choose Try Again there/.test(error.message));
});

test('restore journal: a copy cannot be discarded as a preflight rejection after selection',async t=>{
 const {u,factory}=await rollbackFixture(t);await u.prepareRollback(factory);await u.select();
 await assert.rejects(u.cancelPreparedRollback(factory),/after the update started/);
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

test('channel setter persists the picker choice and refuses a downgrade',async t=>{
 const f=await fixture(t);const u=f.create();await u.load();
 assert.deepEqual(await u.channelConfig(),{channel:'stable',source:'default',editable:true});
 await u.setChannel('nightly');
 assert.deepEqual(JSON.parse(await fs.readFile(path.join(f.root,'update-channel.json'),'utf8')),{channel:'nightly'});
 assert.deepEqual(await f.create().channelConfig(),{channel:'nightly',source:'file',editable:true});
 await assert.rejects(u.setChannel('stable'),/reinstall/);
 await assert.rejects(u.setChannel('beta'),/cannot be changed/);
});

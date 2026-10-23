import { assert, generateKeyPairSync, createHash, sign, execFileSync, fs, os, path, createRequire, vm, fileURLToPath, test, afterRename, require, SourceUpdates, RestorePointError, InstallerRequiredError, verifySourceManifest, treeDigest, requiredLauncher, startLauncher, buildEnvironment, removeSourceDirectory, extractor, repoScripts, publishedTree, LegacyUpdates, OldUpdates, A, B, C, E, KEY, fixture, installedLauncher, rollbackFixture, oldLauncherUpdate, legacyRetriedState, migratedPair, legacyFailedPair, stagedOver } from './helpers/source-updates-fixture.mjs';

test('damaged and unknown-version state is preserved and recovers using the independent database floor',async t=>{
 for(const text of ['[]','{','{"active":17}',JSON.stringify({schemaVersion:999,active:A})]){
  const {f}=await migratedPair(t);
  await fs.writeFile(path.join(f.root,'state.json'),text);const before=new Set(await fs.readdir(f.root));const u=f.create();await u.load();
  const backup=(await fs.readdir(f.root)).find(name=>name.startsWith('state.json.corrupt-')&&!before.has(name));assert(backup);assert.equal(await fs.readFile(path.join(f.root,backup),'utf8'),text);
  assert.equal(u.recoveryRequired,false);assert.equal(u.hasMigrationRisk,true);await assert.rejects(f.create().attach(A),/Unexpected desktop launch revision/);
  const launch=await installedLauncher(f);await launch.start();assert.deepEqual(launch.events,[B]);await launch.manager.attach(B);await launch.manager.databaseStarting();await launch.manager.healthy();assert.equal(launch.manager.hasMigrationRisk,false);
 }
});

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

test('#98: a waiting build takes precedence over a failed-start retry offer', async t => {
 const {f,u,factory}=await rollbackFixture(t);
 // Produce the failed-start record through the real restore path, not a seeded flag.
 await u.prepareRollback(factory);await u.select();await u.databaseStarting();await u.failed();
 await u.recoverRollback(factory);
 assert.equal(u.hasMigrationRisk,false);
 u.stageDownloads();await f.release(C);await u.download();
 // A fresh feed advertises the failed revision after the waiting build was signed.
 await f.release(B);f.requests.length=0;const builds=f.builds;
 for(const run of [()=>u.check(),()=>u.download()]){
  const result=await run();assert.equal(result.kind,'ready');assert.equal(result.version,C.slice(0,12));
 }
 assert.equal(u.pendingVersion,C);assert.equal(f.builds,builds);
 assert.deepEqual(f.requests,Array(2).fill('https://hobbycoders.com/v1/desktop/manifest'));
 // No refusal means Updater does not invoke failed-start recovery while C waits.
 // Once the waiting build is gone, the normal retry-triggering refusal returns.
 assert.equal(await u.discardStaged(C),true);
 await assert.rejects(u.check(),/This revision failed to start/);
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

test('selecting a different producer invalidates a stale floor until databaseStarting records the new one',async t=>{
 const f=await fixture(t);const u=f.create();await u.load();await f.release(A);await u.download();await u.select();await u.databaseStarting();await u.healthy();
 await f.release(B);await u.download();await u.select();
 const marker=JSON.parse(await fs.readFile(path.join(f.root,'database-floor.json'),'utf8'));assert.equal(marker.revision,A);assert.equal(marker.uncertain,true);
 // An older delegated producer can migrate B without rewriting the marker.
 await fs.rm(path.join(f.root,'versions',B),{recursive:true,maxRetries:10,retryDelay:100});await fs.writeFile(path.join(f.root,'state.json'),'[]');
 const launch=await installedLauncher(f);await launch.start();assert.deepEqual(launch.events,['AI Shuffle needs an installer','quit']);
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

test('restore journal: restore failure retains snapshot and fence; explicit Go back retries it',async t=>{
 const {f,u,db,factory,journal}=await rollbackFixture(t);
 await u.prepareRollback(factory); await u.select(); await u.databaseStarting(); await u.failed();
 const restore=db.restore; db.restore=async()=>{throw Error('file locked');};
 await assert.rejects(u.recoverRollback(factory),/file locked/);
 assert.equal((await journal()).phase,'restoring'); assert.equal(u.blocked,true);
 await assert.rejects(u.select(),/healthy/); assert.equal(await u.rollbackVersion(),'0.9.1');
 db.restore=restore; await u.recoverRollback(factory,true); assert.equal((await u.select()).revision,A);
});

test('restore journal: stale snapshot cannot lower newer baseline floor, even via forced recovery or healthy callback',async t=>{
 const {f,u,factory,calls}=await rollbackFixture(t);await u.prepareRollback(factory);await u.select();await u.databaseStarting();await u.failed();
 await fs.writeFile(path.join(f.root,'database-floor.json'),JSON.stringify({schemaVersion:1,revision:C,sequence:300}));
 const installed=f.create();await installed.load();
 await assert.rejects(installed.recoverRollback(factory,true),/no longer matches/);
 await assert.rejects(installed.finishRollback(factory),/cleanup refused/);
 assert.deepEqual(calls,['snapshot']);assert.equal(JSON.parse(await fs.readFile(path.join(f.root,'database-floor.json'))).sequence,300);
});

test('restore journal: healthy promotion and cleanup interruption keep the new build, never restore old data',async t=>{
 const {f,u,db,factory,calls,journal}=await rollbackFixture(t);
 await u.prepareRollback(factory); await u.select(); await u.databaseStarting(); await u.healthy();
 db.discard=async()=>{throw Error('file locked');};
 await u.recoverRollback(factory); assert.equal((await journal()).phase,'committed');
 db.discard=async()=>calls.push('discard');
 const restart=f.create(); await restart.load(); await restart.recoverRollback(factory);
 assert.equal((await restart.select()).revision,B); assert.deepEqual(calls,['snapshot','discard']);
});

test('restore journal: persistent cleanup lock reports actionable error, then Retry enables activation without a futile restart',async t=>{
 const {f,u,factory,db,journal}=await rollbackFixture(t,{baseline:true});u.stageDownloads();await u.prepareRollback(factory);
 let retries=0;db.discard=async()=>{retries++;throw Error('EACCES: locked snapshot');};
 await u.cancelPreparedRollback(factory);await u.cancelInstall();
 assert.equal((await journal()).phase,'committed');assert.equal(u.version,A);assert.equal(u.blocked,false);
 for(let i=0;i<2;i++)await assert.rejects(u.check(),/cleanup could not finish.*EACCES.*restore-point.json.*Retry.*hobbycoders.com/);
 await assert.rejects(u.prepareInstall(),/cleanup could not finish/);
 assert.ok(retries>=3);assert.equal(JSON.parse(await fs.readFile(path.join(f.root,'state.json'))).pending,undefined);
 const events=[],states=[],module={exports:{}};
 vm.runInNewContext(await fs.readFile(new URL('../dist/main/updater.js',import.meta.url),'utf8'),{
  module,exports:module.exports,process:{env:{}},Date,setTimeout,clearTimeout,setInterval,clearInterval,
  require:id=>id==='electron'?{app:{relaunch:()=>events.push('restart'),quit:()=>events.push('quit')}}:id==='./timed-phase'?require('../dist/main/timed-phase.js'):path,
 });
 const updater=new module.exports.Updater();updater.onBeforeInstall(async()=>events.push('stop'));
 const reported=new Promise(resolve=>updater.subscribe(state=>{states.push(state.phase);if(state.phase==='error')resolve();}));
 updater.useApplications(u,'/test/launcher');await reported;
 assert.equal(states.includes('ready'),false);assert.match(updater.current.error,/EACCES.*Retry/);
 await updater.install();assert.deepEqual(events,[],'no futile restart or service stop');
 db.discard=async()=>{};await updater.check();assert.equal(updater.current.phase,'ready');assert.equal(u.cleanupError,null);
 await updater.install();assert.deepEqual(events,['stop','restart','quit']);
 assert.equal(JSON.parse(await fs.readFile(path.join(f.root,'state.json'))).pending,B);
 await u.prepareRollback(factory);assert.equal((await journal()).phase,'ready','a new point is taken only after cleanup');
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

test('installer with no provable floor needs explicit recovery-dialog consent, then stays repaired on relaunch',async t=>{
 const f=await fixture(t);await fs.mkdir(path.join(f.root,'versions'),{recursive:true});await fs.writeFile(path.join(f.root,'state.json'),'{');
 const old=await installedLauncher(f);await old.start();assert.deepEqual(old.events,['AI Shuffle needs an installer','quit']);
 const baseline={revision:C,sequence:200};const declined=await installedLauncher(f,null,{baseline});await declined.start();assert.deepEqual(declined.events,['AI Shuffle needs an installer','quit']);
 const accepted=await installedLauncher(f,null,{baseline,answer:2});await accepted.start();assert.deepEqual(accepted.events,['AI Shuffle needs an installer','bundled']);assert.equal(accepted.manager.recoveryRequired,false);
 const again=await installedLauncher(f,null,{baseline});await again.start();assert.deepEqual(again.events,['bundled']);
 const backup=(await fs.readdir(f.root)).find(name=>name.startsWith('state.json.corrupt-'));assert.equal(await fs.readFile(path.join(f.root,backup),'utf8'),'{');
});

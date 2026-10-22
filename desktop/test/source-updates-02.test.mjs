import { assert, generateKeyPairSync, createHash, sign, execFileSync, fs, os, path, createRequire, vm, fileURLToPath, test, afterRename, require, SourceUpdates, RestorePointError, InstallerRequiredError, verifySourceManifest, treeDigest, requiredLauncher, startLauncher, buildEnvironment, removeSourceDirectory, extractor, repoScripts, publishedTree, LegacyUpdates, OldUpdates, A, B, C, E, KEY, fixture, installedLauncher, rollbackFixture, oldLauncherUpdate, legacyRetriedState, migratedPair, legacyFailedPair, stagedOver } from './helpers/source-updates-fixture.mjs';

test('older launcher: no late copy for any state it cannot prove, and fenced revisions keep their old behaviour',async t=>{
 const refuse=/No verified database restore point/;
 await t.test('not the booting build\'s own process',async t=>{
  const o=await oldLauncherUpdate(t);await o.child();
  const other=new SourceUpdates({...o.f.options,baseline:undefined});await other.attach(A);
  await assert.rejects(other.requireRestorePoint(undefined,o.late),refuse);assert.deepEqual(o.calls,[]);
 });
 await t.test('a floor from another build',async t=>{
  const o=await oldLauncherUpdate(t);await fs.writeFile(o.floorFile,JSON.stringify({schemaVersion:1,revision:C,sequence:50}));
  await assert.rejects((await o.child()).requireRestorePoint(undefined,o.late),refuse);assert.deepEqual(o.calls,[]);
 });
 for(const late of [false,true])await t.test(`a ready ${late?'late ':''}point for another build`,async t=>{
  const o=await oldLauncherUpdate(t);
  await fs.writeFile(o.journalFile,JSON.stringify({schemaVersion:1,phase:'ready',revision:C,previous:A,state:{schemaVersion:1,active:A},floor:{revision:A,sequence:100},...(late?{late:true}:{})}));
  await assert.rejects((await o.child()).requireRestorePoint(undefined,o.late),refuse);assert.deepEqual(o.calls,[]);
 });
 await t.test('a bundled previous build with an unrelated fence or block',async t=>{
  for(const extra of [{mayHaveMigrated:{[E]:40},blocked:true},{blocked:true}]){
   const o=await oldLauncherUpdate(t,{baseline:true});const file=path.join(o.f.root,'state.json');
   await fs.writeFile(file,JSON.stringify({...JSON.parse(await fs.readFile(file,'utf8')),...extra},null,2)+'\n');
   const child=await o.child();
   if(extra.mayHaveMigrated)await assert.rejects(child.requireRestorePoint(undefined,{database:async()=>assert.fail('no copy')}),refuse);
   else{
    // attach fenced the bundled floor itself, which explains the block: still the plain bundled start.
    await child.requireRestorePoint(undefined,{database:async()=>assert.fail('no copy')});
   }
  }
 });
 await t.test('an unreadable database floor',async t=>{
  for(const baseline of [false,true]){
   // attach itself refuses an unreadable floor; this pins the late path's own check behind it.
   const o=await oldLauncherUpdate(t,{baseline});const child=await o.child();child.migrationFloor=null;
   await assert.rejects(child.requireRestorePoint(undefined,{database:async()=>assert.fail('no copy')}),refuse);
  }
 });
 await t.test('a revision that may already have migrated',async t=>{
  const o=await oldLauncherUpdate(t);const child=await o.child();
  await child.databaseStarting();await child.failed();
  const launcher=new OldUpdates(o.f.options);await launcher.load();await launcher.retryFailed();await launcher.select();
  // Exactly as before: its migration fence lets it run again, without any copy.
  await (await o.child()).requireRestorePoint(undefined,{database:async()=>assert.fail('a fenced retry makes no copy')});
  assert.deepEqual(o.calls,[]);await assert.rejects(fs.stat(o.journalFile),{code:'ENOENT'});
 });
});

test('a verified current feed unselects a withdrawn unchosen download without deleting its files',async t=>{
 for(const legacy of [false,true])await t.test(legacy?'legacy pending':'explicit staged',async t=>{
  let f,manager;
  if(legacy){
   f=await fixture(t);await f.release(A);manager=f.create();await manager.load();
   await manager.download();await manager.select();await manager.databaseStarting();await manager.healthy();
   await f.release(B);await manager.download();
  }else{const staged=await stagedOver(t);f=staged.f;manager=staged.u;}
  await f.release(A);f.requests.length=0;const builds=f.builds;
  assert.equal((await manager.check()).kind,'current');assert.equal(manager.pendingVersion,null);
  assert.equal(f.builds,builds);assert.deepEqual(f.requests,['https://hobbycoders.com/v1/desktop/manifest']);
  assert((await fs.stat(path.join(f.root,'versions',B))).isDirectory());
  const reloaded=f.create();await reloaded.load();assert.equal(reloaded.pendingVersion,null);
 });
});

test('build failures preserve the active revision; failed first boots block unsafe rollback but accept newer recovery',async t=>{
 const f=await fixture(t);await f.release(A);let u=f.create();await u.load();await u.download();await u.select();await u.healthy();
 await f.release(B);f.fail=true;await assert.rejects(u.download(),/Local build failed/);assert.equal(u.version,A);f.fail=false;await u.download();
 u=f.create();await u.load();await u.select();u=f.create();await u.load();await assert.rejects(u.select(),/No healthy/);await assert.rejects(u.download(),/failed to start/);
 await f.release(C);await u.download();await u.select();await u.healthy();assert.equal(u.version,C);
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

test('launcher-validated copy still requires a ready journal for the exact booting revision', async t => {
 const { f, u } = await rollbackFixture(t);
 const { SinglePassClusterCopy, checkRestorePointStructure } = require('../dist/main/update-restore-point.js');
 const cluster = path.join(f.root, 'postgres');
 await fs.mkdir(path.join(cluster, 'data', 'global'), { recursive: true });
 await fs.writeFile(path.join(cluster, 'data', 'PG_VERSION'), '17');
 await fs.writeFile(path.join(cluster, 'data', 'global', 'pg_control'), 'control');
 const copy = new SinglePassClusterCopy(cluster, async () => {});
 await u.prepareRollback(async () => copy); // create the real ready journal and snapshot
 await u.prepareRollback(async () => copy); // launcher validates the existing copy
 await u.select();
 const child = f.create(); await child.attach(B);
 const validator = { validate: () => checkRestorePointStructure(cluster) };
 await child.requireRestorePoint(validator);
 const file = path.join(f.root, 'restore-point.json');
 const journal = JSON.parse(await fs.readFile(file, 'utf8'));
 for (const bad of [{ ...journal, revision: C }, { ...journal, phase: 'preparing' }]) {
  await fs.writeFile(file, JSON.stringify(bad));
  await assert.rejects(child.requireRestorePoint(validator), /No verified database restore point/);
 }
 await fs.writeFile(file, JSON.stringify(journal));
 await fs.rm(path.join(cluster, 'update-snapshot', 'data', 'global', 'pg_control'));
 await assert.rejects(child.requireRestorePoint(validator), /validation/);
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
 assert.equal(u.useLease,undefined,'the updater has no lease transport');
 await u.check();assert.deepEqual(requests.at(-1),['https://hobbycoders.com/v1/desktop/manifest']);
});


test('restore journal: legacy same-build retry retains its migration fence without inventing an old restore point',async t=>{
 const {f,u}=await rollbackFixture(t);await u.select();await u.databaseStarting();await u.failed();
 await u.retryFailed();await u.select();const child=f.create();await child.attach(B);
 await child.requireRestorePoint();assert.equal(child.hasMigrationRisk,true);
});
/**
 * 0.9.6 runs A (a source update, or with baseline its bundled build C), downloads
 * B, and its launcher selects B. The current build B then starts as a managed
 * process: no baseline option, as managed-entry creates it.
 */

test('restore journal: snapshot errors never select the update and retain a clear failure reason', async t => {
 const { f, u, db, calls, factory } = await rollbackFixture(t);
 db.snapshot=async()=>{throw Error('disk full');};
 await u.prepareRollback(factory);
 assert.equal((await u.select()).revision,A); assert.deepEqual(calls,['discard']);
 assert.match(await u.rollbackNotice(),/restore point failed: disk full/);
 await assert.rejects(u.check(), /This revision failed to start/);
});

test('restore journal: bundled A cleanup survives replacement by C and does not need A files or binaries',async t=>{
 const {f,u,factory}=await rollbackFixture(t,{baseline:true});await u.prepareRollback(factory);
 await assert.rejects(fs.stat(path.join(f.root,'versions',A,'release.json')),{code:'ENOENT'});
 f.options.baseline={revision:C,sequence:300};const installer=f.create();await installer.load();await installer.useNewerBaseline();
 const pgroot=path.join(f.root,'pg');await fs.mkdir(path.join(pgroot,'update-snapshot'),{recursive:true});
 const {ColdClusterCopy}=require('../dist/main/database-restore.js');let runtimeReads=0;
 const cleanup=async()=>new ColdClusterCopy(pgroot,async()=>{runtimeReads++;throw Error('bundled A no longer exists');});
 const restart=f.create();await restart.load();await restart.recoverRollback(cleanup);
 assert.equal(runtimeReads,0);assert.equal(restart.version,C);assert.equal(restart.blocked,false);
 await assert.rejects(fs.stat(path.join(f.root,'restore-point.json')),{code:'ENOENT'});
 assert.equal(JSON.parse(await fs.readFile(path.join(f.root,'database-floor.json'))).sequence,300);
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


test('real healthy main producer remains unaffected by write-provenance tracking',async t=>{
 const f=await fixture(t);const legacy=new LegacyUpdates(f.options);await legacy.load();await f.release(A);await legacy.download();await legacy.select();await legacy.attach(A);await legacy.databaseStarting();await legacy.healthy();
 for(let i=0;i<2;i++){const launch=await installedLauncher(f);await launch.start();assert.deepEqual(launch.events,[A]);assert.equal(launch.manager.hasMigrationRisk,false);await new LegacyUpdates(f.options).attach(A);}
});

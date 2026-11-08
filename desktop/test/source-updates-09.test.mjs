import { assert, generateKeyPairSync, createHash, sign, execFileSync, fs, os, path, createRequire, vm, fileURLToPath, test, afterRename, require, SourceUpdates, RestorePointError, InstallerRequiredError, verifySourceManifest, treeDigest, requiredLauncher, startLauncher, buildEnvironment, removeSourceDirectory, extractor, repoScripts, publishedTree, LegacyUpdates, OldUpdates, A, B, C, E, KEY, fixture, installedLauncher, rollbackFixture, oldLauncherUpdate, legacyRetriedState, migratedPair, legacyFailedPair, stagedOver } from './helpers/source-updates-fixture.mjs';

test('older launcher: each copy failure gets advice for its own cause',async t=>{
 for(const [error,advice] of [
  [Object.assign(Error('EACCES: permission denied, copyfile'),{code:'EACCES'}),/not allowed to read or write a file in its data folder\. Check that folder's permissions/],
  [Object.assign(Error('EBUSY: resource busy or locked, copyfile'),{code:'EBUSY'}),/in use\. Close other programs/],
  [Object.assign(Error('EPERM: operation not permitted, copyfile'),{code:'EPERM'}),/in use\. Close other programs/],
  [Error('Database restore point failed validation'),/did not match your database/],
  [Error('something else'),/something else\. Choose Try again\./],
 ])await t.test(error.message,async t=>{
  const o=await oldLauncherUpdate(t);o.db.snapshot=async()=>{throw error;};
  await assert.rejects((await o.child()).requireRestorePoint(undefined,o.late),e=>e instanceof RestorePointError&&advice.test(e.message)&&(error.code==='EACCES'||!/permissions/.test(e.message)));
 });
});

test('signature, checksum, traversal and Windows alias failures are rejected before builds',async t=>{
 const f=await fixture(t);const r=await f.release(A);const u=f.create();await u.load();
 const envelope=JSON.parse(r.signed);envelope.signature=Buffer.alloc(64).toString('base64');f.signed=JSON.stringify(envelope);await assert.rejects(u.download(),/signature/);f.signed=r.signed;
 f.bodies.set(r.url,Buffer.alloc(r.manifest.size));await assert.rejects(u.download(),/checksum/);assert.equal(f.builds,0);
 for(const name of ['../escape','C:/escape','folder/CON.txt','folder/file:stream','folder/file.','folder\\file']){
  const g=await fixture(t);await g.release(A,name);const v=g.create();await v.load();await assert.rejects(v.download());assert.equal(g.builds,0,name);
 }
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

test('an app-made restore point that cannot be discarded holds the database closed, so the launcher never restores a stale copy over newer data', async t => {
 const { SinglePassClusterCopy, pointHeldFor } = require('../dist/main/update-restore-point.js');
 const { ColdClusterCopy } = require('../dist/main/database-restore.js');
 const { f, u } = await rollbackFixture(t);
 u.stageDownloads(); await u.download();
 const rows = path.join(f.root, 'postgres', 'data', 'base', '16384', '2619');
 for (const [name, body] of Object.entries({ 'PG_VERSION': '17\n', 'global/pg_control': 'control', 'base/16384/2619': 'rows-v1' })) {
  await fs.mkdir(path.dirname(path.join(f.root, 'postgres', 'data', name)), { recursive: true });
  await fs.writeFile(path.join(f.root, 'postgres', 'data', name), body);
 }
 // Updater.install: the restart is prepared, then the app copies the database for the update's restore point.
 await u.prepareInstall(); await u.prepareRollback(async () => new SinglePassClusterCopy(path.join(f.root, 'postgres'), async () => {}));
 assert.equal(JSON.parse(await fs.readFile(path.join(f.root, 'restore-point.json'), 'utf8')).phase, 'ready');
 // The relaunch fails and so does the discard (a locked journal, a full disk): the journal stays ready.
 // The old app used to move the update back to staged and reopen the database here; the
 // launcher's next start then restored this copy over everything written since.
 // Now nothing in this version opens the database while the point is held...
 assert.equal(await pointHeldFor(f.root, A), true);
 assert.equal(u.pendingVersion, B, 'the update stays pending');
 // #98 must not supersede #225's selected update or its app-made restore point.
 await f.release(C); f.requests.length = 0;
 assert.equal((await u.check()).version, B.slice(0, 12));
 assert.equal((await u.download()).version, B.slice(0, 12));
 assert.deepEqual(f.requests, [], 'a selected update with its restore point never checks for a replacement');
 // ...and the next launcher start keeps the copy as the update's restore point and starts the update.
 const launcher = f.create(); await launcher.load();
 assert.equal(await launcher.recoverRollback(async () => new ColdClusterCopy(path.join(f.root, 'postgres'), async () => {})), false);
 await launcher.prepareRollback(async () => new ColdClusterCopy(path.join(f.root, 'postgres'), async () => {}));
 assert.equal((await launcher.select()).revision, B);
 assert.equal(await fs.readFile(rows, 'utf8'), 'rows-v1');
 // Once the update started (or the point is committed), the running version is the point's own.
 assert.equal(await pointHeldFor(f.root, B), false);
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

test('installed-entry factory fully hashes a real ready journal before selection', async t => {
 const launcher = await fs.readFile(new URL('../launcher.json', import.meta.url), 'utf8');
 const { version } = JSON.parse(launcher);
 const { SinglePassClusterCopy, launcherValidatedRestorePoint, checkRestorePointStructure } = require('../dist/main/update-restore-point.js');
 const { IncrementalClusterCopy } = require('../dist/main/incremental-restore-point.js');
 const { ColdClusterCopy } = require('../dist/main/database-restore.js');
 assert.equal(version, 10, 'the audited installed factory must cover the shipped launcher');
 assert.equal(launcherValidatedRestorePoint('win32', version, true, false), true);
 const entry = await fs.readFile(new URL('../dist/main/installed-entry.js', import.meta.url), 'utf8');
 for (const [producer, Copy, cached] of [
  ['single-pass', SinglePassClusterCopy, false],
  ['incremental', IncrementalClusterCopy, false],
  ['incremental cache', IncrementalClusterCopy, true],
 ]) for (const corrupt of [null, path.join('global', 'pg_control'), path.join('base', '1', '1259')]) {
  await t.test(`${producer}: ${corrupt ?? 'valid'} contents`, async t => {
   const { f, u, journal } = await rollbackFixture(t);
   const cluster = path.join(f.root, 'postgres');
   for (const [name, body] of [['PG_VERSION', '17'], [path.join('global', 'pg_control'), 'control'], [path.join('base', '1', '1259'), 'rows']]) {
    await fs.mkdir(path.dirname(path.join(cluster, 'data', name)), { recursive: true });
    await fs.writeFile(path.join(cluster, 'data', name), body);
   }
   const copy = new Copy(cluster, async () => {});
   assert.equal(copy.validate, ColdClusterCopy.prototype.validate);
   let retained;
   if (cached) {
    // A cache an older build kept (#1275: discard() no longer keeps one).
    await copy.snapshot(); await fs.rename(path.join(cluster, 'update-snapshot'), path.join(cluster, 'update-copy-cache'));
    retained = await fs.stat(path.join(cluster, 'update-copy-cache', 'data', 'global', 'pg_control'));
   }
   await u.prepareRollback(async () => copy);
   assert.equal((await journal()).phase, 'ready');
   assert.equal((await journal()).revision, B);
   if (cached) assert.equal((await fs.stat(path.join(cluster, 'update-snapshot', 'data', 'global', 'pg_control'))).ino, retained.ino, 'the real producer reuses the retained file');
   // Same names AND same size: structural validation cannot detect either change.
   if (corrupt) {
    const file = path.join(cluster, 'update-snapshot', 'data', corrupt);
    const bytes = await fs.readFile(file);
    await fs.writeFile(file, Buffer.alloc(bytes.length, 'X'));
    await checkRestorePointStructure(cluster);
   }
   const events = [], module = { exports: {} };
   let manager;
   class InstalledUpdates extends SourceUpdates {
    constructor(options) { super({ ...f.options, baseline: options.baseline, launcher: options.launcher }); manager = this; assert.equal(options.launcher, 10); }
    async select(...args) {
     assert.deepEqual(events, ['hash:start', 'hash:done'], 'full validation must finish before selection');
     events.push('select'); return super.select(...args);
    }
   }
   class ObservedCopy extends ColdClusterCopy {
    async validate() { events.push('hash:start'); await super.validate(); events.push('hash:done'); }
   }
   // Run the actual Windows entry and factory, not a reimplementation of their order.
   // Only external Electron/process operations and fixture resource paths are replaced.
   const app = { requestSingleInstanceLock: () => true, releaseSingleInstanceLock: () => {}, getPath: () => f.root, getName: () => 'test', getAppPath: () => f.root,
    quit: () => events.push('quit'), commandLine: { getSwitchValue: () => '' } };
   vm.runInNewContext(entry, { module, exports: module.exports, process: { platform: 'win32', resourcesPath: f.root, argv: [] }, require: id => {
    if (id === 'electron') return { app };
    if (id === 'node:path') return path;
    if (id === 'node:fs') return { promises: { ...fs, readFile: async (file, ...args) => {
     if (String(file).endsWith('desktop-build.json')) return JSON.stringify({ revision: A, sequence: 50, launcher: 4 });
     if (file === path.join(f.root, 'launcher.json')) return launcher;
     if (String(file).endsWith('application-update-key.pem')) return f.options.publicKey;
     return fs.readFile(file, ...args);
    } } };
    if (id === './source-updates') return { SourceUpdates: InstalledUpdates };
    if (id === './database-restore') return { ColdClusterCopy: ObservedCopy };
    if (id === './postgres') return { resolvePostgresBinaries: () => assert.fail('ready validation must not resolve database binaries') };
    if (id === './worker-process') return { quiesceRecordedWorker: () => assert.fail('ready validation must not stop a worker') };
    if (id === './launcher-swap') return require('../dist/main/launcher-swap.js');
    if (id === './installed-host') return { installedExecutable: () => '/test/AI Shuffle.exe', saveInstalledHost: async () => {}, installedLaunchArgs: () => [],
     launchInstalledSource: async (selected, _root, _args, launcher) => { assert.equal(selected.revision, B); assert.equal(launcher, 10); events.push('launch'); } };
    assert.fail('Unexpected installed-entry dependency: ' + id);
   } });
   if (corrupt) {
    await assert.rejects(module.exports.startInstalled(), /validation/);
    assert.deepEqual(events, ['hash:start'], 'corrupt contents never reach selection or launch');
    assert.equal(manager.pendingVersion, B);
    assert.equal((await journal()).phase, 'ready', 'the rejected point is retained');
    assert.equal(JSON.parse(await fs.readFile(path.join(f.root, 'state.json'), 'utf8')).booting, undefined);
   } else {
    await module.exports.startInstalled();
    assert.deepEqual(events, ['hash:start', 'hash:done', 'select', 'launch', 'quit']);
   }
   assert.equal(await fs.readFile(path.join(cluster, 'data', 'global', 'pg_control'), 'utf8'), 'control');
  });
 }
});


test('a corrupt independent marker fails closed and owner-confirmed repair preserves both quarantines',async t=>{
 const f=await fixture(t);const seed=f.create();await seed.load();await f.release(A);await seed.download();await seed.select();await seed.databaseStarting();await seed.healthy();
 await fs.writeFile(path.join(f.root,'database-floor.json'),'{');await fs.writeFile(path.join(f.root,'state.json'),'[]');
 const baseline={revision:C,sequence:200};const declined=await installedLauncher(f,null,{baseline});await declined.start();assert.deepEqual(declined.events,['AI Shuffle needs an installer','quit']);
 const accepted=await installedLauncher(f,null,{baseline,answer:2});await accepted.start();assert.deepEqual(accepted.events,['AI Shuffle needs an installer','bundled']);
 for(const prefix of ['state.json.corrupt-','database-floor.json.corrupt-']){const file=(await fs.readdir(f.root)).find(name=>name.startsWith(prefix));assert(file);assert.equal(await fs.readFile(path.join(f.root,file),'utf8'),prefix.startsWith('state')?'[]':'{');}
});

test('#99: installed Linux refuses a different signed Electron before offering or downloading',async t=>{
 const f=await fixture(t),u=f.create();await u.load();u.useInstalledElectron('44.2.0');
 await f.release(A,undefined,{desktop:{electron:'44.2.0'}});
 assert.equal((await u.check()).kind,'available');
 await f.release(B,undefined,{version:'1.2.0',desktop:{electron:'44.2.1'}});f.requests.length=0;
 for(const run of [()=>u.check(),()=>u.download()])await assert.rejects(run(),/AI Shuffle 1.2.0 needs Electron 44.2.1; this Linux install has 44.2.0.*Install the latest AI Shuffle installer.*Your files and data have been kept.*No update was downloaded/);
 assert.deepEqual(f.requests,Array(2).fill('https://hobbycoders.com/v1/desktop/manifest'));
 assert.equal(f.builds,0);assert.equal(u.pendingVersion,null);
 await assert.rejects(fs.stat(path.join(f.root,'versions',B)),{code:'ENOENT'});
 // Windows/profile managers never opt in; old manifests remain compatible.
 assert.equal((await f.create().check()).kind,'available');
 await f.release(C);assert.equal((await u.check()).kind,'available');
});

test('legacy pending with unverifiable signed provenance stays fenced under disabled update policy',async t=>{
 const {f}=await migratedPair(t);await fs.writeFile(path.join(f.root,'state.json'),JSON.stringify(legacyRetriedState()));await fs.unlink(path.join(f.root,'versions',B,'release.json'));await fs.writeFile(path.join(f.root,'allow-updates.json'),JSON.stringify({allow_updates:false}));
 const u=f.create();await u.load();assert.deepEqual(JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8')).mayHaveMigrated,{[B]:null});await assert.rejects(u.select());await assert.rejects(f.create().attach(A),/may have been migrated/);await u.useNewerBaseline();assert.equal(u.hasMigrationRisk,true);
});

test('restore journal: bundled-baseline first-start failure returns to the bundled code, never a fake source directory',async t=>{
 const {f,u,factory}=await rollbackFixture(t,{baseline:true});
 await u.prepareRollback(factory); await u.select(); await u.databaseStarting();
 const restart=f.create(); await restart.load(); await restart.recoverRollback(factory);
 assert.equal(restart.version,A); assert.equal(restart.hasSourceState,false); assert.equal(restart.hasMigrationRisk,false);
 const launcher=await installedLauncher(f); await launcher.start(); assert.ok(launcher.events.includes('bundled')); assert.ok(!launcher.events.includes(B));
});

test('a compatibility preflight failure never writes booting or blocks the next installed launch',async t=>{
 const f=await fixture(t);await f.release(A);const u=f.create();await u.load();await u.download();
 await assert.rejects(u.select(async()=>{throw Error('Installer required');}),/Installer required/);
 let state=JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8'));assert.equal(state.booting,undefined);assert.equal(state.pending,A);
 const next=f.create();await next.load();assert.equal(next.blocked,false);
 next.stageDownloads();await next.cancelInstall();state=JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8'));
 assert.equal(state.pending,undefined);assert.equal(state.staged,A);assert.equal(next.hasSourceState,false);
});

test('installer baseline rejects source metadata older than its bundled build',async t=>{
 const f=await fixture(t);await f.release(B);
 const u=new SourceUpdates({...f.options,baseline:{revision:A,sequence:200}});await u.load();
 assert.equal((await u.check()).kind,'current');assert.equal(f.builds,0);
});

import { assert, generateKeyPairSync, createHash, sign, execFileSync, fs, os, path, createRequire, vm, fileURLToPath, test, afterRename, require, SourceUpdates, RestorePointError, InstallerRequiredError, verifySourceManifest, treeDigest, requiredLauncher, startLauncher, buildEnvironment, removeSourceDirectory, extractor, repoScripts, publishedTree, LegacyUpdates, OldUpdates, A, B, C, E, KEY, fixture, installedLauncher, rollbackFixture, oldLauncherUpdate, legacyRetriedState, migratedPair, legacyFailedPair, stagedOver } from './helpers/source-updates-fixture.mjs';

test('older launcher: leftovers of an earlier late copy are removed before a new one, or the start stays closed',async t=>{
 const latePoint=phase=>({schemaVersion:1,phase,revision:B,previous:A,state:{schemaVersion:1,active:A},floor:{revision:A,sequence:100},late:true});
 for(const phase of ['preparing','ready'])await t.test(`an unfenced ${phase} copy (the app stopped before fencing; A may have run since)`,async t=>{
  const o=await oldLauncherUpdate(t);await fs.writeFile(o.journalFile,JSON.stringify(latePoint(phase)));
  await (await o.child()).requireRestorePoint(undefined,o.late);
  assert.deepEqual(o.calls,['discard','snapshot'],'the possibly stale copy is replaced');assert.equal((await o.journal()).phase,'ready');
 });
 await t.test('the previous build runs instead (killed during the copy) and removes the abandoned copy once healthy',async t=>{
  const o=await oldLauncherUpdate(t);await o.child();await fs.writeFile(o.journalFile,JSON.stringify(latePoint('preparing')));
  const launcher=new OldUpdates(o.f.options);await launcher.load();assert.equal((await launcher.select()).revision,A,'0.9.6 runs A after an unstarted first start');
  const a=new SourceUpdates({...o.f.options,baseline:undefined});await a.attach(A);await a.healthy();
  await a.finishRollback(o.late.database);
  assert.deepEqual(o.calls,['discard']);await assert.rejects(fs.stat(o.journalFile),{code:'ENOENT'});
  assert.equal(JSON.parse(await fs.readFile(path.join(o.f.root,'state.json'),'utf8')).staged,B,'B stays ready to install');
 });
 for(const valid of [true,false])await t.test(`fenced but not yet ready, copy ${valid?'intact':'damaged'}`,async t=>{
  const o=await oldLauncherUpdate(t);await (await o.child()).requireRestorePoint(undefined,o.late);
  await fs.writeFile(o.journalFile,JSON.stringify({...await o.journal(),phase:'preparing'}));
  if(!valid)o.db.validate=async()=>{o.calls.push('validate');throw Error('Database restore point failed validation');};
  const again=await o.child();await again.requireRestorePoint(undefined,o.late);
  assert.deepEqual(o.calls,valid?['snapshot','validate']:['snapshot','validate','discard','snapshot']);
  assert.equal((await o.journal()).phase,'ready');assert.equal(o.preparing,2);
 });
 await t.test('cleanup blocked',async t=>{
  const o=await oldLauncherUpdate(t);const {snapshot,discard}=o.db;
  o.db.snapshot=async()=>{throw Error('disk full');};o.db.discard=async()=>{throw Error('EBUSY: locked');};
  await assert.rejects((await o.child()).requireRestorePoint(undefined,o.late),/restore point: disk full\./);
  assert.equal((await o.journal()).phase,'committed','never restorable, only cleanup');
  await assert.rejects((await o.child()).requireRestorePoint(undefined,o.late),/restore point: EBUSY: locked\. A file in the AI Shuffle data folder is in use\. Close other programs/);
  o.db.snapshot=snapshot;o.db.discard=discard;
  await (await o.child()).requireRestorePoint(undefined,o.late);assert.equal((await o.journal()).phase,'ready');
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

test('older launcher: interleavings around the fence (pinned from review probes)',async t=>{
 await t.test('floor written but state not saved: 0.9.6 fences the update and its retry finishes the copy',async t=>{
  const o=await oldLauncherUpdate(t);const child=await o.child();
  child.databaseStarting=async function(){await this.writeMigrationFloor(B,101);throw Error('power loss');};
  await assert.rejects(child.requireRestorePoint(undefined,o.late),/power loss/);
  assert.equal((await o.journal()).phase,'preparing');
  const launcher=new OldUpdates(o.f.options);await launcher.load();assert.equal(launcher.blocked,true);
  await assert.rejects(launcher.select(),/No healthy source build|older code cannot run/);
  assert.equal(await launcher.retryFailed(),true);assert.equal((await launcher.select()).revision,B);
  await (await o.child()).requireRestorePoint(undefined,o.late);
  assert.equal((await o.journal()).phase,'ready');assert.deepEqual(o.calls,['snapshot','validate']);
 });
 await t.test('fenced but still preparing, then a current installer: no restore, back to A, B marked failed',async t=>{
  const o=await oldLauncherUpdate(t);await (await o.child()).requireRestorePoint(undefined,o.late);
  await fs.writeFile(o.journalFile,JSON.stringify({...await o.journal(),phase:'preparing'}));
  await new OldUpdates(o.f.options).load();
  const current=new SourceUpdates({...o.f.options,baseline:{revision:C,sequence:50}});await current.load();
  assert.equal(await current.recoverRollback(o.late.database),true);
  assert.deepEqual(o.calls,['snapshot','discard'],'B never started PostgreSQL, so nothing is restored');
  assert.deepEqual(await o.floor(),{schemaVersion:1,revision:A,sequence:100});
  assert.ok(JSON.parse(await fs.readFile(path.join(o.f.root,'state.json'),'utf8')).failed.includes(B));
  assert.equal((await current.select()).revision,A);
 });
});

test('a blocked launch can run the newest failed build again, never an older one',async t=>{
 const f=await fixture(t);await f.release(A);let u=f.create();await u.load();await u.download();await u.select();await u.healthy();
 await f.release(B);await u.download();u=f.create();await u.load();await u.select();
 u=f.create();await u.load();assert.equal(u.blocked,true);
 assert.equal(await u.retryFailed(),true);assert.equal(u.blocked,false);
 assert.equal((await u.select()).revision,B);await u.healthy();assert.equal(u.version,B);
 assert.equal(await u.retryFailed(),false,'nothing is blocked any more');
});

test('ready-journal launcher preparation reads every snapshot byte once without recopying', async t => {
 const { SinglePassClusterCopy } = require('../dist/main/update-restore-point.js');
 const { ColdClusterCopy } = require('../dist/main/database-restore.js');
 const { f, u } = await rollbackFixture(t);
 const postgres = path.join(f.root, 'postgres');
 const mib = Number(process.env.SHUFFLE_ROLLBACK_BENCH_MIB || 8);
 assert(Number.isSafeInteger(mib) && mib > 0 && mib <= 1024);
 const block = Buffer.alloc(1024 * 1024, 0x5a);
 for (const [name, body] of Object.entries({ 'PG_VERSION': '17\n', 'global/pg_control': 'control' })) {
  await fs.mkdir(path.dirname(path.join(postgres, 'data', name)), { recursive: true });
  await fs.writeFile(path.join(postgres, 'data', name), body);
 }
 const rows = path.join('base', '16384', '2619');
 await fs.mkdir(path.dirname(path.join(postgres, 'data', rows)), { recursive: true });
 const file = await fs.open(path.join(postgres, 'data', rows), 'w');
 try { for (let i = 0; i < mib; i++) await file.write(block); } finally { await file.close(); }
 // Produce the ready journal through the app's real snapshot path, not seeded metadata.
 await u.prepareRollback(async () => new SinglePassClusterCopy(postgres, async () => {}));
 const snapshot = path.join(postgres, 'update-snapshot', 'data');
 const nativeFs = require('node:fs');
 const readStream = nativeFs.createReadStream;
 let bytes = 0, reads = 0, copies = 0, validations = 0;
 t.mock.method(nativeFs, 'createReadStream', (name, ...args) => {
  const stream = readStream(name, ...args);
  if (String(name).startsWith(snapshot + path.sep)) {
   reads++;
   stream.on('data', chunk => { bytes += chunk.length; });
  }
  return stream;
 });
 const copyFile = fs.copyFile;
 t.mock.method(fs, 'copyFile', (...args) => { copies++; return copyFile(...args); });
 const database = new ColdClusterCopy(postgres, async () => assert.fail('ready point must not quiesce or copy again'));
 const validate = database.validate.bind(database);
 t.mock.method(database, 'validate', async () => { validations++; await validate(); });
 const times = [];
 for (let i = 0; i < 3; i++) {
  const launcher = f.create(); await launcher.load();
  bytes = 0; reads = 0; validations = 0;
  const start = performance.now();
  assert.equal(await launcher.recoverRollback(async () => database), false);
  await launcher.prepareRollback(async () => database);
  times.push(Number((performance.now() - start).toFixed(2)));
  assert.equal(validations, 1);
  assert.equal(reads, 3);
  assert.equal(bytes, mib * block.length + Buffer.byteLength('17\ncontrol'));
  assert.equal(copies, 0);
 }
 t.diagnostic(JSON.stringify({ fixtureMiB: mib, readyPreparationMs: times, snapshotReads: reads, snapshotBytes: bytes, copies }));
 // Same-size corruption near the end must be caught, not trusted from structure/manifest.
 const damaged = await fs.open(path.join(snapshot, rows), 'r+');
 try { await damaged.write(Buffer.from('!'), 0, 1, mib * block.length - 1); } finally { await damaged.close(); }
 const launcher = f.create(); await launcher.load();
 await assert.rejects(launcher.prepareRollback(async () => database), /failed validation/);
 assert.equal(launcher.pendingVersion, B, 'validation failure leaves the update unselected');
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

test('older launcher: a bundled previous build starts under the migration fence, never a restore point no launcher could use',async t=>{
 for(const outcome of ['healthy','crash'])await t.test(outcome,async t=>{
  const o=await oldLauncherUpdate(t,{baseline:true});const late={...o.late,database:async()=>assert.fail('no copy for a bundled previous build')};
  const child=await o.child();await child.requireRestorePoint(undefined,late);
  assert.equal(o.preparing,0);await assert.rejects(fs.stat(o.journalFile),{code:'ENOENT'});
  await child.databaseStarting();
  if(outcome==='healthy'){await child.healthy();assert.equal(child.version,B);return;}
  await child.failed();
  const launcher=new OldUpdates(o.f.options);await launcher.load();assert.equal(launcher.blocked,true);
  await assert.rejects(launcher.select(),/No healthy source build/);
  // A newer installer is not stranded behind an unusable restore point.
  const installer=new SourceUpdates({...o.f.options,baseline:{revision:E,sequence:300}});await installer.load();
  assert.equal(await installer.recoverRollback(late.database),false);await installer.useNewerBaseline();
  assert.equal(installer.version,E);assert.equal(installer.blocked,false);
 });
});

test('restore journal: a corrupt journal or missing previous build never clears the migration fence',async t=>{
 const {f,u,factory}=await rollbackFixture(t);
 await u.prepareRollback(factory); await u.select(); await u.databaseStarting(); await u.failed();
 await fs.rm(path.join(f.root,'versions',A,'build.json'));
 await assert.rejects(u.recoverRollback(factory)); assert.equal(u.hasMigrationRisk,true);
 await fs.writeFile(path.join(f.root,'restore-point.json'),'{}');
 await assert.rejects(u.recoverRollback(factory),/Invalid database restore journal/); assert.equal(u.hasMigrationRisk,true);
});

test('a failed first start that may have migrated the database and was not put back is never offered again',async t=>{
 const {retryableStarts}=require('../dist/main/failed-start.js');
 const f=await fixture(t);const u=f.create();await u.load();
 await f.release(A);await u.download();await u.select();await u.databaseStarting();await u.healthy();
 // A launcher without restore points: no journal, so the failure stays fenced.
 await f.release(B);await u.download();await u.select();
 const next=f.create();await next.load();
 assert.equal(next.blocked,true);assert.deepEqual(await retryableStarts(f.root),[]);
});

test('invalid check metadata fails before any archive request and can be retried',async t=>{
 const f=await fixture(t);const release=await f.release(A);const u=f.create();await u.load();
 f.signed='{}';await assert.rejects(u.check(),/Unsigned/);assert.equal(f.builds,0);
 assert.deepEqual(f.requests,['https://hobbycoders.com/v1/desktop/manifest']);
 f.signed=release.signed;assert.equal((await u.check()).kind,'available');assert.equal(f.builds,0);
});

test('Python dependencies install only from the hash-pinned locks, like the container updater',async t=>{
 const {pythonInstallSteps,pythonDependencyKey,buildSource}=require('../dist/main/source-build.js');
 const steps=pythonInstallSteps();
 assert(!steps[1].includes('--no-deps'));assert(steps[1].includes('--only-binary=pynput,evdev-binary'));assert(steps[1].includes('desktop/vendor'));
 assert.deepEqual(steps[2],['-P','app/core/voice_dependencies.py','--install']);
 const pipSteps=steps.filter((_,i)=>i!==2);
 assert.deepEqual(pipSteps.map(args=>args.slice(-3)),[['--require-hashes','-r','requirements.lock'],['--require-hashes','-r','requirements-native.lock'],['--no-deps','--no-build-isolation','./fluid']]);
 for(const args of pipSteps){assert.deepEqual(args.slice(0,3),['-m','pip','install']);assert(!args.includes('requirements.txt')&&!args.includes('requirements-native.txt'));}
 // The bundled runtime is reused only when it was installed from the same locks.
 const {pythonDependencies}=await import('../scripts/runtime-contract.mjs');
 const repo=fileURLToPath(new URL('../..',import.meta.url));
 assert.equal(await pythonDependencyKey(repo),await pythonDependencies(repo));
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'shuffle-lock-test-'));t.after(()=>fs.rm(root,{recursive:true,force:true,maxRetries:10,retryDelay:100}));
 const source=path.join(root,'source');await fs.mkdir(path.join(source,'fluid'),{recursive:true});
 for(const name of ['requirements.txt','requirements-native.txt','requirements.lock','fluid/pyproject.toml'])await fs.writeFile(path.join(source,name),'x');
 const log=path.join(root,'build.log');const runtime={root,python:'python-not-run',node:'node-not-run',npm:'npm',pythonDependencies:''};
 await assert.rejects(buildSource({source,server:path.join(root,'server'),cache:path.join(root,'cache'),runtime,revision:A,log,progress(){}}),/no hash-pinned requirements-native\.lock/);
 await assert.rejects(fs.stat(log),{code:'ENOENT'},'nothing ran before the refusal');
});

test('#99: optional desktop.electron must be an exact locked version',async()=>{
 const {privateKey,publicKey:key}=generateKeyPairSync('ed25519');const publicKey=key.export({type:'spki',format:'pem'});
 for(const electron of ['44.2.0',null,44,'^44.2.0','44.2',{},'44.2.0-beta.1']){
  const payload=Buffer.from(JSON.stringify({format:2,revision:A,sequence:5,sha256:KEY,size:10,desktop:{electron}}));
  const signed=JSON.stringify({payload:payload.toString('base64'),signature:sign(null,payload,privateKey).toString('base64')});
  if(electron==='44.2.0')assert.equal(verifySourceManifest(signed,publicKey).desktop.electron,electron);
  else assert.throws(()=>verifySourceManifest(signed,publicKey),/release fields/);
 }
});

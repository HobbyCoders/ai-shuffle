import { assert, generateKeyPairSync, createHash, sign, execFileSync, fs, os, path, createRequire, vm, fileURLToPath, test, afterRename, require, SourceUpdates, RestorePointError, InstallerRequiredError, verifySourceManifest, treeDigest, requiredLauncher, startLauncher, buildEnvironment, removeSourceDirectory, extractor, repoScripts, publishedTree, LegacyUpdates, OldUpdates, A, B, C, E, KEY, fixture, installedLauncher, rollbackFixture, oldLauncherUpdate, legacyRetriedState, migratedPair, legacyFailedPair, stagedOver } from './helpers/source-updates-fixture.mjs';

test('real main missing retry metadata cannot recover below any known healthy or migration floor',async t=>{
 for(const evidence of ['healthy manifest','migration entry','database marker'])for(const sequence of (evidence==='healthy manifest'?[50,100]:[50,100,150]))await t.test(`${evidence}, installer=${sequence}`,async t=>{
  const {f}=await legacyFailedPair(t);await fs.rm(path.join(f.root,'versions',B),{recursive:true,maxRetries:10,retryDelay:100});
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

test('#98: when the newer download fails, the staged build stays ready; an unreachable feed keeps it too',async t=>{
 const {f,u}=await stagedOver(t);
 await f.release(C);f.fail=true;
 await assert.rejects(u.download(),/Local build failed/);
 // The same holds without stageDownloads (a pending download): it stays pending.
 {const g=await fixture(t);await g.release(A);const p=g.create();await p.load();await p.download();await p.select();await p.databaseStarting();await p.healthy();
  await g.release(B);await p.download();await g.release(C);g.fail=true;await assert.rejects(p.download(),/Local build failed/);
  assert.equal(JSON.parse(await fs.readFile(path.join(g.root,'state.json'),'utf8')).pending,B);
  assert((await fs.stat(path.join(g.root,'versions',B,'release.json'))).isFile());}
 assert.equal(u.pendingVersion,B);assert.equal(JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8')).staged,B);
 assert((await fs.stat(path.join(f.root,'versions',B,'release.json'))).isFile());
 await assert.rejects(fs.stat(path.join(f.root,'versions',C)),{code:'ENOENT'});
 f.signed='{}';assert.deepEqual(await u.check(),{kind:'ready',version:B.slice(0,12),release:'1.0.0',notes:'Notes for bbbb'});
 // Nothing newer, or nothing this launcher can start: B stays the offer.
 f.fail=false;await f.release(E,undefined,{desktop:{launcher:9,start:9}});f.options.launcher=3;
 assert.equal((await u.check()).version,B.slice(0,12));
});

test('older launcher: a crash after the late restore point never runs older code; a current installer restores it',async t=>{
 for(const recovery of ['0.9.6 retry','current installer'])await t.test(recovery,async t=>{
  const o=await oldLauncherUpdate(t);const child=await o.child();
  await child.requireRestorePoint(undefined,o.late);await child.databaseStarting();await child.failed();
  if(recovery==='0.9.6 retry'){
   // 0.9.6 ignores restore-point.json. It keeps the migration fence and offers only B again.
   const launcher=new OldUpdates(o.f.options);await launcher.load();assert.equal(launcher.blocked,true);
   await assert.rejects(launcher.select(),/No healthy source build/);
   assert.equal(await launcher.retryFailed(),true);assert.equal((await launcher.select()).revision,B);
   assert.equal((await o.journal()).phase,'ready','the restore point is kept');
   const again=await o.child();await again.requireRestorePoint(undefined,o.late);
   assert.deepEqual(o.calls,['snapshot'],'the fenced retry makes no second copy');
   await again.databaseStarting();await again.healthy();await again.finishRollback(o.late.database);
   assert.deepEqual(o.calls,['snapshot','discard']);await assert.rejects(fs.stat(o.journalFile),{code:'ENOENT'});
  } else {
   const current=new SourceUpdates({...o.f.options,baseline:{revision:C,sequence:50}});await current.load();
   assert.equal(await current.recoverRollback(o.late.database),true);
   assert.deepEqual(o.calls,['snapshot','restore','discard']);assert.deepEqual(o.phases,['preparing','restoring']);
   assert.equal((await current.select()).revision,A);assert.equal(current.hasMigrationRisk,false);
   assert.deepEqual(await o.floor(),{schemaVersion:1,revision:A,sequence:100});
   assert.match(await current.rollbackNotice(),/failed to start.*went back to/);
  }
 });
});

test('checks verify metadata only; explicit download stages the signed release and a newer release replaces it',async t=>{
 const f=await fixture(t);await f.release(A);const u=f.create();await u.load();
 await u.download();await u.select();await u.healthy();const old=await f.release(B);
 f.requests.length=0;const builds=f.builds;const progress=[];
 assert.deepEqual(await u.check((...args)=>progress.push(args)),{kind:'available',version:B.slice(0,12),release:old.manifest.version,notes:old.manifest.notes});
 assert.deepEqual(f.requests,['https://hobbycoders.com/v1/desktop/manifest']);assert.equal(f.builds,builds);assert.deepEqual(progress,[]);
 assert.equal(u.pendingVersion,null);await assert.rejects(fs.stat(path.join(f.root,'staging')), {code:'ENOENT'});
 assert.deepEqual(await u.download(),{kind:'ready',version:B.slice(0,12),release:old.manifest.version,notes:old.manifest.notes});
 assert.equal(f.requests.filter(url=>url===old.url).length,1);assert.equal(f.builds,builds+1);
 assert.equal(await fs.readFile(path.join(f.root,'versions',B,'release.json'),'utf8'),old.signed);
 // #98: a pending download that never ran (no stageDownloads) no longer hides a newer release.
 const newer=await f.release(C);f.requests.length=0;
 assert.deepEqual(await u.check(),{kind:'available',version:C.slice(0,12),release:newer.manifest.version,notes:newer.manifest.notes});
 assert.equal(u.pendingVersion,B,'a check downloads nothing');assert.equal(f.builds,builds+1);
 assert.equal((await u.download()).version,C.slice(0,12));assert.equal(u.pendingVersion,C);
 const state=JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8'));assert.equal(state.pending,C);assert.equal(state.staged,undefined);
 await assert.rejects(fs.stat(path.join(f.root,'versions',B)),{code:'ENOENT'});
 const restarted=f.create();await restarted.load();f.requests.length=0;
 assert.equal((await restarted.check()).kind,'ready');assert.deepEqual(f.requests,['https://hobbycoders.com/v1/desktop/manifest']);
 assert.equal(f.builds,builds+2);assert.equal((await restarted.select()).revision,C);
});

test('#98: a staged build does not hide a newer release; a download replaces it and removes its files',async t=>{
 const {f,u}=await stagedOver(t);
 await f.release(C,undefined,{version:'1.0.1'});f.requests.length=0;const builds=f.builds;
 assert.deepEqual(await u.check(),{kind:'available',version:C.slice(0,12),release:'1.0.1',notes:'Notes for cccc'});
 assert.equal(u.pendingVersion,B,'a check downloads nothing; B stays installable');assert.equal(f.builds,builds);
 assert.deepEqual(await u.download(),{kind:'ready',version:C.slice(0,12),release:'1.0.1',notes:'Notes for cccc'});
 const state=JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8'));
 assert.equal(state.staged,C);assert.equal(state.pending,undefined);assert.equal(u.pendingVersion,C);
 await assert.rejects(fs.stat(path.join(f.root,'versions',B)),{code:'ENOENT'});
 assert((await fs.stat(path.join(f.root,'versions',A))).isDirectory(),'the running build stays');
 await u.prepareInstall();const restarted=f.create();await restarted.load();assert.equal((await restarted.select()).revision,C);
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

test('restore journal: baseline C supersedes unused A-to-B snapshot without ever restoring it',async t=>{
 const {f,u,factory,calls,journal}=await rollbackFixture(t);await u.prepareRollback(factory);
 f.options.baseline={revision:C,sequence:300};
 const installed=f.create();await installed.load();await installed.recoverRollback(factory);
 assert.equal((await journal()).phase,'ready');
 await installed.useNewerBaseline();
 assert.equal((await journal()).phase,'committed','cancel is durable before C can start');
 await installed.databaseStarting();await installed.healthy();await installed.finishRollback(factory);
 assert.deepEqual(calls,['snapshot','discard']);
 assert.equal(JSON.parse(await fs.readFile(path.join(f.root,'database-floor.json'))).sequence,300);
 assert.equal(installed.version,C);
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

test('older launcher: after the refused start, a current installer\'s launcher retries the staged update with a normal restore point',async t=>{
 const o=await oldLauncherUpdate(t);
 // What 1.0.0 did: no late copy, so it refused before PostgreSQL started.
 await assert.rejects((await o.child()).requireRestorePoint(),/No verified database restore point/);
 // The owner installs a current installer; its launcher keeps A and the update staged.
 const installer=async()=>{const launch=await installedLauncher(o.f,null,{baseline:{revision:C,sequence:50}});await launch.start();return launch;};
 assert.deepEqual((await installer()).events,[A]);
 const state=JSON.parse(await fs.readFile(path.join(o.f.root,'state.json'),'utf8'));
 assert.equal(state.active,A);assert.equal(state.staged,B);assert.equal(state.booting,undefined);
 // A (0.9.6) runs, then the owner chooses the staged update again.
 const a=new OldUpdates(o.f.options);await a.attach(A);await a.databaseStarting();a.stageDownloads();await a.prepareInstall();
 assert.deepEqual((await installer()).events,['quiesce','snapshot',B]);
 const child=await o.child();await child.requireRestorePoint(undefined,{database:async()=>assert.fail('the launcher made the point')});
 await child.databaseStarting();assert.equal(JSON.parse(await fs.readFile(o.journalFile,'utf8')).phase,'ready');
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

test('discardStaged never forgets active, previous, pending or possibly migrated failed code',async t=>{
 const f=await fixture(t);
 for(const protectedState of [{active:B},{previous:B},{pending:B},{failed:[B]},{booting:B},{blocked:true}]){
  await fs.mkdir(path.join(f.root,'versions'),{recursive:true});await fs.writeFile(path.join(f.root,'state.json'),JSON.stringify({staged:B,...protectedState}));
  const loader=f.create();await loader.load();assert.equal(await loader.discardStaged(B),false);assert.equal(loader.pendingVersion,B);
 }
});

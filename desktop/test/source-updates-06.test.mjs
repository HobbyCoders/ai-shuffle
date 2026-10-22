import { assert, generateKeyPairSync, createHash, sign, execFileSync, fs, os, path, createRequire, vm, fileURLToPath, test, afterRename, require, SourceUpdates, RestorePointError, InstallerRequiredError, verifySourceManifest, treeDigest, requiredLauncher, startLauncher, buildEnvironment, removeSourceDirectory, extractor, repoScripts, publishedTree, LegacyUpdates, OldUpdates, A, B, C, E, KEY, fixture, installedLauncher, rollbackFixture, oldLauncherUpdate, legacyRetriedState, migratedPair, legacyFailedPair, stagedOver } from './helpers/source-updates-fixture.mjs';

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

test('#98: a build selected to install or fenced is never replaced and the feed is not read',async t=>{
 for(const step of ['selected','fenced']){
  const {f,u,b}=await stagedOver(t);let manager=u;
  if(step==='selected')await u.prepareInstall();
  else{
   // A staged retry of a build that may already have migrated the database (its fence survives).
   const state=JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8'));
   await fs.writeFile(path.join(f.root,'state.json'),JSON.stringify({...state,mayHaveMigrated:{[B]:b.manifest.sequence},blocked:true}));
   manager=f.create();await manager.load();manager.stageDownloads();assert.equal(manager.pendingVersion,B);
  }
  await f.release(C);f.requests.length=0;
  assert.equal((await manager.check()).kind,'ready',step);
  assert.equal((await manager.download()).version,B.slice(0,12),step);
  assert.deepEqual(f.requests,[],step);
  await assert.rejects(fs.stat(path.join(f.root,'versions',C)),{code:'ENOENT'});
  assert((await fs.stat(path.join(f.root,'versions',B))).isDirectory(),step);
  await f.release(A);f.requests.length=0;
  assert.equal((await manager.check()).kind,'ready',step);
  assert.equal(manager.pendingVersion,B);assert.deepEqual(f.requests,[],step);
 }
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

test('a restore point the app made before restarting is only checked by the launcher; one from a failed install is never reused',async t=>{
 const {f,u,calls,factory}=await rollbackFixture(t);
 const validated=[];const checking=async previous=>({...await factory(previous),validate:async()=>validated.push(previous),snapshot:async()=>assert.fail('the launcher must not copy again')});
 // The running app copies the database with its window open, then restarts into the launcher.
 await u.prepareRollback(factory);assert.deepEqual(calls,['snapshot']);assert.equal(await u.rollbackVersion(),'0.9.1');
 const launcher=f.create();await launcher.load();
 assert.equal(await launcher.recoverRollback(checking),false);await launcher.prepareRollback(checking);
 assert.deepEqual(validated,[A]);assert.equal((await launcher.select()).revision,B);
 // Another install whose restart fails: the app discards its point before the update stops being pending.
 const g=await rollbackFixture(t);
 g.u.stageDownloads();await g.u.prepareRollback(g.factory);
 await g.u.cancelPreparedRollback(g.factory);await g.u.cancelInstall();
 assert.deepEqual(g.calls,['snapshot','discard']);assert.equal(await g.u.rollbackVersion(),null);
 // The running version used the database again; the next install copies it afresh.
 await g.u.databaseStarting();await g.u.prepareInstall();await g.u.prepareRollback(g.factory);
 assert.deepEqual(g.calls,['snapshot','discard','snapshot']);
});

test('#99: an incompatible newer Electron keeps the staged build ready with a notice',async t=>{
 const {f,u}=await stagedOver(t);u.useInstalledElectron('44.2.0');
 await f.release(C,undefined,{desktop:{electron:'45.0.0'}});f.requests.length=0;
 for(const run of [()=>u.check(),()=>u.download()]){
  const result=await run();assert.equal(result.kind,'ready');assert.equal(result.version,B.slice(0,12));
  assert.equal(result.release,'1.0.0');assert.equal(result.notes,'Notes for bbbb');
  assert.match(result.notice,/needs Electron 45.0.0.*Install the latest AI Shuffle installer.*newer release was not downloaded; the earlier downloaded update remains ready to install/);
  assert.doesNotMatch(result.notice,/No update was downloaded\./);
 }
 assert.equal(u.pendingVersion,B);assert.equal(f.requests.length,2);
 assert((await fs.stat(path.join(f.root,'versions',B,'release.json'))).isFile());
 await u.prepareInstall();assert.equal(JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8')).pending,B);
});

test('#97: a newer start-floor refusal leaves the staged build ready with a notice',async t=>{
 const {f,u}=await stagedOver(t);f.options.launcher=4;
 await f.release(C,undefined,{version:'1.2.0',desktop:{start:5}});f.requests.length=0;
 for(const run of [()=>u.check(),()=>u.download()]){
  const result=await run();assert.equal(result.kind,'ready');assert.equal(result.version,B.slice(0,12));
  assert.match(result.notice,/AI Shuffle 1.2.0 needs a newer desktop launcher.*launcher 4; it needs 5.*newer release was not downloaded; the earlier downloaded update remains ready/);
  assert.doesNotMatch(result.notice,/No update was downloaded\./);
 }
 assert.equal(u.pendingVersion,B);assert.equal(f.requests.length,2);
});

test('disabled updates cannot discard a legacy retried revision and installed Linux launches B, never older A',async t=>{
 const {f,b}=await migratedPair(t);await fs.writeFile(path.join(f.root,'state.json'),JSON.stringify(legacyRetriedState()));await fs.writeFile(path.join(f.root,'allow-updates.json'),JSON.stringify({allow_updates:false}));
 const launch=await installedLauncher(f);await launch.start();assert.deepEqual(launch.events,[B]);assert.equal(launch.manager.hasMigrationRisk,true);
 assert.deepEqual(JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8')).mayHaveMigrated,{[B]:b.manifest.sequence});
 await assert.rejects(f.create().attach(A),/may have been migrated/);await launch.manager.attach(B);await launch.manager.databaseStarting();await launch.manager.healthy();assert.equal(launch.manager.version,B);assert.equal(launch.manager.hasMigrationRisk,false);
});

test('#97: a release the running launcher cannot start is refused before it is offered or downloaded',async t=>{
 const f=await fixture(t);f.options.launcher=3;
 const u=f.create();await u.load();
 // desktop.launcher alone (the swap after a healthy start) never refuses: PR #89's 3 -> 4 path.
 await f.release(A,undefined,{desktop:{launcher:4,start:3}});
 assert.equal((await u.check()).kind,'available');
 await f.release(B,undefined,{version:'1.1.0',desktop:{launcher:5,start:4}});f.requests.length=0;
 for(const run of [()=>u.check(),()=>u.download()]){
  await assert.rejects(run(),error=>error instanceof InstallerRequiredError&&error.code==='installer_required'&&
   /AI Shuffle 1\.1\.0 needs a newer desktop launcher.*launcher 3; it needs 4.*Install the latest AI Shuffle installer.*Your files and data have been kept.*No update was downloaded/.test(error.message));
 }
 assert.deepEqual(f.requests,['https://hobbycoders.com/v1/desktop/manifest','https://hobbycoders.com/v1/desktop/manifest'],'no archive was requested');
 assert.equal(f.builds,0);assert.equal(u.pendingVersion,null);
 await assert.rejects(fs.stat(path.join(f.root,'versions',B)),{code:'ENOENT'});
 // The launcher is never compared without one (profile and development builds), or for releases without desktop.start.
 delete f.options.launcher;assert.equal((await f.create().check()).kind,'available');
 f.options.launcher=0;await f.release(C,undefined,{desktop:{launcher:4}});assert.equal((await f.create().check()).kind,'available');
});

test('restore journal: interruption before baseline floor promotion leaves cleanup-only journal',async t=>{
 const {f,u,factory,calls,journal}=await rollbackFixture(t);await u.prepareRollback(factory);f.options.baseline={revision:C,sequence:300};
 const installed=f.create();await installed.load();
 const undo=afterRename(async(from,to)=>{if(to.endsWith('restore-point.json'))throw Error('power loss');});
 try{await assert.rejects(installed.useNewerBaseline(),/power loss/);}finally{undo();}
 assert.equal((await journal()).phase,'committed');
 const restart=f.create();await restart.load();await restart.recoverRollback(factory);await restart.useNewerBaseline();
 assert.deepEqual(calls,['snapshot','discard']);assert.equal(restart.version,C);
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

test('the desktop tree digest matches the publisher for ordering and non-ASCII paths',async t=>{
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'shuffle-tree-test-'));t.after(()=>fs.rm(root,{recursive:true,force:true,maxRetries:10,retryDelay:100}));
 const names=['z.txt','a-b/x','a.b','a/b','A/B','é/ü.md','日本/語.txt','a/b c','requirements.lock'];
 const files=names.map(name=>[name,createHash('sha256').update('content of '+name).digest('hex')]);
 // ZIP names are case-sensitive even when the test host's filesystem is not.
 const archive=path.join(root,'tree.zip');execFileSync(process.env.PYTHON||'python',['-c','import sys,json,zipfile; z=zipfile.ZipFile(sys.argv[1],"w"); [z.writestr(sys.argv[2]+name,"content of "+name) for name in json.loads(sys.argv[3])]; z.close()',archive,'ai-shuffle-'+A+'/',JSON.stringify(names)]);
 assert.equal(treeDigest(files),publishedTree(archive));assert.equal(treeDigest([...files].reverse()),treeDigest(files));
});

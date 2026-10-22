import { assert, generateKeyPairSync, createHash, sign, execFileSync, fs, os, path, createRequire, vm, fileURLToPath, test, afterRename, require, SourceUpdates, RestorePointError, InstallerRequiredError, verifySourceManifest, treeDigest, requiredLauncher, startLauncher, buildEnvironment, removeSourceDirectory, extractor, repoScripts, publishedTree, LegacyUpdates, OldUpdates, A, B, C, E, KEY, fixture, installedLauncher, rollbackFixture, oldLauncherUpdate, legacyRetriedState, migratedPair, legacyFailedPair, stagedOver } from './helpers/source-updates-fixture.mjs';

test('missing retried B plus corrupted state never delegates to remaining A, including marker-less legacy installs',async t=>{
 for(const legacy of [false,true])for(const text of ['[]','{','{"active":17}','{"schemaVersion":999}']){
  const {f}=await migratedPair(t);const retry=f.create();await retry.load();assert.equal(await retry.retryFailed(),true);
  // Legacy main writes no independent marker, but can have this exact retry state.
  if(legacy)await fs.unlink(path.join(f.root,'database-floor.json'));
  await fs.rm(path.join(f.root,'versions',B),{recursive:true,maxRetries:10,retryDelay:100});await fs.writeFile(path.join(f.root,'state.json'),text);
  const launch=await installedLauncher(f);await launch.start();assert.deepEqual(launch.events,['AI Shuffle needs an installer','quit']);
  assert.equal(launch.manager.recoveryRequired,true);await assert.rejects(launch.manager.select());
  const backup=(await fs.readdir(f.root)).find(name=>name.startsWith('state.json.corrupt-'));assert.equal(await fs.readFile(path.join(f.root,backup),'utf8'),text);
 }
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

test('restore journal: no unprotected first database start, including an older-launcher bundled handoff',async t=>{
 for(const baseline of [false,true]){
  const {f,u}=await rollbackFixture(t,{baseline});await u.select();
  const child=f.create();await child.attach(B);
  await assert.rejects(child.requireRestorePoint(),/No verified database restore point/);
  assert.equal(JSON.parse(await fs.readFile(path.join(f.root,'database-floor.json'))).revision,A);
 }
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

test('disabled account policy blocks downloads and cancels staged activation on offline restart',async t=>{
 const f=await fixture(t);await f.release(A);let u=f.create();await u.load();await u.download();await u.select();await u.healthy();
 await f.release(B);await u.download();
 await fs.writeFile(path.join(f.root,'allow-updates.json'),JSON.stringify({allow_updates:false}));
 await assert.rejects(u.download(),/disabled/);
 u=f.create();await u.load();assert.equal(u.pendingVersion,null);assert.equal((await u.select()).revision,A);
 await fs.writeFile(path.join(f.root,'allow-updates.json'),JSON.stringify({allow_updates:true}));
 assert.equal((await u.download()).kind,'ready');
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

test('older launcher: once the copy is ready the update is fenced, so a stop before PostgreSQL never runs A beside it',async t=>{
 const o=await oldLauncherUpdate(t);const child=await o.child();await child.requireRestorePoint(undefined,o.late);
 const state=JSON.parse(await fs.readFile(path.join(o.f.root,'state.json'),'utf8'));
 assert.equal(state.waiting,undefined);assert.equal(state.mayHaveMigrated[B],101);assert.deepEqual(await o.floor(),{schemaVersion:1,revision:B,sequence:101});
 // The app stops here, before PostgreSQL. 0.9.6 treats it as a failed first start, never as unstarted.
 const launcher=new OldUpdates(o.f.options);await launcher.load();
 await assert.rejects(launcher.select(),/No healthy source build/);assert.equal(await launcher.retryFailed(),true);
 assert.equal((await launcher.select()).revision,B);
 await (await o.child()).requireRestorePoint(undefined,o.late);assert.deepEqual(o.calls,['snapshot']);
});

test('restore journal: a newer bundled installer escapes locked cleanup and removes the obsolete update offer',async t=>{
 const {f,u,factory,db}=await rollbackFixture(t,{baseline:true});u.stageDownloads();await u.prepareRollback(factory);
 db.discard=async()=>{throw Error('locked snapshot');};await u.cancelPreparedRollback(factory);await u.cancelInstall();
 f.options.baseline={revision:C,sequence:300};const installer=f.create();await installer.load();await installer.recoverRollback(factory);await installer.useNewerBaseline();
 assert.equal(installer.version,C);assert.equal(installer.pendingVersion,null);assert.equal(installer.hasSourceState,false);assert.equal(installer.blocked,false);
 assert.equal(JSON.parse(await fs.readFile(path.join(f.root,'database-floor.json'))).sequence,300);
 // Cleanup still cannot bypass a real filesystem lock, but cannot prevent C starting.
 await installer.finishRollback(async()=>{throw Error('unavailable cleanup');});assert.equal(installer.version,C);
 db.discard=async()=>{};await installer.recoverRollback(factory);assert.equal(installer.cleanupError,null);
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

test('cleanup detaches dependency junctions; builds do not inherit credentials',async t=>{
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'shuffle-cache-test-'));t.after(()=>fs.rm(root,{recursive:true,force:true,maxRetries:10,retryDelay:100}));
 const cache=path.join(root,'cache');await fs.mkdir(cache);await fs.writeFile(path.join(cache,'sentinel'),'keep');
 const version=path.join(root,'version');await fs.mkdir(path.join(version,'source/frontend'),{recursive:true});await fs.symlink(cache,path.join(version,'source/frontend/node_modules'),process.platform==='win32'?'junction':'dir');
 await fs.mkdir(path.join(version,'source/mobile'));await fs.symlink(path.join(version,'source/frontend/node_modules'),path.join(version,'source/mobile/node_modules'),process.platform==='win32'?'junction':'dir');
 await removeSourceDirectory(version);assert.equal(await fs.readFile(path.join(cache,'sentinel'),'utf8'),'keep');
 process.env.SHUFFLE_TEST_SECRET='not-for-builds';try{assert.equal(buildEnvironment(process.execPath,'python').SHUFFLE_TEST_SECRET,undefined);}finally{delete process.env.SHUFFLE_TEST_SECRET;}
});

test('#97: desktop.start is validated like desktop.launcher and is absent from older manifests',async()=>{
 const {privateKey,publicKey:key}=generateKeyPairSync('ed25519');const publicKey=key.export({type:'spki',format:'pem'});
 const signed=fields=>{const payload=Buffer.from(JSON.stringify({format:2,revision:A,sequence:5,sha256:KEY,size:10,...fields}));return JSON.stringify({payload:payload.toString('base64'),signature:sign(null,payload,privateKey).toString('base64')});};
 assert.equal(startLauncher(verifySourceManifest(signed({}),publicKey)),0);
 assert.equal(startLauncher(verifySourceManifest(signed({desktop:{launcher:4}}),publicKey)),0);
 assert.equal(startLauncher(verifySourceManifest(signed({desktop:{launcher:4,start:3}}),publicKey)),3);
 for(const start of [-1,1.5,'3',null])assert.throws(()=>verifySourceManifest(signed({desktop:{launcher:4,start}}),publicKey),/release fields/,String(start));
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
 await request('https://hobbycoders.com/v1/desktop/sources/'+A);
 assert.equal(requests[2][1].headers,undefined);
 for(const url of ['https://evil.example/v1/desktop/manifest','https://hobbycoders.com/v1/account','http://hobbycoders.com/v1/desktop/manifest'])await assert.rejects(request(url),/Invalid source/);
 assert.equal(requests.length,3);
});

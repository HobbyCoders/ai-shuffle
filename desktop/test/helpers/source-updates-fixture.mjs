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
import { afterRename } from './crash-rename.mjs';
// Preserve the original test directory as the base for exported require calls.
const require = createRequire(new URL('../source-updates.test.mjs', import.meta.url));
const { SourceUpdates, RestorePointError, InstallerRequiredError, verifySourceManifest, treeDigest, requiredLauncher, startLauncher } = require('../dist/main/source-updates.js');
const { buildEnvironment, removeSourceDirectory } = require('../dist/main/source-build.js');
const extractor = fileURLToPath(new URL('./source-archive.py', import.meta.url));
const repoScripts = fileURLToPath(new URL('../../../scripts', import.meta.url));
// The publisher's own digest (scripts/source_archive.py), so the desktop check is tested against it, not against itself.
const publishedTree = archive => execFileSync(process.env.PYTHON||'python',['-c','import sys; sys.path.insert(0, sys.argv[1]); import source_archive as s; print(s.tree_digest(s.archive_files(sys.argv[2])))',repoScripts,archive],{encoding:'utf8'}).trim();
// Unmodified origin/main producer at 802f93bf20bb96f5f091c0fde895bf879d197a70.
// Keep a fixture so shallow CI checkouts exercise the real legacy writes too.
const legacyModule={exports:{}};
vm.runInNewContext(require('typescript').transpileModule(await fs.readFile(new URL('./legacy-source-updates.ts',import.meta.url),'utf8'),{compilerOptions:{module:require('typescript').ModuleKind.CommonJS,target:require('typescript').ScriptTarget.ES2022,esModuleInterop:true}}).outputText,{module:legacyModule,exports:legacyModule.exports,require:createRequire(new URL('../../dist/main/source-updates.js',import.meta.url)),process,Buffer,console,Response,AbortSignal,setTimeout,clearTimeout});
// Keep the legacy state producer unchanged, but resolve its test runtime with
// the current application's platform-aware paths (the snapshot is Windows-only).
class LegacyUpdates extends legacyModule.exports.SourceUpdates {
 application(revision){return SourceUpdates.prototype.application.call(this,revision);}
}
// The 0.9.6 updater, the last without database restore points. Its launcher
// selects a pending build and hands it over without calling prepareRollback.
const oldModule={exports:{}};
vm.runInNewContext(require('typescript').transpileModule(await fs.readFile(new URL('./restore-point-free-source-updates.ts',import.meta.url),'utf8'),{compilerOptions:{module:require('typescript').ModuleKind.CommonJS,target:require('typescript').ScriptTarget.ES2022,esModuleInterop:true}}).outputText,{module:oldModule,exports:oldModule.exports,require:createRequire(new URL('../../dist/main/source-updates.js',import.meta.url)),process,Buffer,console,Response,AbortSignal,setTimeout,clearTimeout});
const OldUpdates=oldModule.exports.SourceUpdates;
const A='a'.repeat(40), B='b'.repeat(40), C='c'.repeat(40), KEY='d'.repeat(64);
const E='e'.repeat(40);
async function fixture(t) {
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'shuffle-source-test-'));t.after(()=>fs.rm(root,{recursive:true,force:true,maxRetries:10,retryDelay:100}));
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
 vm.runInNewContext(await fs.readFile(new URL('../../dist/main/installed-entry.js',import.meta.url),'utf8'),{module,exports:module.exports,process:{platform:'linux',env:{},versions:{electron:'44.2.0'},resourcesPath:f.root,argv:[]},require:id=>{
  if(id==='electron')return {app,dialog:{showMessageBox:async options=>{events.push(options.title);dialogs.push(options);return {response:answer};}}};
  if(id==='node:path')return path;
  if(id==='node:fs')return {promises:{...fs,readFile:async file=>file.endsWith('desktop-build.json')?JSON.stringify(baseline):file.endsWith('application-update-key.pem')?f.options.publicKey:fs.readFile(file,'utf8')}};
  if(id==='./source-updates')return {SourceUpdates:InstalledUpdates};
  if(id==='./postgres')return {resolvePostgresBinaries:()=>({}),PostgresSupervisor:class {async quiesce(){}}};
  if(id==='./database-restore')return {ColdClusterCopy:class {constructor(_root,quiesce){this.quiesce=quiesce;}async validate(){}async snapshot(){await this.quiesce(true);events.push('snapshot');}async restore(){await this.quiesce(false);events.push('restore');}async discard(){events.push('discard');}}};
  if(id==='./worker-process')return {quiesceRecordedWorker:async()=>events.push('quiesce')};
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
async function oldLauncherUpdate(t,{baseline=false}={}){
 const f=await fixture(t);
 if(baseline)f.options.baseline={revision:C,sequence:50};
 let old=new OldUpdates(f.options);await old.load();
 if(!baseline){await f.release(A);await old.download();await old.select();await old.attach(A);await old.databaseStarting();await old.healthy();}
 else await old.databaseStarting();
 await f.release(B);old=new OldUpdates(f.options);await old.load();await old.download();
 const launcher=new OldUpdates(f.options);await launcher.load();assert.equal((await launcher.select()).revision,B);
 const calls=[],phases=[];let preparing=0;
 const journalFile=path.join(f.root,'restore-point.json'),floorFile=path.join(f.root,'database-floor.json');
 const journal=async()=>JSON.parse(await fs.readFile(journalFile,'utf8'));
 const db={validate:async()=>{calls.push('validate');},snapshot:async()=>{phases.push((await journal()).phase);calls.push('snapshot');},
  restore:async()=>{phases.push((await journal()).phase);calls.push('restore');},discard:async()=>{calls.push('discard');}};
 const late={database:async previous=>{assert.equal(previous,A);return db;},preparing:()=>preparing++};
 const child=async()=>{const c=new SourceUpdates({...f.options,baseline:undefined});await c.attach(B);return c;};
 return {f,db,calls,phases,late,child,journal,journalFile,floor:async()=>JSON.parse(await fs.readFile(floorFile,'utf8')),floorFile,get preparing(){return preparing;}};
}
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
async function stagedOver(t){
 const f=await fixture(t);await f.release(A);const u=f.create();await u.load();await u.download();await u.select();await u.databaseStarting();await u.healthy();
 u.stageDownloads();const b=await f.release(B,undefined,{version:'1.0.0'});assert.equal((await u.download()).kind,'ready');
 assert.equal(JSON.parse(await fs.readFile(path.join(f.root,'state.json'),'utf8')).staged,B);
 return {f,u,b};
}

export { assert, generateKeyPairSync, createHash, sign, execFileSync, fs, os, path, createRequire, vm, fileURLToPath, test, afterRename, require, SourceUpdates, RestorePointError, InstallerRequiredError, verifySourceManifest, treeDigest, requiredLauncher, startLauncher, buildEnvironment, removeSourceDirectory, extractor, repoScripts, publishedTree, LegacyUpdates, OldUpdates, A, B, C, E, KEY, fixture, installedLauncher, rollbackFixture, oldLauncherUpdate, legacyRetriedState, migratedPair, legacyFailedPair, stagedOver };

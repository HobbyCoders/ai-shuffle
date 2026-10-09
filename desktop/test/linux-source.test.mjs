import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { promises as fs, readFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';
import { test } from 'node:test';
import { stampDesktopBuild } from '../scripts/stamp-desktop-build.mjs';
import { reuseBundledPython } from '../scripts/build-worker.mjs';

const desktop=fileURLToPath(new URL('..',import.meta.url));
test('bundled Python reuse requires identical locks, interpreter, platform and architecture',async t=>{
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'shuffle-python-reuse-'));t.after(()=>fs.rm(root,{recursive:true,force:true}));
 const worker=path.join(root,'bundle');const destination=path.join(root,'staged-python');
 const expected={platform:'linux',arch:'x64',pythonDependencies:'a'.repeat(64),python:{version:'3.12.14',build:'20260901',triple:'x86_64-unknown-linux-gnu'}};
 await fs.mkdir(path.join(worker,'python/bin'),{recursive:true});await fs.writeFile(path.join(worker,'python/bin/python3.12'),'bundled interpreter');
 let fileLinks=true;
 try{await fs.symlink('python3.12',path.join(worker,'python/bin/python3'));}
 catch(error){if(process.platform!=='win32'||error.code!=='EPERM')throw error;fileLinks=false;t.diagnostic('File-symlink assertions unavailable without Windows symlink permission');await fs.copyFile(path.join(worker,'python/bin/python3.12'),path.join(worker,'python/bin/python3'));}
 await fs.mkdir(path.join(worker,'python/lib'),{recursive:true});await fs.writeFile(path.join(worker,'python/lib/verified-packages'),'packages installed from hashes');
 const write=manifest=>fs.writeFile(path.join(worker,'manifest.json'),JSON.stringify(manifest));
 for(const manifest of [{...expected,stub:true},{...expected,platform:'win32'},{...expected,arch:'arm64'},{...expected,pythonDependencies:'b'.repeat(64)},{...expected,python:{...expected.python,build:'older'}},{...expected,python:{...expected.python,version:'3.13.0'}}]){
  await write(manifest);assert.equal(await reuseBundledPython(worker,destination,expected),null);
  await assert.rejects(fs.stat(destination),{code:'ENOENT'});
 }
 await write(expected);assert.equal((await reuseBundledPython(worker,destination,expected)).reused,true);
 assert.equal(await fs.readFile(path.join(destination,'lib/verified-packages'),'utf8'),'packages installed from hashes');
 if(fileLinks)assert.equal(await fs.readlink(path.join(destination,'bin/python3')),'python3.12');else assert.equal(await fs.readFile(path.join(destination,'bin/python3'),'utf8'),'bundled interpreter');
 await fs.writeFile(path.join(destination,'lib/verified-packages'),'changed staged copy');
 assert.equal(await fs.readFile(path.join(worker,'python/lib/verified-packages'),'utf8'),'packages installed from hashes','never mutate bundled resources');
 await fs.symlink(process.platform==='win32'?path.join(root,'outside'):'../../../outside',path.join(worker,'python/lib/escape'),process.platform==='win32'?'junction':'dir');
 await assert.rejects(reuseBundledPython(worker,path.join(root,'bad-copy'),expected),process.platform==='win32'?/Bundled symlink is absolute/:/outside resource directory/);
 await fs.writeFile(path.join(worker,'manifest.json'),'{');await assert.rejects(reuseBundledPython(worker,path.join(root,'corrupt'),expected),SyntaxError);
});
test('Python reuse rejects root links and copy-time swaps on Linux and Windows before execution',async t=>{
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'shuffle-root-link-'));t.after(()=>fs.rm(root,{recursive:true,force:true}));
 for(const platform of ['linux','win32']){
  const worker=path.join(root,platform);const source=path.join(worker,'python');const external=path.join(root,'external-'+platform);
  const expected={platform,arch:'x64',pythonDependencies:'a'.repeat(64),python:{version:'3.12.14',build:'20260901',triple:'x86_64-unknown-linux-gnu'}};
  await fs.mkdir(worker);await fs.mkdir(external);await fs.writeFile(path.join(worker,'manifest.json'),JSON.stringify(expected));
  const linkType=process.platform==='win32'?'junction':'dir';await fs.symlink(external,source,linkType);
  await assert.rejects(reuseBundledPython(worker,path.join(root,'initial-'+platform),expected),/root is a link/);
  await fs.unlink(source);await fs.mkdir(source);
  await assert.rejects(reuseBundledPython(worker,path.join(root,'raced-'+platform),expected,async(from,to,options)=>{
   await fs.rename(from,from+'-original');await fs.symlink(external,from,linkType);
   // fs.cp recreates junctions as privileged symlinks on Windows; reproduce
   // the same copied root using an unprivileged junction for this race check.
   if(process.platform==='win32')await fs.symlink(external,to,linkType);else await fs.cp(from,to,options);
  }),/root is a link/,'destination validation rejects the copied external link before returning an executable');
 }
});
test('CLI never executes Python through a rejected bundle root link',{skip:process.platform!=='linux'||process.arch!=='x64'},async t=>{
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'shuffle-no-linked-exec-'));t.after(()=>fs.rm(root,{recursive:true,force:true}));
 const worker=path.join(root,'worker');const external=path.join(root,'external');const sentinel=path.join(root,'executed');
 await fs.mkdir(worker);await fs.mkdir(path.join(external,'bin'),{recursive:true});
 await fs.writeFile(path.join(external,'bin/python3'),`#!/usr/bin/env node\nrequire('node:fs').writeFileSync(${JSON.stringify(sentinel)},'executed');\n`,{mode:0o755});
 const {pythonDependencies}=await import('../scripts/runtime-contract.mjs');
 await fs.writeFile(path.join(worker,'manifest.json'),JSON.stringify({platform:'linux',arch:'x64',pythonDependencies:await pythonDependencies(path.resolve(desktop,'..')),python:{version:'3.12.14',build:'20260901',triple:'x86_64-unknown-linux-gnu'}}));
 await fs.symlink(external,path.join(worker,'python'),'dir');
 assert.throws(()=>execFileSync(process.execPath,['scripts/build-worker.mjs','--runtime-only','--python-from',worker,'--out',path.join(root,'out'),'--postgres-out',path.join(root,'postgres')],{cwd:desktop,stdio:'pipe'}),/root is a link/);
 await assert.rejects(fs.stat(sentinel),{code:'ENOENT'});
});
test('build:worker stamps the trusted baseline on Linux; repeated Windows stamping is identical',async t=>{
 const out=await fs.mkdtemp(path.join(os.tmpdir(),'shuffle-baseline-'));t.after(()=>fs.rm(out,{recursive:true,force:true}));
 // The same npm entry used by the Linux artifact workflow, without downloads.
 execFileSync(process.platform==='win32'?'npm.cmd':'npm',['run','build:worker','--','--stub','--platform','linux','--arch','x64','--out',out],{cwd:desktop,shell:process.platform==='win32'});
 const file=path.join(out,'desktop-build.json');const bytes=await fs.readFile(file,'utf8');const baseline=JSON.parse(bytes);
 assert.equal(baseline.revision,execFileSync('git',['rev-parse','HEAD'],{cwd:desktop,encoding:'utf8'}).trim());
 assert.equal(baseline.sequence,Number(execFileSync('git',['show','-s','--format=%ct','HEAD'],{cwd:desktop,encoding:'utf8'}).trim())*1000);
 await stampDesktopBuild(out,desktop);assert.equal(await fs.readFile(file,'utf8'),bytes);
 const mac=path.join(out,'mac');
 execFileSync(process.platform==='win32'?'npm.cmd':'npm',['run','build:worker','--','--stub','--platform','darwin','--arch','arm64','--out',mac],{cwd:desktop,shell:process.platform==='win32'});
 assert.deepEqual(await fs.readdir(mac),['manifest.json'],'macOS packaging remains unchanged');
});
test('Linux native setup selects bin/python3, bin/node and lib/node_modules/npm from the writable cache',async()=>{
 const module={exports:{}};const spawns=[];const reads=[];
 const code=readFileSync(new URL('../dist/main/source-runtime.js',import.meta.url),'utf8');
 vm.runInNewContext(code,{module,exports:module.exports,process:{platform:'linux',arch:'x64'},require:id=>{
  if(id==='node:path')return path;
  if(id==='node:crypto')return {createHash:()=>({update(){return this;},digest:()=> 'a'.repeat(64)}),randomUUID:()=> 'build-id'};
  if(id==='./source-build')return {buildEnvironment:()=>({})};
  if(id==='node:fs')return {promises:{access:async()=>{throw Error('not cached');},mkdir:async()=>{},rename:async()=>{},rm:async()=>{},open:async()=>({fd:1,close:async()=>{}}),readFile:async file=>{reads.push(file);return file.endsWith('manifest.json')?JSON.stringify({pythonDependencies:'b'.repeat(64)}):'signed source';}}};
  if(id==='node:child_process')return {spawn:(...args)=>{spawns.push(args);const child={once:(event,fn)=>{if(event==='exit')queueMicrotask(()=>fn(0));return child;}};return child;}};
  throw Error('Unexpected module '+id);
 },queueMicrotask});
 const {runtime}=await module.exports.prepareNativeRuntime('/signed/source','/home/me/updates','/opt/AI Shuffle/resources/worker/node/bin/node',()=>{});
 const base=path.join('/home/me/updates','runtimes','a'.repeat(24),'worker');
 assert.equal(runtime.python,path.join(base,'python/bin/python3'));assert.equal(runtime.node,path.join(base,'node/bin/node'));assert.equal(runtime.npm,path.join(base,'node/lib/node_modules/npm/bin/npm-cli.js'));
 assert.equal(spawns[0][0],'/opt/AI Shuffle/resources/worker/node/bin/node');
 assert(spawns[0][1].includes('--runtime-only'));assert(spawns[0][1].includes(path.join('/home/me/updates','downloads')));
 assert.equal(spawns[0][1][spawns[0][1].indexOf('--python-from')+1],path.resolve(path.dirname('/opt/AI Shuffle/resources/worker/node/bin/node'),'../..'));
 assert(reads.includes(path.join('/signed/source','desktop/scripts/build-worker.mjs')));
});

import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
const code=readFileSync(new URL('../dist/main/installed-entry.js',import.meta.url),'utf8');
function fixture({locked=true,source=false,broken=false,blocked=false}={}){
 const events=[];const module={exports:{}};let options;
 const app={requestSingleInstanceLock:()=>{events.push('lock');return locked;},quit:()=>events.push('quit'),
  getPath:()=>path.resolve('existing-profile'),getName:()=> 'existing-name',getAppPath:()=>path.resolve('installed-app'),releaseSingleInstanceLock:()=>events.push('unlock'),commandLine:{getSwitchValue:()=>''}};
 class SourceUpdates{constructor(value){options=value;}hasSourceState=source;blocked=blocked;async load(){events.push('load');}async useNewerBaseline(){events.push('baseline');}async select(){events.push('select');if(broken)throw Error('No healthy source build');return {revision:'b'.repeat(40)};}}
 const host={saveInstalledHost:async(root,value)=>events.push({host:value,root}),installedLaunchArgs:args=>args.filter(x=>x.startsWith('aishuffle:')),launchInstalledSource:async()=>events.push('launch-source')};
 vm.runInNewContext(code,{module,exports:module.exports,process:{execPath:path.resolve('Programs/AI Shuffle.exe'),resourcesPath:path.resolve('resources'),argv:['AI Shuffle.exe','aishuffle://test']},require:id=>{
  if(id==='electron')return {app};if(id==='node:path')return path;
  if(id==='node:fs')return {promises:{readFile:async file=>file.endsWith('desktop-build.json')?JSON.stringify({revision:'a'.repeat(40),sequence:100}):'trusted-public-key'}};
  if(id==='./source-updates')return {SourceUpdates};if(id==='./installed-host')return host;
  if(id==='./index'){events.push('run-bundled');return {};}
  throw Error('Unexpected module '+id);
 }});
 return {entry:module.exports,events,get options(){return options;}};
}
test('initial installed launch is offline and uses bundled code with the existing profile',async()=>{
 const f=fixture();await f.entry.startInstalled();assert.equal(f.events.at(-1),'run-bundled');
 assert.equal(f.entry.installedContext().updates.hasSourceState,false);assert.equal(f.options.root,path.resolve('existing-profile','updates'));
 assert.equal(f.events.find(e=>e?.host)?.host.userData,path.resolve('existing-profile'));
});
test('existing shortcut defers to the running app before interpreting first-boot state',async()=>{
 const f=fixture({locked:false});await f.entry.startInstalled();assert.deepEqual(f.events,['lock','quit']);
});
test('installed EXE selects staged source and releases the instance lock before starting it',async()=>{
 const f=fixture({source:true});await f.entry.startInstalled();assert.deepEqual(f.events.slice(-4),['select','unlock','launch-source','quit']);assert(!f.events.includes('run-bundled'));
});
test('a source build that cannot be selected never silently runs the old bundled app over its database',async()=>{
 const f=fixture({source:true,broken:true});await assert.rejects(f.entry.startInstalled(),/No healthy/);assert(!f.events.includes('run-bundled'));assert(!f.events.includes('launch-source'));
});
test('DESK-02: a blocked newer source never falls back to an older database-incompatible baseline',async()=>{
 const f=fixture({source:true,broken:true,blocked:true});
 await assert.rejects(f.entry.startInstalled(),/No healthy/);
 assert.deepEqual(f.events.map(e=>typeof e==='string'?e:'host'),['lock','load','baseline','host','select']);
 assert(!f.events.includes('run-bundled'));assert(!f.events.includes('launch-source'));
 assert.equal(f.entry.installedContext(),null);
});

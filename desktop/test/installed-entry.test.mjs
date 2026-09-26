import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
const code=readFileSync(new URL('../dist/main/installed-entry.js',import.meta.url),'utf8');
function fixture({locked=true,source=false,broken=false,blocked=false,answer=1}={}){
 const events=[];const module={exports:{}};let options;
 const app={requestSingleInstanceLock:()=>{events.push('lock');return locked;},quit:()=>events.push('quit'),
  getPath:()=>path.resolve('existing-profile'),getName:()=> 'existing-name',getAppPath:()=>path.resolve('installed-app'),releaseSingleInstanceLock:()=>events.push('unlock'),commandLine:{getSwitchValue:()=>''},whenReady:async()=>{}};
 const dialog={showMessageBox:async options=>{events.push({dialog:options});return {response:answer};}};
 class SourceUpdates{constructor(value){options=value;}hasSourceState=source;blocked=blocked;async load(){events.push('load');}async useNewerBaseline(){events.push('baseline');}async retryFailed(){events.push('retry');return true;}async select(){events.push('select');if(broken)throw Error('No healthy source build');return {revision:'b'.repeat(40)};}}
 const host={saveInstalledHost:async(root,value)=>events.push({host:value,root}),installedLaunchArgs:args=>args.filter(x=>x.startsWith('aishuffle:')),launchInstalledSource:async()=>events.push('launch-source')};
 vm.runInNewContext(code,{module,exports:module.exports,process:{execPath:path.resolve('Programs/AI Shuffle.exe'),resourcesPath:path.resolve('resources'),argv:['AI Shuffle.exe','aishuffle://test']},require:id=>{
  if(id==='electron')return {app,dialog};if(id==='node:path')return path;
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
const kinds=events=>events.map(e=>typeof e==='string'?e:e.dialog?'dialog':'host');
test('DESK-02: a blocked newer source never falls back to an older database-incompatible baseline',async()=>{
 const f=fixture({source:true,blocked:true});
 await f.entry.startInstalled();
 // Quit at the prompt: nothing runs, neither the failed build nor the bundled baseline.
 assert.deepEqual(kinds(f.events),['lock','load','baseline','host','dialog','quit']);
 assert.equal(f.entry.installedContext(),null);
 const retried=fixture({source:true,blocked:true,answer:0});
 await retried.entry.startInstalled();
 assert.deepEqual(kinds(retried.events).slice(4),['dialog','retry','select','unlock','launch-source','quit']);
 assert(!retried.events.includes('run-bundled'));
 // Nothing left that can run again: the error, still never the baseline.
 const gone=fixture({source:true,blocked:true,answer:0,broken:true});
 await assert.rejects(gone.entry.startInstalled(),/No healthy/);
 assert(!gone.events.includes('run-bundled'));assert(!gone.events.includes('launch-source'));
});
test('the blocked prompt says why nothing older runs and offers to try the update again',async()=>{
 const f=fixture({source:true,blocked:true});await f.entry.startInstalled();
 const {dialog}=f.events.find(e=>e?.dialog);
 assert.deepEqual([...dialog.buttons],['Try Again','Quit']);assert.equal(dialog.cancelId,1);
 assert.match(dialog.detail,/will not go back to an older version/);
});

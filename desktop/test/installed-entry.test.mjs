import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
const code=readFileSync(new URL('../dist/main/installed-entry.js',import.meta.url),'utf8');
function fixture({locked=true,source=false,broken=false,blocked=false,answer=1,platform='win32',env={},missingStamp=false,badStamp=false,incompatible=false,activeFallback=false,state=null,recovery=false,recovered=false,confirmable=false,retryAvailable=true,installerError=false,build={revision:'a'.repeat(40),sequence:100},launcherJson=null,trial='none'}={}){
 const events=[];const module={exports:{}};let options;let launched;
 const app={requestSingleInstanceLock:()=>{events.push('lock');return locked;},quit:()=>events.push('quit'),
  getPath:()=>path.resolve('existing-profile'),getName:()=> 'existing-name',getAppPath:()=>path.resolve('installed-app'),releaseSingleInstanceLock:()=>events.push('unlock'),commandLine:{getSwitchValue:()=>''},whenReady:async()=>{}};
 const dialog={showMessageBox:async options=>{events.push({dialog:options});return {response:answer};}};
 class SourceUpdates{constructor(value){options=value;}get hasSourceState(){return state?!!(state.active||state.pending):this.sourceState;}sourceState=source;get pendingVersion(){return state?.staged??state?.pending??null;}async application(revision){events.push('application');return {revision,desktopDir:path.resolve('staged-desktop'),node:path.resolve('runtime/node/bin/node')};}blocked=blocked;async load(){events.push('load');}async useNewerBaseline(confirmed=false){events.push('baseline');if(confirmed&&confirmable){this.recoveryRequired=false;this.blocked=false;}}canConfirmBaselineRecovery=confirmable;recoveryRequired=recovery;recoveredState=recovered;async retryFailed(){events.push('retry');return retryAvailable;}async select(preflight){events.push('select');if(broken)throw Error('No healthy source build');const selected={revision:state?(state.pending??state.active):'b'.repeat(40),desktopDir:path.resolve('staged-desktop'),node:path.resolve('runtime/node/bin/node')};await preflight?.(selected);if(preflight&&(!state||state.pending)){events.push('booting');if(state)state.booting=selected.revision;}return selected;}stageDownloads(){events.push('stage');}async cancelInstall(){events.push('cancel');if(state?.pending){state.staged=state.pending;delete state.pending;}this.sourceState=activeFallback;this.cancelled=true;}}
 class InstallerRequired extends Error {}
 const host={InstallerRequired,requireInstalledElectron:async application=>{events.push('preflight');if(incompatible&&(state?application.revision==='b'.repeat(40):!events.includes('cancel')))throw new InstallerRequired('New installer required');},installedExecutable:()=>env.APPIMAGE??path.resolve('Programs/AI Shuffle.exe'),saveInstalledHost:async(root,value)=>events.push({host:value,root}),installedLaunchArgs:args=>args.filter(x=>x.startsWith('aishuffle:')),launchInstalledSource:async(...args)=>{launched=args;events.push('launch-source');}};
 vm.runInNewContext(code,{module,exports:module.exports,process:{platform,arch:'x64',env,versions:{electron:'44.2.0'},execPath:path.resolve('Programs/AI Shuffle.exe'),resourcesPath:path.resolve('resources'),argv:['AI Shuffle.exe','aishuffle://test']},require:id=>{
  if(id==='electron')return {app,dialog,shell:{openExternal:async url=>events.push(['open',url]),showItemInFolder:file=>events.push(['reveal',file])}};if(id==='node:path')return path;
  if(id==='node:fs')return {promises:{readFile:async file=>{
   if(file.endsWith('launcher.json')){if(launcherJson===null)throw Object.assign(Error('ENOENT'),{code:'ENOENT'});return launcherJson;}
   if(!file.endsWith('desktop-build.json'))return 'trusted-public-key';
   if(missingStamp)throw Object.assign(Error('Missing stamp'),{code:'ENOENT'});
   return badStamp?'{}':JSON.stringify(build);
  }}};
  if(id==='./launcher-swap')return {LAUNCHER_FILE:'launcher.json',parseLauncher:text=>JSON.parse(text),
   launcherTrial:(root,own)=>{events.push({trial:own});return trial;},countLauncherStart:()=>events.push('count'),
   restoreLauncher:(root,exe)=>events.push({restore:exe})};
  if(id==='./source-updates')return {SourceUpdates};if(id==='./installed-host')return host;
  if(id==='./installer-updates')return {installerPlatform:()=>platform==='linux'?'linux':null,InstallerUpdates:class {
   constructor(options){events.push(['installer-options',options]);}async check(){events.push('installer-check');if(installerError)throw Error('Offline');return {kind:'available'};}async download(){events.push('installer-download');return {file:'verified-installer.AppImage',version:'0.9.2'};}
  }};
  if(id===path.join(path.resolve('staged-desktop'),'dist/main/managed-entry.js'))return {startManagedSource:async(...args)=>events.push(['delegate',args[3]])};
  if(id==='./index'){events.push('run-bundled');return {};}
  throw Error('Unexpected module '+id);
 }});
 return {entry:module.exports,events,get options(){return options;},get launched(){return launched;}};
}
test('initial installed launch is offline and uses bundled code with the existing profile',async()=>{
 const f=fixture();await f.entry.startInstalled();assert.equal(f.events.at(-1),'run-bundled');
 assert.equal(f.entry.installedContext().updates.hasSourceState,false);assert.equal(f.options.root,path.resolve('existing-profile','updates'));
 assert.equal(f.events.find(e=>e?.host)?.host.userData,path.resolve('existing-profile'));
});
test('Linux baseline uses userData and bundled bin/node, retaining the AppImage relaunch target',async()=>{
 const appimage=path.resolve('Downloads/AI Shuffle.AppImage');
 const f=fixture({platform:'linux',env:{APPIMAGE:appimage}});await f.entry.startInstalled();
 assert.equal(f.options.bootstrapNode,path.resolve('resources/worker/node/bin/node'));
 assert.equal(f.options.baseline.revision,'a'.repeat(40));assert.equal(f.options.baseline.sequence,100);
 assert.equal(f.options.root,path.resolve('existing-profile/updates'));
 assert.equal(f.entry.installedContext().executable,appimage);
 assert.equal(f.events.find(e=>e?.host)?.host.executable,appimage);
 assert.equal(f.events.at(-1),'run-bundled');
 assert(!f.events.some(e=>e?.trial!==undefined),'Linux never enters Windows launcher trials');
 const staged=fixture({platform:'linux',source:true,launcherJson:'not a Windows launcher',trial:'restore'});await staged.entry.startInstalled();
 assert.deepEqual(staged.events.slice(-4),['select','preflight','booting',['delegate',false]]);assert(!staged.events.includes('launch-source'));
});
test('Linux incompatible Electron preflight preserves installer offers without recording a failed boot',async()=>{
 const f=fixture({platform:'linux',source:true,incompatible:true});await f.entry.startInstalled();
 assert.deepEqual(f.events.slice(-6),['select','preflight','stage','cancel','unlock','run-bundled']);
 assert(!f.events.includes('booting'));assert(!f.events.includes('launch-source'));assert.equal(f.entry.installedContext(),null);
 const old=fixture({platform:'linux',source:true,incompatible:true,activeFallback:true});await old.entry.startInstalled();
 assert.deepEqual(old.events.at(-1),['delegate',true]);assert(!old.events.includes('run-bundled'),'never downgrade an active source database');
});
test('second Linux launch rechecks incompatible staged updates with and without an active source; recovery clears installer-only mode',async()=>{
 for(const active of [undefined,'a'.repeat(40)]){
  const state={pending:'b'.repeat(40),...(active?{active}:{})};
  const first=fixture({platform:'linux',state,incompatible:true});await first.entry.startInstalled();
  assert.equal(state.staged,'b'.repeat(40));assert.equal(state.pending,undefined);assert.equal(state.booting,undefined);
  const assertMode=(f,only)=>active?assert.deepEqual(f.events.at(-1),['delegate',only]):assert.equal(f.entry.installedContext().installersOnly,only);
  assertMode(first,true);
  const second=fixture({platform:'linux',state,incompatible:true});await second.entry.startInstalled();assertMode(second,true);assert(second.events.includes('application'));assert(!second.events.includes('booting'));
  const restored=fixture({platform:'linux',state,incompatible:false});await restored.entry.startInstalled();assertMode(restored,false);
  state.staged='c'.repeat(40);const replaced=fixture({platform:'linux',state,incompatible:true});await replaced.entry.startInstalled();assertMode(replaced,false);
 }
});
test('unstamped Linux keeps the bundled/installer path; malformed stamps fail closed',async()=>{
 const f=fixture({platform:'linux',missingStamp:true});await f.entry.startInstalled();
 assert.deepEqual(f.events,['lock','unlock','run-bundled']);assert.equal(f.entry.installedContext(),null);assert.equal(f.options,undefined);
 await assert.rejects(fixture({platform:'linux',badStamp:true}).entry.startInstalled(),/Invalid installed desktop revision/);
 await assert.rejects(fixture({missingStamp:true}).entry.startInstalled(),/Missing stamp/);
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
const kinds=events=>events.filter(e=>e?.trial===undefined).map(e=>typeof e==='string'?e:e.dialog?'dialog':'host');
test('unrecoverable state offers an installer without loading source or bundled worker code',async()=>{
 const f=fixture({platform:'linux',recovery:true});await f.entry.startInstalled();
 assert.deepEqual(kinds(f.events),['lock','load','baseline','dialog','quit']);const prompt=f.events.find(e=>e.dialog).dialog;
 assert.equal(prompt.buttons[0],'Get Latest Installer');assert.match(prompt.detail,/data have been kept/);assert.match(prompt.detail,/next launch/);assert.equal(f.entry.installedContext(),null);
});
test('unknown floor recovery offers an explicit installer choice without making it the default',async()=>{
 const declined=fixture({platform:'linux',recovery:true,confirmable:true});await declined.entry.startInstalled();
 const prompt=declined.events.find(e=>e.dialog).dialog;assert.deepEqual([...prompt.buttons],['Get Latest Installer','Quit','Use This Installer']);assert.equal(prompt.defaultId,0);assert.equal(prompt.cancelId,1);assert.match(prompt.detail,/older installer could damage your data/);assert(!declined.events.includes('run-bundled'));
 const accepted=fixture({platform:'linux',recovery:true,confirmable:true,answer:2});await accepted.entry.startInstalled();assert.equal(accepted.events.at(-1),'run-bundled');assert.equal(accepted.events.filter(e=>e==='baseline').length,2);
});
test('recovery downloads and reveals a verified Linux installer; an offline failure remains a recovery offer',async()=>{
 const f=fixture({platform:'linux',recovery:true,answer:0});await f.entry.startInstalled();
 assert(f.events.includes('installer-check'));assert(f.events.includes('installer-download'));assert.deepEqual(f.events.at(-2),['reveal','verified-installer.AppImage']);assert.equal(f.events.at(-1),'quit');
 const options=f.events.find(e=>Array.isArray(e)&&e[0]==='installer-options')[1];assert.equal(options.platform,'linux');assert.equal(options.arch,'x64');assert.equal(options.publicKey,'trusted-public-key');
 assert(!f.events.includes('select'));assert(!f.events.includes('run-bundled'));
 const offline=fixture({platform:'linux',recovery:true,answer:0,installerError:true});await offline.entry.startInstalled();
 assert.equal(offline.events.filter(e=>e.dialog).length,2);assert.match(offline.events.filter(e=>e.dialog).at(-1).dialog.detail,/Offline.*data is unchanged/);assert(!offline.events.includes('installer-download'));assert.equal(offline.events.at(-1),'quit');
});
test('recovered source startup failures and exhausted retries offer installers, never older workers',async()=>{
 for(const settings of [{source:true,recovered:true,broken:true},{source:true,blocked:true,retryAvailable:false}]){
  const f=fixture({platform:'linux',answer:0,...settings});await f.entry.startInstalled();assert(f.events.includes('installer-download'));assert(!f.events.includes('run-bundled'));assert(!f.events.includes('launch-source'));assert.equal(f.events.at(-1),'quit');
 }
});
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
test('#1821 D5: the launcher passes its stamped version to the updated app, and an unstamped installer passes 0',async()=>{
 const stamped=fixture({source:true,build:{revision:'a'.repeat(40),sequence:100,launcher:2}});await stamped.entry.startInstalled();
 assert.equal(stamped.launched[3],2);
 // SourceUpdates still receives exactly the baseline it always did.
 assert.deepEqual({...stamped.options.baseline},{revision:'a'.repeat(40),sequence:100});
 const old=fixture({source:true});await old.entry.startInstalled();
 assert.equal(old.launched[3],0);
 for(const launcher of [-1,1.5,'2',null]){
  const bad=fixture({source:true,build:{revision:'a'.repeat(40),sequence:100,launcher}});
  await assert.rejects(bad.entry.startInstalled(),/Invalid installed desktop launcher version/,String(launcher));
  assert(!bad.events.includes('launch-source'));
 }
});
test('#1821 D5: a swapped-in launcher reports the version in its own archive, not the installer stamp',async()=>{
 const f=fixture({source:true,build:{revision:'a'.repeat(40),sequence:100,launcher:1},launcherJson:'{"version":2,"minimum":0}'});
 await f.entry.startInstalled();
 assert.equal(f.launched[3],2);assert.deepEqual(f.events.find(e=>e?.trial!==undefined),{trial:2});
 assert(!f.events.includes('count'));
});
test('#1821 D5: a launcher on trial counts its start just before it hands over',async()=>{
 const f=fixture({source:true,launcherJson:'{"version":2,"minimum":0}',trial:'trial'});
 await f.entry.startInstalled();
 assert.deepEqual(f.events.slice(-4),['unlock','count','launch-source','quit']);
 assert(!f.events.some(e=>e?.restore));
});
test('#1821 D5: a launcher whose trial start never reached the app restores the previous launcher and starts nothing',async()=>{
 const f=fixture({source:true,launcherJson:'{"version":2,"minimum":0}',trial:'restore'});
 await f.entry.startInstalled();
 assert.deepEqual(f.events.slice(-2),[{restore:path.resolve('Programs/AI Shuffle.exe')},'quit']);
 assert(!f.events.includes('load'));assert(!f.events.includes('launch-source'));assert(!f.events.includes('run-bundled'));
});
test('#1821 D5: a launcher on trial that fails before the hand-over restores the previous one and still reports the error',async()=>{
 const f=fixture({source:true,broken:true,launcherJson:'{"version":2,"minimum":0}',trial:'trial'});
 await assert.rejects(f.entry.startInstalled(),/No healthy/);
 assert(f.events.some(e=>e?.restore));assert(!f.events.includes('launch-source'));
 // Outside a trial the same failure restores nothing.
 const plain=fixture({source:true,broken:true,launcherJson:'{"version":2,"minimum":0}'});
 await assert.rejects(plain.entry.startInstalled(),/No healthy/);assert(!plain.events.some(e=>e?.restore));
});

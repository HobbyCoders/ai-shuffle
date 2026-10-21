import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
import { test } from 'node:test';

function fixture(env = {}, modules = {}, platform = 'win32') {
 const calls = []; const timers = [];
 const app = { relaunch: options => calls.push(options), quit: () => calls.push('quit') };
 const module = { exports: {} };
 vm.runInNewContext(readFileSync(new URL('../dist/main/updater.js', import.meta.url), 'utf8'), {
  exports: module.exports, module, require: id => id === 'electron' ? { app } : modules[id] ?? path,
  process: { env, platform, versions: {electron:'44.2.0'} }, setTimeout: fn => { timers.push(fn); return timers.length; }, clearTimeout() {},
  setInterval: fn => { timers.push(fn); return timers.length; }, clearInterval() {}, Date, console
 });
 return { updater: new module.exports.Updater(), calls, timers, app };
}
test('#99: only installed Linux updates opt into the installed Electron preflight', () => {
 for (const [platform,executable,expected] of [['linux','/AI Shuffle.AppImage',['44.2.0']],['linux',undefined,[]],['win32','C:/AI Shuffle.exe',[]]]) {
  const {updater}=fixture({},{},platform),versions=[];
  updater.useApplications(source({useInstalledElectron:version=>versions.push(version)}),executable);
  assert.deepEqual(versions,expected);
 }
});
test('bridge reports staged identity without bypassing either busy check', async () => {
 const {updater,calls}=fixture(); const pending='b'.repeat(40); let request={};
 updater.useApplications(source({pendingVersion:pending}),'C:/Programs/AI Shuffle.exe');
 updater.setPolicy(async()=>true);
 updater.onInstallGuard(async()=> 'Busy with active sessions; retry when finished.');
 updater.setBridge(async()=>{const next=request;request={};return next;},async()=>false);
 updater.start(); await new Promise(setImmediate);
 await updater.check();
 assert.equal(updater.current.staged_version,pending);
 await updater.download();
 request={action:'install',expires_at:Date.now()/1000+10};
 await updater.exchange(); await new Promise(setImmediate);
 assert.equal(calls.length,0,'ordinary install still checks running sessions');
 updater.setBridge(async()=>{const next=request;request={};return next;},async()=>true);
 request={action:'install',expires_at:Date.now()/1000+10};
 await updater.exchange(); await new Promise(setImmediate);
 assert.equal(calls.length,0,'install guard still checks again immediately before restart');
 updater.stop();
});

const available = { kind: 'available', version: 'bbbbbbbbbbbb' };
const ready = { ...available, kind: 'ready' };
function source(overrides = {}) { return {
 check: async () => available, download: async () => ready,
 channelConfig: async () => ({channel:'stable',source:'default',editable:true}),
 version: 'a'.repeat(40), options: { root: 'C:/isolated/source' },
 application: async () => ({ node: 'C:/isolated/new-node/node.exe', desktopDir: 'C:/isolated/desktop' }), ...overrides
}; }
function deferred() { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; }

test('initial staged check keeps its notice without a parallel release-info overwrite',async()=>{
 const {updater}=fixture();let metadataReads=0;
 updater.useApplications(source({pendingVersion:ready.version,retryCleanup:async()=>{},
  check:async()=>({...ready,notes:'Staged notes',notice:'A newer release needs the latest installer.'}),
  releaseInfo:async()=>{metadataReads++;return {notes:'Staged notes'};}}));
 await new Promise(setImmediate);
 assert.equal(updater.current.phase,'ready');assert.equal(metadataReads,0);
 assert.match(updater.current.notes,/A newer release needs the latest installer/);
});

test('a newer incompatible release is a notice beside the installable staged release, not an error',async()=>{
 const {updater,calls}=fixture({},'linux');
 const notice='Install the latest AI Shuffle installer for the newer release. The downloaded update remains ready to install.';
 const notes=Array.from({length:20},(_,i)=>`Release note line ${i+1}`).join('\n');
 const result={...ready,release:'1.0.0',notes,notice};
 const manager=source({check:async()=>result,download:async()=>result,retryCleanup:async()=>{}});
 updater.useApplications(manager,'/AI Shuffle.AppImage');
 await updater.check();assert.equal(updater.current.phase,'ready');assert.equal(updater.current.error,null);
 assert.equal(updater.current.message,null);assert.equal(updater.current.version,ready.version);
 assert.equal(updater.current.notes,notice+'\n\n'+notes);
 await updater.install();assert.equal(calls.at(-1),'quit');
 // The same result arriving from a download must keep its notice too.
 const second=fixture({},'linux').updater;
 second.useApplications(source({download:async()=>result}),'/AI Shuffle.AppImage');
 await second.check();await second.download();assert.equal(second.current.phase,'ready');
 assert.equal(second.current.notes,notice+'\n\n'+notes);
});

test('cleanup failing after ready refuses before stopping services and Retry re-enables installation',async()=>{
 const {updater,calls}=fixture();let locked=false;
 const manager=source({check:async()=>ready,cleanupError:null,retryCleanup:async()=>{if(locked){manager.cleanupError='Cleanup locked: close programs, then Retry';throw Error(manager.cleanupError);}manager.cleanupError=null;}});
 updater.useApplications(manager);await updater.check();assert.equal(updater.current.phase,'ready');
 updater.onBeforeInstall(async()=>calls.push('stop'));locked=true;await updater.install();
 assert.equal(updater.current.phase,'error');assert.match(updater.current.error,/Cleanup locked/);assert.deepEqual(calls,[]);
 locked=false;await updater.check();await updater.install();assert.equal(calls[0],'stop');assert.equal(calls.at(-1),'quit');
});

test('worker bridge performs one-shot downloads and refuses expired or busy restarts', async () => {
 const {updater,calls}=fixture();let request={};const seen=[];
 updater.useApplications(source());
 updater.setBridge(async state=>{seen.push(state.phase);const next=request;request={};return next;},async()=>false);
 await updater.check();updater.start();await new Promise(setImmediate);
 request={action:'download',expires_at:Date.now()/1000+10};await updater.exchange();await new Promise(setImmediate);
 assert.equal(updater.current.phase,'ready',JSON.stringify({seen,request,timer:updater.bridgeTimer,exchanging:updater.exchanging,current:updater.current}));
 request={action:'install',expires_at:Date.now()/1000+10};await updater.exchange();
 assert.match(updater.current.error,/Busy/);assert.equal(calls.length,0);
 updater.setBridge(async()=>({action:'install',expires_at:0}),async()=>true);await updater.exchange();assert.equal(calls.length,0);
 updater.stop();assert(seen.length>0);
});
test('worker channel requests write through the source updater and report the persisted choice', async () => {
 const {updater}=fixture();let channel='stable';let request={};const seen=[];
 updater.useApplications(source({
  channelConfig:async()=>({channel,source:channel==='stable'?'default':'file',editable:true}),
  setChannel:async value=>{channel=value;}
 }));
 updater.setBridge(async state=>{seen.push(state.channel_config);const next=request;request={};return next;},async()=>false);
 updater.start();await new Promise(setImmediate);
 request={action:'set_channel',channel:'nightly',expires_at:Date.now()/1000+10};
 await updater.exchange();await new Promise(setImmediate);
 assert.equal(channel,'nightly');assert.equal(seen.at(-1).channel,'nightly');
 assert.equal(updater.current.phase,'idle');updater.stop();
});
test('a return to stable goes through channel-return, reports what runs, and reports a refusal once', async () => {
 const returns=[];let refuse=true;let channel='nightly';let set=0;
 const channelReturn={
  installedRelease:async(u,version)=>({revision:u.version,container_sequence:5,version}),
  returnToStable:async(u,manifest,version)=>{returns.push([manifest,version]);if(refuse)throw Error('This app runs code that is newer than the current stable release'+'.'.repeat(2000));channel='stable';}
 };
 const {updater,app}=fixture({},{'./channel-return':channelReturn});app.getVersion=()=>'1.0.2';
 let request={};const seen=[];
 updater.useApplications(source({
  channel:async()=>channel,
  channelConfig:async()=>({channel,source:'file',editable:true}),
  setChannel:async()=>{set++;throw Error('Returning to stable requires a reinstall');}
 }));
 updater.setBridge(async state=>{seen.push(state);const next=request;request={};return next;},async()=>false);
 updater.start();await new Promise(setImmediate);
 assert.deepEqual(seen.at(-1).installed_release,{revision:'a'.repeat(40),container_sequence:5,version:'1.0.2'});
 request={action:'set_channel',channel:'stable',stable_manifest:'{"signed":true}',attempt:7,expires_at:Date.now()/1000+10};
 await updater.exchange();await new Promise(setImmediate);
 assert.deepEqual(returns,[['{"signed":true}','1.0.2']]);assert.equal(set,0);assert.equal(channel,'nightly');
 // Reported on the next exchange, then dropped: the worker has cleared the request.
 assert.match(seen.at(-1).channel_error,/newer than the current stable release/);
 // Capped to what the worker accepts, and tied to the attempt it answers.
 assert.equal(seen.at(-1).channel_error.length,1000);assert.equal(seen.at(-1).channel_attempt,7);
 await updater.exchange();assert.equal(seen.at(-1).channel_error,undefined);assert.equal(seen.at(-1).channel_attempt,undefined);
 refuse=false;request={action:'set_channel',channel:'stable',stable_manifest:'{"signed":true}',expires_at:Date.now()/1000+10};
 await updater.exchange();await new Promise(setImmediate);
 assert.equal(channel,'stable');assert.equal(seen.at(-1).channel_config.channel,'stable');assert.equal(seen.at(-1).channel_error,undefined);
 // Without a checked manifest a return still goes to the launcher's setter, which refuses it.
 channel='nightly';request={action:'set_channel',channel:'stable',expires_at:Date.now()/1000+10};
 await updater.exchange();await new Promise(setImmediate);
 assert.equal(set,1);assert.equal(channel,'nightly');updater.stop();
});
test('account policy gates checks, downloads and staged installs, and failures fail closed', async () => {
 const { updater, calls } = fixture(); let enabled = false; let checks = 0;
 updater.setPolicy(async () => enabled);
 updater.useApplications(source({check:async()=>{ checks++; return available; }}));
 await updater.check(); assert.equal(checks,0);
 enabled=true; await updater.check(); assert.equal(checks,1);
 enabled=false; await updater.download(); assert.equal(updater.current.phase,'available');
 enabled=true; await updater.download(); assert.equal(updater.current.phase,'ready');
 enabled=false; await updater.install(); assert.equal(calls.length,0);
 updater.setPolicy(async()=>{throw Error('offline');});
 await updater.install(); assert.equal(calls.length,0); assert.match(updater.current.error,/Could not read/);
 updater.setPolicy(async()=>true); await updater.install(); assert.equal(calls.at(-1),'quit');
});
test('metadata checks expose available; only explicit download builds, then install stops services and relaunches', async () => {
 const { updater, calls } = fixture(); const states = []; updater.subscribe(s => states.push(s)); let downloads = 0;
 updater.useApplications(source({ download: async progress => { downloads++; progress(null, 'Building frontend…'); return ready; } }));
 updater.onBeforeInstall(async () => { calls.push('stop-worker'); calls.push('stop-postgres'); });
 await updater.install(); await updater.check(); assert.equal(updater.current.phase, 'available');
 assert.equal(downloads, 0); assert.equal(states.some(s => s.phase === 'downloading'), false);
 await updater.install(); assert.equal(calls.length, 0);
 await updater.download(); assert.equal(updater.current.phase, 'ready'); assert.equal(downloads, 1);
 assert(states.some(s => s.phase === 'downloading' && s.message === 'Building frontend…' && s.percent === null));
 await updater.check(); await updater.download(); assert.equal(downloads, 1);
 await updater.install();
 assert.equal(calls[0], 'stop-worker'); assert.equal(calls[1], 'stop-postgres');
 assert.equal(calls[2].execPath, 'C:/isolated/new-node/node.exe');
 assert.deepEqual(Array.from(calls[2].args), [path.join('C:/isolated/desktop', 'scripts/bootstrap.mjs'), '--root', 'C:/isolated/source']);
 assert.equal(calls[3], 'quit');
 await updater.install(); assert.equal(calls.length, 4, 'double-click does not restart twice');
});

test('Linux worker bridge reports source phases and running_version, then restarts the original AppImage',async()=>{
 const {updater,calls}=fixture();const states=[];let request={};
 updater.useApplications(source({options:{root:'/home/me/.config/AI Shuffle/updates'},download:async progress=>{progress(50,'Building Linux application');return ready;}}),'/home/me/Applications/AI Shuffle.AppImage');
 updater.setPolicy(async()=>true);updater.subscribe(state=>states.push(state));
 updater.setBridge(async state=>{assert.equal(state.running_version,'a'.repeat(40));const next=request;request={};return next;},async()=>true);
 updater.start();await new Promise(setImmediate);
 for(const action of ['check','download','install']){
  request={action,expires_at:Date.now()/1000+10};await updater.exchange();await new Promise(setImmediate);
 }
 for(const phase of ['checking','available','downloading','ready'])assert(states.some(state=>state.phase===phase),phase);
 assert.equal(calls[0].execPath,'/home/me/Applications/AI Shuffle.AppImage');assert.deepEqual([...calls[0].args],[]);assert.equal(calls[1],'quit');updater.stop();
});
test('build errors retain the available version and retry without another check', async () => {
 const { updater } = fixture(); let attempts = 0;
 updater.useApplications(source({ download: async progress => { progress(42, 'Downloading…'); if (++attempts === 1) throw Error('Build interrupted'); return ready; } }));
 await updater.check(); await updater.download(); assert.equal(updater.current.phase, 'error');
 assert.equal(updater.current.version, available.version); assert.equal(updater.current.percent, null);
 await updater.download(); assert.equal(updater.current.phase, 'ready'); assert.equal(updater.current.error, null); assert.equal(attempts, 2);
});

test('installed app updates restart the existing EXE without a console launcher or baseline source directory', async () => {
 const { updater, calls } = fixture();
 updater.useApplications(source({ application: async () => assert.fail('Installer baseline is not a source directory') }), 'C:/Programs/AI Shuffle/AI Shuffle.exe');
 updater.onBeforeInstall(async () => calls.push('stopped'));
 await updater.check(); await updater.download(); await updater.install();
 assert.equal(calls[0], 'stopped');
 assert.equal(calls[1].execPath, 'C:/Programs/AI Shuffle/AI Shuffle.exe');
 assert.deepEqual(Array.from(calls[1].args), []);
 assert.equal(calls[2], 'quit');
});

test('shutdown failures retain staged updates for direct install retry without rebuilding', async () => {
 const { updater, calls } = fixture(); let builds = 0; let attempts = 0;
 updater.useApplications(source({ download: async () => { builds++; return ready; } }));
 updater.onBeforeInstall(async () => { if (++attempts === 1) throw Error('Database did not stop'); });
 await updater.check(); await updater.download(); await updater.install();
 assert.equal(updater.current.phase, 'ready'); assert.match(updater.current.error, /Database did not stop/); assert.equal(calls.length, 0);
 await updater.check(); await updater.download(); await updater.install();
 assert.equal(builds, 1); assert.equal(attempts, 2); assert.equal(calls.at(-1), 'quit'); assert.equal(updater.current.error, null);
});

test('installed restart drops the running revision so legacy launchers select the pending update', async () => {
 const env = { SHUFFLE_SOURCE_ROOT: 'C:/isolated/source', SHUFFLE_SOURCE_REVISION: 'a'.repeat(40), SHUFFLE_SOURCE_NODE: 'C:/runtime/node.exe', PATH: 'keep-path' };
 const { updater, app } = fixture(env);
 let inherited;
 app.relaunch = () => { inherited = { ...env }; };
 updater.useApplications(source({ pendingVersion: 'b'.repeat(40) }), 'C:/Programs/AI Shuffle.exe');
 await updater.install();
 assert.deepEqual(inherited, { PATH: 'keep-path' });
 assert.deepEqual(env, { PATH: 'keep-path' });
});

test('failed installed relaunch restores source context and remains retryable', async () => {
 const original = { SHUFFLE_SOURCE_ROOT: 'C:/isolated/source', SHUFFLE_SOURCE_REVISION: 'a'.repeat(40), SHUFFLE_SOURCE_NODE: 'C:/runtime/node.exe' };
 const env = { ...original };
 const { updater, app } = fixture(env);
 app.relaunch = () => { assert.deepEqual(env, {}); throw Error('Relaunch failed'); };
 updater.useApplications(source({ pendingVersion: 'b'.repeat(40) }), 'C:/Programs/AI Shuffle.exe');
 await updater.install();
 assert.deepEqual(env, original);
 assert.equal(updater.current.phase, 'ready');
 assert.match(updater.current.error, /Relaunch failed/);
 app.relaunch = () => assert.deepEqual(env, {});
 await updater.install();
 assert.equal(updater.current.error, null);
});

test('relaunch failure is retryable without downloading again', async () => {
 const { updater, app, calls } = fixture(); const relaunch = app.relaunch;
 updater.useApplications(source({ pendingVersion: 'b'.repeat(40), download: async () => { throw Error('Must not download'); } }));
 app.relaunch = () => { throw Error('Relaunch failed'); };
 await updater.install(); assert.equal(updater.current.phase, 'ready'); assert.match(updater.current.error, /Relaunch failed/);
 app.relaunch = relaunch; await updater.install(); assert.equal(calls.at(-1), 'quit');
});

test('pending source state is immediately ready and checks and downloads leave it intact', async () => {
 const { updater } = fixture();
 updater.useApplications(source({ pendingVersion: 'b'.repeat(40), check: async () => { assert.fail('pending must not check'); }, download: async () => { assert.fail('pending must not rebuild'); } }));
 assert.equal(updater.current.phase, 'ready'); assert.equal(updater.current.version, ready.version);
 await updater.check(); await updater.download(); assert.equal(updater.current.phase, 'ready');
});

test('bridge reports the running revision separately from the staged update', async () => {
 const { updater } = fixture(); const seen = [];
 updater.useApplications(source({ pendingVersion: 'b'.repeat(40) }));
 updater.setBridge(async state => { seen.push(state); return {}; }, async () => true);
 await updater.exchange();
 assert.equal(seen[0].version, 'b'.repeat(12));
 assert.equal(seen[0].running_version, 'a'.repeat(40));
 assert.equal(seen[0].staged_version, 'b'.repeat(40));
 updater.useApplications(source({ version: 'b'.repeat(40), pendingVersion: 'b'.repeat(40) }));
 await updater.exchange();
 assert.equal(seen[1].phase, 'idle');
 assert.equal(seen[1].running_version, 'b'.repeat(40));
 assert.equal(seen[1].staged_version, null);
});

test('a pending revision already running its first boot is not offered for installation again', async () => {
 const { updater, calls } = fixture();
 updater.useApplications(source({ pendingVersion: 'a'.repeat(40) }));
 assert.equal(updater.current.phase, 'idle'); assert.equal(updater.current.version, null);
 await updater.install(); assert.deepEqual(calls, []);
});

test('overlapping check, download and install commands never race or duplicate work', async () => {
 const { updater, calls } = fixture(); const checking = deferred(); const downloading = deferred(); const stopping = deferred();
 let checks = 0; let downloads = 0; let stops = 0;
 updater.useApplications(source({ check: async () => { checks++; await checking.promise; return available; }, download: async () => { downloads++; await downloading.promise; return ready; } }));
 updater.onBeforeInstall(async () => { stops++; await stopping.promise; });
 const check = updater.check(); await updater.check(); await updater.download(); await updater.install();
 assert.equal(checks, 1); assert.equal(downloads, 0); assert.equal(stops, 0);
 checking.resolve(); await check;
 const download = updater.download(); await updater.check(); await updater.download(); await updater.install();
 assert.equal(checks, 1); assert.equal(downloads, 1); assert.equal(stops, 0);
 downloading.resolve(); await download;
 const install = updater.install(); await updater.check(); await updater.download(); await updater.install();
 assert.equal(checks, 1); assert.equal(downloads, 1); assert.equal(stops, 1);
 stopping.resolve(); await install; assert.equal(calls.length, 2);
});

test('initial and periodic checks are metadata-only and never enable interactive authentication', async () => {
 const { updater, timers } = fixture(); const modes = [];
 updater.useApplications(source({ check: async (progress, interactive) => { assert.equal(progress, undefined); modes.push(interactive); return available; }, download: async () => { assert.fail('automatic download'); } }));
 updater.start(); assert.equal(timers.length, 2);
 for (const timer of timers) { timer(); await new Promise(resolve => setImmediate(resolve)); }
 assert.deepEqual(modes, [false, false]); assert.equal(updater.current.phase, 'available');
 await updater.check(true); assert.deepEqual(modes, [false, false, true]); updater.stop();
});

test('metadata failures retry and a current result clears stale availability', async () => {
 const { updater } = fixture(); let attempts = 0;
 updater.useApplications(source({ check: async () => { if (++attempts === 1) throw Error('Offline'); return attempts === 2 ? available : { kind: 'current' }; } }));
 await updater.check(); assert.equal(updater.current.phase, 'error');
 await updater.check(); assert.equal(updater.current.phase, 'available'); assert.equal(updater.current.error, null);
 await updater.check(); assert.equal(updater.current.phase, 'idle'); assert.equal(updater.current.version, null);
});

test('D22: installed apps check once shortly after launch and then daily, with no consent prompt', async () => {
 const delays = []; const fns = [];
 const module = { exports: {} };
 vm.runInNewContext(readFileSync(new URL('../dist/main/updater.js', import.meta.url), 'utf8'), {
  exports: module.exports, module, require: id => id === 'electron' ? { app: {} } : path, process: { env: {} },
  setTimeout: (fn, ms) => { delays.push(['timeout', ms]); fns.push(fn); return fns.length; }, clearTimeout() {},
  setInterval: (fn, ms) => { delays.push(['interval', ms]); fns.push(fn); return fns.length; }, clearInterval() {}, Date
 });
 const updater = new module.exports.Updater();
 let checks = 0;
 updater.useApplications(source({ check: async () => { checks++; return { kind: 'current' }; } }));
 updater.start();
 assert.deepEqual(delays, [['timeout', 15_000], ['interval', 24 * 60 * 60 * 1000]]);
 for (const fn of fns) { fn(); await new Promise(setImmediate); }
 assert.equal(checks, 2);
 // A preview build has no update checks at all.
 const preview = new module.exports.Updater(false);
 preview.useApplications(source());
 delays.length = 0; preview.start();
 assert.deepEqual(delays, []);
});

test('the signed release name and notes travel with the offered update and clear when current', async () => {
 const { updater } = fixture(); const seen = [];
 updater.subscribe(state => seen.push(state));
 let result = { ...available, release: '0.9.0', notes: 'Faster startup.\nFixes the sidebar.' };
 updater.useApplications(source({ check: async () => result, download: async () => ({ ...result, kind: 'ready' }) }));
 await updater.check();
 assert.equal(updater.current.phase, 'available'); assert.equal(updater.current.release, '0.9.0'); assert.equal(updater.current.notes, 'Faster startup.\nFixes the sidebar.');
 await updater.download();
 assert.equal(updater.current.phase, 'ready'); assert.equal(updater.current.release, '0.9.0'); assert.match(seen.at(-1).notes, /Fixes the sidebar/);
 const { updater: other } = fixture(); result = { kind: 'current' };
 other.useApplications(source({ check: async () => result })); await other.check();
 assert.equal(other.current.release, null); assert.equal(other.current.notes, null);
});

test('a download staged before a restart shows its notes from the stored manifest; older managers show none', async () => {
 const pending = 'b'.repeat(40);
 const { updater } = fixture();
 updater.useApplications(source({ pendingVersion: pending, releaseInfo: async revision => { assert.equal(revision, pending); return { release: '0.9.1', notes: 'Stored notes' }; } }));
 assert.equal(updater.current.phase, 'ready'); await new Promise(setImmediate);
 assert.equal(updater.current.release, '0.9.1'); assert.equal(updater.current.notes, 'Stored notes');
 const { updater: legacy } = fixture();
 legacy.useApplications(source({ pendingVersion: pending })); await new Promise(setImmediate);
 assert.equal(legacy.current.phase, 'ready'); assert.equal(legacy.current.notes, null);
});
test('macOS and Linux: installer checks run on launch and daily, offer the release, and download without installing', async () => {
 const { updater, calls, timers } = fixture(); const saved = []; let checks = 0; let downloads = 0; let enabled = true;
 const installers = {
  check: async () => { checks++; return { kind: 'available', version: '0.9.0', release: '0.9.0', installer: {} }; },
  download: async progress => { downloads++; progress(50, 'Downloading AI Shuffle 0.9.0…'); return { file: '/Users/me/Downloads/AI-Shuffle-0.9.0.dmg', version: '0.9.0' }; }
 };
 updater.setPolicy(async () => enabled);
 updater.useInstallers(installers, file => saved.push(file));
 assert.equal(updater.current.phase, 'idle');
 updater.start();
 // One timer for the launch check, one for the daily check; no consent prompt in between.
 assert.equal(timers.length, 2);
 timers[0](); await new Promise(setImmediate);
 assert.equal(checks, 1);
 assert.equal(updater.current.phase, 'available');
 assert.equal(updater.current.version, '0.9.0');
 assert.equal(updater.current.release, '0.9.0');
 await updater.check();
 assert.equal(checks, 2);
 enabled = false; await updater.download(); assert.equal(downloads, 0);
 enabled = true; await updater.download();
 assert.equal(downloads, 1);
 assert.deepEqual(saved, ['/Users/me/Downloads/AI-Shuffle-0.9.0.dmg']);
 assert.equal(updater.current.phase, 'idle');
 assert.match(updater.current.message, /0\.9\.0 was downloaded and verified/);
 // The web UI announces the saved file; the next check clears it.
 assert.equal(updater.current.installer, '/Users/me/Downloads/AI-Shuffle-0.9.0.dmg');
 // There is nothing to install in place: no relaunch, ever.
 await updater.install(); assert.deepEqual(calls, []);
 installers.check = async () => ({ kind: 'current' }); await updater.check();
 assert.equal(updater.current.phase, 'idle'); assert.equal(updater.current.version, null); assert.equal(updater.current.installer, null);
 installers.check = async () => { throw Error('offline'); }; await updater.check();
 assert.equal(updater.current.phase, 'error');
 updater.stop();
});
test('installer checks never replace Windows source updates and respect the no-updates switch', () => {
 const windows = fixture();
 windows.updater.useApplications(source());
 windows.updater.useInstallers({ check: async () => { throw Error('must not run'); }, download: async () => { throw Error('must not run'); } }, () => {});
 assert.equal(windows.updater.installers, null);
 const off = fixture({ SHUFFLE_DESKTOP_NO_UPDATES: '1' });
 off.updater.useInstallers({ check: async () => ({ kind: 'current' }), download: async () => ({}) }, () => {});
 assert.equal(off.updater.current.phase, 'unsupported');
 off.updater.start(); assert.equal(off.timers.length, 0);
});

test('NC-6: the worker bridge ticks every 30 s, not 2 s, and pushes a new phase at once', async () => {
 const delays = []; const ticks = [];
 const module = { exports: {} };
 vm.runInNewContext(readFileSync(new URL('../dist/main/updater.js', import.meta.url), 'utf8'), {
  exports: module.exports, module, require: id => id === 'electron' ? { app: {} } : path, process: { env: {} },
  setTimeout: () => 0, clearTimeout() {}, setInterval: (fn, ms) => { delays.push(ms); ticks.push(fn); return ticks.length; }, clearInterval() {}, Date
 });
 const { Updater, BRIDGE_INTERVAL_MS } = module.exports;
 assert.equal(BRIDGE_INTERVAL_MS, 30_000);
 // The worker's windows: the desktop updater is gone after DESKTOP_PRESENCE_SECONDS of silence
 // and a queued request lapses after DESKTOP_REQUEST_SECONDS. One missed report (two ticks of
 // silence) must fit inside presence (R11-10), and a request must outlive presence.
 const worker = readFileSync(new URL('../../app/core/application_updates.py', import.meta.url), 'utf8');
 const seconds = name => Number(worker.match(new RegExp(`^${name} = (\\d+)$`, 'm'))[1]);
 assert(2 * BRIDGE_INTERVAL_MS < seconds('DESKTOP_PRESENCE_SECONDS') * 1000);
 assert(seconds('DESKTOP_PRESENCE_SECONDS') <= seconds('DESKTOP_REQUEST_SECONDS'));
 const updater = new Updater(); const seen = [];
 let release; const gate = () => new Promise(resolve => { release = resolve; });
 updater.useApplications(source({ check: async () => { await gate(); return { kind: 'available', version: 'bbbbbbbbbbbb' }; } }));
 updater.setBridge(async state => { seen.push(state.phase); return {}; }, async () => false);
 updater.start();
 assert.deepEqual(delays.filter(ms => ms < 60_000), [30_000], 'one bridge interval, no 2 s heartbeat');
 await new Promise(setImmediate);
 assert.deepEqual(seen, ['idle'], 'one exchange at start');
 // An explicit check: "checking" and then "available" reach the worker without waiting for the tick.
 const check = updater.check(true); await new Promise(setImmediate);
 assert.deepEqual(seen, ['idle', 'checking']);
 release(); await check; await new Promise(setImmediate);
 assert.deepEqual(seen, ['idle', 'checking', 'available']);
 // Progress inside a phase waits for the interval.
 updater.useApplications(source({ download: async progress => { progress(10, 'a'); progress(20, 'b'); return { kind: 'ready', version: 'bbbbbbbbbbbb' }; } }));
 seen.length = 0; updater.set({ phase: 'available' }); await new Promise(setImmediate);
 seen.length = 0; await updater.download(); await new Promise(setImmediate);
 assert.deepEqual(seen, ['downloading', 'ready'], 'no exchange per progress tick');
 // A phase change during an exchange is reported right after it, not dropped.
 seen.length = 0; let hold;
 updater.setBridge(async state => { seen.push(state.phase); if (!hold) await new Promise(resolve => { hold = resolve; }); return {}; }, async () => false);
 const ex = updater.exchange(); await new Promise(setImmediate); updater.set({ phase: 'error' }); hold(); await ex; await new Promise(setImmediate);
 assert.deepEqual(seen, ['ready', 'error']);
 updater.stop(); seen.length = 0; updater.set({ phase: 'idle' }); await new Promise(setImmediate);
 assert.deepEqual(seen, [], 'nothing after stop');
});

test('an install refused by the guard stops nothing, reports why and can run later; a failing guard fails closed', async () => {
 const { updater, calls } = fixture(); const order = []; let refusal = 'Busy with active sessions; retry when finished.';
 updater.useApplications(source({ pendingVersion: 'b'.repeat(40) }));
 updater.onInstallGuard(async () => { order.push('guard'); return refusal; });
 updater.onBeforeInstall(async () => { order.push('stop'); });
 updater.onInstallFailed(async () => { order.push('restart'); });
 await updater.install();
 assert.deepEqual(order, ['guard'], 'refused: the worker and database keep running');
 assert.equal(calls.length, 0); assert.equal(updater.current.phase, 'ready');
 assert.equal(updater.current.error, refusal, 'the web UI shows the reason');
 updater.onInstallGuard(async () => { order.push('guard'); throw Error('active-sessions 503'); });
 await updater.install();
 assert.deepEqual(order, ['guard', 'guard'], 'nothing was stopped, so nothing is restarted');
 assert.equal(calls.length, 0); assert.match(updater.current.error, /active-sessions 503/);
 refusal = null; updater.onInstallGuard(async () => { order.push('guard'); return refusal; });
 await updater.install();
 assert.deepEqual(order, ['guard', 'guard', 'guard', 'stop']);
 assert.equal(calls.at(-1), 'quit');
});

test('the account policy is read before the install guard runs', async () => {
 const { updater, calls } = fixture(); let asked = 0;
 updater.useApplications(source({ pendingVersion: 'b'.repeat(40) }));
 updater.setPolicy(async () => false);
 updater.onInstallGuard(async () => { asked++; return null; });
 await updater.install();
 assert.equal(asked, 0); assert.equal(calls.length, 0);
});

test('a bridge that fails or a session count that cannot be read never installs and never rejects', async () => {
 const { updater, calls } = fixture(); let installs = 0;
 updater.useApplications(source({ pendingVersion: 'b'.repeat(40) }));
 updater.onBeforeInstall(async () => { installs++; });
 updater.setBridge(async () => { throw Error('worker restarting'); }, async () => true);
 updater.start();
 await updater.exchange();
 updater.setBridge(async () => ({ action: 'install', expires_at: Date.now() / 1000 + 10 }), async () => { throw Error('active-sessions 503'); });
 await updater.exchange(); await new Promise(setImmediate);
 assert.equal(installs, 0); assert.equal(calls.length, 0);
 assert.equal(updater.current.phase, 'ready');
 // An unknown action, or one without an expiry, does nothing either.
 updater.setBridge(async () => ({ action: 'reinstall-everything', expires_at: Date.now() / 1000 + 10 }), async () => true);
 await updater.exchange();
 updater.setBridge(async () => ({ action: 'install' }), async () => true);
 await updater.exchange(); await new Promise(setImmediate);
 assert.equal(installs, 0);
 updater.stop();
});

const FAILED_START = 'Error: This revision failed to start. Check again after a newer update. Previous files and data have been retained.';
test('a check refused for a failed first start the launcher put back offers that update again and says why', async () => {
 const {updater}=fixture();const B='b'.repeat(40);let failed=true;const released=[];
 updater.useApplications(source({check:async()=>{if(failed)throw Error(FAILED_START.slice(7));return {...available,version:B.slice(0,12),release:'1.0.2'};}}));
 updater.onFailedStart({release:async()=>{released.push(B);failed=false;return [B];},installerNewer:async()=>assert.fail('a retry needs no installer')});
 await updater.check();
 assert.deepEqual(released,[B]);
 assert.equal(updater.current.phase,'available');assert.equal(updater.current.version,B.slice(0,12));assert.equal(updater.current.release,'1.0.2');
 assert.match(updater.current.message,/did not finish.*try again.*can take a few minutes/i);assert.equal(updater.current.error,null);
 // Downloading it is the retry; the hint goes once the update is no longer the one that failed.
 await updater.download();assert.equal(updater.current.phase,'ready');assert.equal(updater.current.message,null);
});
test('a failed first start that cannot be retried reports whether the installer is newer, and nothing else does', async () => {
 for (const newer of [false,true,null]) {
  const {updater}=fixture();
  updater.useApplications(source({check:async()=>{throw Error(FAILED_START.slice(7));}}));
  updater.onFailedStart({release:async()=>[],installerNewer:async()=>newer});
  await updater.check();
  assert.equal(updater.current.phase,'error');assert.equal(updater.current.error,FAILED_START);assert.equal(updater.current.installer_newer,newer);
 }
 const {updater}=fixture();let asked=0;
 updater.useApplications(source({check:async()=>{throw Error('Source update request failed (503)');}}));
 updater.onFailedStart({release:async()=>{asked++;return [];},installerNewer:async()=>{asked++;return true;}});
 await updater.check();assert.equal(asked,0);assert.equal(updater.current.installer_newer,null);
 // A release that fails leaves the original refusal.
 const broken=fixture().updater;
 broken.useApplications(source({check:async()=>{throw Error(FAILED_START.slice(7));}}));
 broken.onFailedStart({release:async()=>{throw Error('locked');},installerNewer:async()=>false});
 await broken.check();assert.equal(broken.current.error,FAILED_START);assert.equal(broken.current.installer_newer,false);
});
test('restarting into an update says the new version is starting before the window goes', async () => {
 const {updater,calls}=fixture();const seen=[];
 updater.useApplications(source({check:async()=>ready,prepareInstall:async()=>calls.push('prepare')}),'C:/Programs/AI Shuffle/AI Shuffle.exe');
 updater.onRestarting(()=>calls.push('announce'));updater.subscribe(state=>seen.push(state.message));
 await updater.check();await updater.install();
 assert.deepEqual(calls.slice(0,2),['prepare','announce']);assert.equal(calls.at(-1),'quit');
 assert.match(seen.at(-1),/Starting the new version\. This can take a few minutes/);
 // A notification that cannot be shown never stops the restart.
 const second=fixture();second.updater.useApplications(source({check:async()=>ready}),'C:/Programs/AI Shuffle/AI Shuffle.exe');
 second.updater.onRestarting(()=>{throw Error('no notifications');});
 const error=console.error;console.error=()=>{};
 try{await second.updater.check();await second.updater.install();}finally{console.error=error;}
 assert.equal(second.calls.at(-1),'quit');
});
test('the restore point is made after the update is pending and before the restart, and discarded if the install then fails', async () => {
 const {updater,calls,app}=fixture();
 updater.useApplications(source({check:async()=>ready,prepareInstall:async()=>calls.push('pending'),cancelInstall:async()=>calls.push('cancel')}),'C:/Programs/AI Shuffle/AI Shuffle.exe');
 updater.onBeforeInstall(async()=>calls.push('stop'));
 updater.onInstallPrepared(async()=>calls.push('point'),async()=>calls.push('discard'));
 await updater.check();await updater.install();
 assert.deepEqual(calls.slice(0,3),['stop','pending','point']);assert.equal(calls.at(-1),'quit');assert(!calls.includes('discard'));
 // The restart itself fails: the point goes before the update stops being pending.
 const second=fixture();const order=second.calls;
 second.app.relaunch=()=>{throw Error('cannot relaunch');};
 second.updater.useApplications(source({check:async()=>ready,cancelInstall:async()=>order.push('cancel')}),'C:/Programs/AI Shuffle/AI Shuffle.exe');
 second.updater.onInstallPrepared(async()=>order.push('point'),async()=>order.push('discard'));
 second.updater.onRestartCancelled(()=>order.push('close notice'));
 second.updater.onInstallFailed(async()=>order.push('start stack'));
 await second.updater.check();await second.updater.install();
 assert.deepEqual(order,['point','close notice','discard','cancel','start stack']);assert.match(second.updater.current.error,/cannot relaunch/);
 // A copy that fails is the install's error; nothing is restarted.
 const third=fixture();
 third.updater.useApplications(source({check:async()=>ready}),'C:/Programs/AI Shuffle/AI Shuffle.exe');
 third.updater.onInstallPrepared(async()=>{throw Error('Update B was not started because its database restore point failed: disk full');},async()=>{});
 await third.updater.check();await third.updater.install();
 assert.match(third.updater.current.error,/disk full/);assert(!third.calls.includes('quit'));
});
test('a restore point that cannot be discarded keeps the update pending and never reopens the database', async () => {
 const {updater,calls,app}=fixture();const order=[];
 app.relaunch=()=>{throw Error('cannot relaunch');};
 updater.useApplications(source({check:async()=>ready,cancelInstall:async()=>order.push('cancel')}),'C:/Programs/AI Shuffle/AI Shuffle.exe');
 updater.onInstallPrepared(async()=>order.push('point'),async()=>{order.push('discard');throw Error('EBUSY: restore-point.json is locked');});
 updater.onInstallFailed(async()=>order.push('start stack'));
 updater.onRestartCancelled(()=>order.push('close notice'));
 const error=console.error;console.error=()=>{};
 try{await updater.check();await updater.install();}finally{console.error=error;}
 // Neither cancelInstall (the update stays pending) nor a restart of the stack.
 assert.deepEqual(order,['point','close notice','discard']);assert(!calls.includes('quit'));
 assert.equal(updater.current.phase,'ready');
 assert.match(updater.current.error,/cannot relaunch[\s\S]*Quit AI Shuffle and open it again to finish installing/);
});
test('an install whose database copy failed ends with nothing staged and says so plainly', async () => {
 const {updater}=fixture();const manager=source({check:async()=>ready,pendingVersion:'b'.repeat(40)});
 updater.useApplications(manager,'C:/Programs/AI Shuffle/AI Shuffle.exe');
 updater.onInstallPrepared(async()=>{manager.pendingVersion=null;throw Error('The update was not installed: AI Shuffle could not save a copy of your database first (disk full).');},async()=>{});
 let restarted=0;updater.onInstallFailed(async()=>{restarted++;});
 await updater.check();await updater.install();
 assert.equal(updater.current.phase,'error');assert.match(updater.current.error,/could not save a copy of your database first \(disk full\)/);
 assert.doesNotMatch(updater.current.error,/did not finish|went back/);assert.equal(restarted,1,'the running version starts its stack again');
});
test('#98: a failed download of a newer release keeps the staged one ready to install', async () => {
 const { updater, calls } = fixture();
 const staged = 'c'.repeat(40);
 updater.useApplications(source({ pendingVersion: staged, cleanupError: null, retryCleanup: async () => {},
  check: async () => available, download: async () => { throw Error('Source archive checksum or size mismatch'); },
  releaseInfo: async () => ({ release: '1.0.0', notes: 'Staged notes' }), prepareInstall: async () => calls.push('prepare') }));
 // A modern manager with a staged build checks the feed at once (#98).
 await new Promise(setImmediate); assert.equal(updater.current.phase, 'available');
 await updater.download();
 assert.equal(updater.current.phase, 'ready'); assert.equal(updater.current.version, staged.slice(0, 12));
 assert.match(updater.current.error, /checksum/);
 await new Promise(setImmediate); assert.equal(updater.current.release, '1.0.0');
 updater.onBeforeInstall(async () => calls.push('stop')); await updater.install();
 assert.deepEqual(calls.slice(0, 2), ['stop', 'prepare']);
 // Without a staged build the failure is an error, as before.
 const plain = fixture(); plain.updater.useApplications(source({ download: async () => { throw Error('offline'); } }));
 await plain.updater.check(); await plain.updater.download(); assert.equal(plain.updater.current.phase, 'error');
});

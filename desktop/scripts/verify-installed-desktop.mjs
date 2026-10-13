// Real NSIS -> existing-profile upgrade -> signed stable update -> same EXE launch.
// Run only on a fresh Windows CI machine. Never upload its profile; only the
// redacted logs listed in evidenceLogs are copied into the evidence folder.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { spawn, spawnSync } from 'node:child_process';
import { installPreview } from './install-preview.mjs';
import { closeInstalledDesktop } from './installed-desktop-lifecycle.mjs';
import { configurePersistenceFixture, verifyOfflineSetup, workerJson } from '../../tests/integration/container_ui_smoke.mjs';
assert(process.platform==='win32'&&process.env.CI==='true','Fresh Windows CI only');
// No person is here to acknowledge the first-launch agent access disclosure.
process.env.SHUFFLE_DESKTOP_ACCEPT_AGENT_ACCESS='1';
const [installerArg,evidenceArg]=process.argv.slice(2);
const installer=path.resolve(installerArg),output=path.resolve(evidenceArg),temporary=path.resolve(process.env.RUNNER_TEMP);
assert(output.startsWith(temporary+path.sep));
const install=path.join(temporary,'shuffle-stable-installed'),profile=path.join(process.env.APPDATA,'ai-shuffle-desktop');
const sourceRoot=path.join(profile,'updates'),exe=path.join(install,'AI Shuffle.exe');
const exists=p=>fs.stat(p).then(()=>true,e=>{if(e.code==='ENOENT')return false;throw e;});
assert(!await exists(install)&&!await exists(profile),'Refusing to use an existing installation/profile');
assert(!await exists(path.join(process.env.USERPROFILE,'.ai-shuffle')),'Fresh worker data required');
await fs.mkdir(output,{recursive:true});
const report={success:false,checks:[]};
const passed=message=>{report.checks.push(message);console.log('PASS: '+message);};
const electronLog=path.join(temporary,'installed-desktop.log');
const log=await fs.open(electronLog,'a');
let chromium,browser,page;
// The installed app's own logs (desktop/README.md), so a failed start names its
// cause. Only these files, never the database, descriptors, keys or browser state.
const evidenceLogs=[['logs','worker.log'],['logs','worker.prev.log'],['logs','postgres.log'],['updates','build.log'],['updates','launch.log']]
 .map(([folder,name])=>({key:folder+'/'+name,file:path.join(profile,folder,name),name}));
evidenceLogs.push({key:'installed-desktop.log',file:electronLog,name:'installed-desktop.log'});
const LOG_TAIL_CHARS=8*1024*1024;
const redact=line=>line.replace(/github_pat_[A-Za-z0-9_]+|\bgh[pousr]_[A-Za-z0-9]{20,}|\bsk-[A-Za-z0-9_-]{16,}/g,'[redacted]')
 .replace(/\bBearer\s+[^\s"',]+/gi,'Bearer [redacted]').replace(/:\/\/[^\/\s:@]+:[^@\s]+@/g,'://[redacted]@')
 .replace(/\b([\w-]*(?:token|password|passwd|secret|authorization|api[_-]?key|cookie)[\w-]*)(["']?\s*[:=]\s*["']?)[^\s"',&]+/gi,'$1$2[redacted]');
async function collectLogs(){
 const target=path.join(output,'logs');await fs.mkdir(target,{recursive:true});report.logs={};
 for(const {key,file,name} of evidenceLogs){let text;
  try{text=await fs.readFile(file,'utf8');}catch(error){report.logs[key]=error.code==='ENOENT'?'missing':String(error);continue;}
  const truncated=text.length>LOG_TAIL_CHARS;const lines=(truncated?text.slice(-LOG_TAIL_CHARS):text).split(/\r?\n/).map(redact);
  await fs.writeFile(path.join(target,name),lines.join('\n'));report.logs[key]=truncated?'copied (tail)':'copied';
  if(!report.success&&name==='worker.log')console.error('--- worker.log (last 60 lines) ---\n'+lines.slice(-60).join('\n'));}
}
async function until(fn,message,timeout=120000){const end=Date.now()+timeout;while(Date.now()<end){if(await fn())return;await new Promise(r=>setTimeout(r,500));}throw Error(message);}
// The installed launcher and updated Electron overlap briefly during handoff.
// Let Chromium allocate a port for each process instead of racing for one port.
async function connect(){await until(async()=>{
 let candidate;
 try{
  const port=(await fs.readFile(path.join(profile,'DevToolsActivePort'),'utf8')).split(/\r?\n/)[0];assert.match(port,/^[1-9]\d{0,4}$/);
  candidate=await chromium.connectOverCDP('http://127.0.0.1:'+port,{timeout:1500});
  const target=candidate.contexts().flatMap(c=>c.pages()).find(p=>p.url().startsWith('http://127.0.0.1:8000/'));
  if(target&&await target.evaluate(()=>!!window.shuffleDesktop)){browser=candidate;page=target;return true;}
 }catch{}
 if(candidate)await candidate.close().catch(()=>{});
 return false;
},'Installed desktop debugger and worker did not become ready',180000);}
const launched=[];
async function launch(){const child=spawn(exe,['--remote-debugging-port=0'],{windowsHide:true,stdio:['ignore',log.fd,log.fd]});launched.push(child);child.on('error',e=>console.error(e));await connect();}
async function close(){
 await closeInstalledDesktop(page,browser,()=>until(async()=>{try{await fetch('http://127.0.0.1:8000/health/ready',{signal:AbortSignal.timeout(500)});return false;}catch{return true;}},'Worker did not stop',90000));
 // A bundled launch owns the lock in this child; source handoff children have
 // already exited. Do not immediately relaunch against a still-held lock.
 await until(()=>launched.every(child=>child.exitCode!==null||child.signalCode!==null),'Installed launcher did not exit',90000);
 browser=null;page=null;}
async function auth(){return page.evaluate(async()=>{const r=await fetch('/api/v1/auth/status');const s=await r.json();return {setup:s.setup_required,onboarding:s.onboarding_complete};});}
async function screenshot(name){await page.screenshot({path:path.join(output,name+'.png'),mask:[page.locator('code, textarea, input')]});}
async function requestUpdate(action){
 // Use the same authenticated owner API as the sidebar. The sidebar itself
 // is unavailable until credential-backed onboarding has completed.
 const phase=action==='install'?'ready':'available';
 await until(async()=>{
  const snapshot=await workerJson(page,'/api/v1/settings/application-updates/workers');
  return snapshot.workers.some(worker=>worker.supported&&worker.online&&worker.allow_updates&&worker.phase===phase);
 },`Worker update bridge did not report ${phase}`,60000);
 const receipt=await workerJson(page,'/api/v1/settings/application-updates','POST',{action});
 assert(receipt.results.some(result=>result.accepted),`Update ${action} was refused: ${JSON.stringify(receipt)}`);
}
try{
 await installPreview(installer,install,()=>console.log('Installing verified Windows package…'));
 assert(await exists(exe));
 const buildPath=path.join(install,'resources/worker/desktop-build.json');
 const build=JSON.parse(await fs.readFile(buildPath,'utf8'));report.installerRevision=build.revision;
 ({chromium}=await import(pathToFileURL(path.join(install,'resources/worker/server/browser-mcp/node_modules/playwright-core/index.mjs')).href));
 await launch();
 const before=await page.evaluate(async()=>({worker:await window.shuffleDesktop.worker.getState(),updates:await window.shuffleDesktop.updates.getState()}));
 assert.equal(before.worker.external,false);assert(before.worker.logPath.toLowerCase().startsWith(profile.toLowerCase()));assert.notEqual(before.updates.phase,'unsupported');
 assert(!await exists(path.join(sourceRoot,'versions',build.revision)),'First startup must run bundled code without a source build');
 const setupMode=await verifyOfflineSetup(page);
 const workspace=path.join(temporary,'workspace');await fs.mkdir(workspace,{recursive:true});
 const workspaceURL=await configurePersistenceFixture(page,workspace);
 const expected=await auth();assert.equal(expected.onboarding,false);
 const network=await workerJson(page,'/api/v1/network/status');
 assert(network.instance_id,'Persistence fixture must have an instance identity');
 const persisted=async()=>{
  assert.deepEqual(await auth(),expected);
  assert.equal((await workerJson(page,workspaceURL)).workspace_path,workspace);
  assert.equal((await workerJson(page,'/api/v1/network/status')).instance_id,network.instance_id);
  const credentials=await workerJson(page,'/api/v1/settings/claude-auth');
  assert.equal(credentials.mode,setupMode);assert.equal(credentials.gate.ready,false);
  assert.equal(await fs.readFile(path.join(profile,'installer-preservation.txt'),'utf8'),'keep existing user data');
 };
 await fs.writeFile(path.join(profile,'installer-preservation.txt'),'keep existing user data');
 await screenshot('installed-app');await close();passed('Normal installer starts bundled app, enforces the credential gate and enables stable updates');
 await installPreview(installer,install,()=>console.log('Verifying upgrade of the same installation…'));
 assert.equal(await fs.readFile(path.join(profile,'installer-preservation.txt'),'utf8'),'keep existing user data');
 await launch();await persisted();await verifyOfflineSetup(page);await close();passed('Installing over the same location preserves unfinished setup, workspace, instance and user profile');
 // Change only this disposable installed copy's baseline metadata to model an
 // earlier release. The installer asset, trust key, updater, IPC and public
 // signed stable archive are untouched. This forces a real first source update.
 await fs.writeFile(buildPath,JSON.stringify({revision:'0'.repeat(40),sequence:1}));
 // The earlier release is also the last code that opened this database. The
 // launcher's migration fence otherwise sees the installer revision as newer
 // than the faked baseline and correctly refuses to boot the older bundle.
 await fs.writeFile(path.join(sourceRoot,'database-floor.json'),JSON.stringify({schemaVersion:1,revision:'0'.repeat(40),sequence:1}));
 await launch();await page.evaluate(()=>window.shuffleDesktop.updates.check());
 await until(async()=>{const state=await page.evaluate(()=>window.shuffleDesktop.updates.getState());if(state.phase==='error')throw Error(state.error);return state.phase==='available';},'Stable update was not offered',45000);
 await requestUpdate('download');
 await until(async()=>{const state=await page.evaluate(()=>window.shuffleDesktop.updates.getState());if(state.phase==='error')throw Error(state.error);return ['downloading','ready'].includes(state.phase);},'Accepted stable download did not start within the request window',90000);
 passed('Accepted stable download starts through the worker update bridge');
 await until(async()=>{const state=await page.evaluate(()=>window.shuffleDesktop.updates.getState());if(state.phase==='error')throw Error(state.error);return state.phase==='ready';},'Stable update did not finish building',1200000);
 const staged=JSON.parse(await fs.readFile(path.join(sourceRoot,'state.json'),'utf8'));
 // Download stages the build; only the explicit install action may activate it.
 const updatedRevision=staged.staged;assert.equal(updatedRevision,build.revision);assert.equal(staged.pending,undefined);report.updatedRevision=updatedRevision;
 await screenshot('update-ready');
 const disconnected=new Promise(resolve=>browser.once('disconnected',resolve));
 await requestUpdate('install');await Promise.race([disconnected,new Promise((_,reject)=>setTimeout(()=>reject(Error('Installed app did not exit')),90000))]);
 await connect();assert.equal(await page.evaluate(()=>window.shuffleDesktop.version),'main@'+updatedRevision.slice(0,12));await persisted();
 await until(async()=>JSON.parse(await fs.readFile(path.join(sourceRoot,'state.json'),'utf8')).active===updatedRevision,'Source update did not commit healthy state');
 const host=JSON.parse(await fs.readFile(path.join(sourceRoot,'installed-host.json'),'utf8'));assert.equal(host.executable,exe);assert.equal(host.userData,profile);
 const after=await page.evaluate(()=>window.shuffleDesktop.worker.getState());assert(after.logPath.toLowerCase().startsWith(profile.toLowerCase()));assert.notEqual(after.pid,before.worker.pid);
 await screenshot('updated-same-app');await close();passed('Real signed stable code builds through the owner update API and restarts with existing database and unfinished setup');
 await launch();assert.equal(await page.evaluate(()=>window.shuffleDesktop.version),'main@'+updatedRevision.slice(0,12));await persisted();await verifyOfflineSetup(page);await close();
 passed('Original installed EXE opens the updated revision on subsequent launches');
 report.gaps=['Offline setup stops at the real credential gate; no provider login, completed onboarding, model call or post-setup sidebar interaction was exercised. Update download and install use the same authenticated owner API as the sidebar.'];
 report.success=true;
}catch(error){report.error=String(error);console.error(error);process.exitCode=1;
 try{report.updateState=await page.evaluate(()=>window.shuffleDesktop.updates.getState());}catch{}
 try{report.sourceState=JSON.parse(await fs.readFile(path.join(sourceRoot,'state.json'),'utf8'));}catch{}
 try{report.workerReady=(await fetch('http://127.0.0.1:8000/health/ready',{signal:AbortSignal.timeout(1000)})).status;}catch{}
 try{const launchLog=await fs.readFile(path.join(sourceRoot,'launch.log'),'utf8');report.debuggerPorts=[...launchLog.matchAll(/DevTools listening on ws:\/\/127\.0\.0\.1:(\d+)/g)].map(match=>Number(match[1]));}catch{}
 // Build children receive a credential-free environment. The redacted logs
 // themselves are copied by collectLogs; this keeps a short error summary.
 try{const buildLog=await fs.readFile(path.join(sourceRoot,'build.log'),'utf8');report.buildErrors=buildLog.split(/\r?\n/).filter(line=>/error|failed|traceback|cannot|no such|not found|not recognized/i.test(line)).slice(-25).map(line=>redact(line).slice(0,1000));console.error(report.buildErrors.join('\n'));}catch{}
 if(page&&!page.isClosed())await screenshot('failure').catch(()=>{});}
finally{if(page)await close().catch(()=>{});await log.close();await collectLogs().catch(error=>{report.logsError=String(error);});await fs.writeFile(path.join(output,'acceptance.json'),JSON.stringify(report,null,2));}
// A failed launch can leave the app (and its worker and PostgreSQL) running,
// which keeps Node alive until the job timeout. Stop them and exit now.
if(process.exitCode)for(const child of launched)if(child.exitCode===null&&child.pid)spawnSync('taskkill',['/pid',String(child.pid),'/T','/F'],{stdio:'ignore'});
process.exit(process.exitCode??0);

// Launcher replacement acceptance (#1821 D5, desktop/LAUNCHER.md) on a fresh
// Windows CI machine: install a published installer whose launcher is older,
// stage this checkout as a signed revision that needs a newer launcher, start
// it, and check that the updated app swaps app.asar, that the new launcher then
// starts it twice, and that the installer's desktop-build.json is unchanged.
//
// Publishes nothing and uses no secret. The release key is an ephemeral key
// made here: its public half replaces build/application-update-key.pem in the
// installed app.asar and in the staged source, so no real signing key is
// needed and nothing it signs is trusted by any other install.
import assert from 'node:assert/strict';
import { createHash, generateKeyPairSync, sign } from 'node:crypto';
import fs from 'node:fs/promises';
import { createRequire } from 'node:module';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { spawn, execFileSync } from 'node:child_process';
import { installPreview } from './install-preview.mjs';
import { readLauncher } from './launcher-contract.mjs';
assert(process.platform==='win32'&&process.env.CI==='true','Fresh Windows CI only');
process.env.SHUFFLE_DESKTOP_ACCEPT_AGENT_ACCESS='1';
const require=createRequire(import.meta.url);
const desktop=path.resolve(import.meta.dirname,'..'),repo=path.resolve(desktop,'..');
// This checkout's built launcher code (npm run build), the code being accepted.
const { SourceUpdates, verifySourceManifest }=require('../dist/main/source-updates.js');
const { SOURCE_ORIGIN }=require('../dist/main/source-auth.js');
const { readAsarMember }=require('../dist/main/asar.js');
const asar=require('@electron/asar');
const [installerArg,evidenceArg]=process.argv.slice(2);
const installer=path.resolve(installerArg),output=path.resolve(evidenceArg),temporary=path.resolve(process.env.RUNNER_TEMP);
assert(output.startsWith(temporary+path.sep));
const install=path.join(temporary,'shuffle-launcher-installed'),profile=path.join(process.env.APPDATA,'ai-shuffle-desktop');
const sourceRoot=path.join(profile,'updates'),exe=path.join(install,'AI Shuffle.exe'),resources=path.join(install,'resources');
const appAsar=path.join(resources,'app.asar'),oldAsar=path.join(resources,'app.asar.old'),newAsar=path.join(resources,'app.new.asar');
const markerPath=path.join(sourceRoot,'launcher-swap.json');
const exists=p=>fs.stat(p).then(()=>true,e=>{if(e.code==='ENOENT')return false;throw e;});
const sha256=data=>createHash('sha256').update(data).digest('hex');
const fileHash=async p=>sha256(await fs.readFile(p));
const marker=async()=>{try{return JSON.parse(await fs.readFile(markerPath,'utf8'));}catch(e){if(e.code==='ENOENT')return null;throw e;}};
const asarLauncher=file=>readLauncher(readAsarMember(require('node:fs'),file,'launcher.json').toString('utf8')).version;
assert(!await exists(install)&&!await exists(profile),'Refusing to use an existing installation/profile');
assert(!await exists(path.join(process.env.USERPROFILE,'.ai-shuffle')),'Fresh worker data required');
await fs.mkdir(output,{recursive:true});
const report={success:false,checks:[]};
const passed=message=>{report.checks.push(message);console.log('PASS: '+message);};
const log=await fs.open(path.join(temporary,'launcher-swap.log'),'a');
let chromium,browser,page;
const evidenceLogs=[['logs','worker.log'],['logs','worker.prev.log'],['updates','build.log'],['updates','launch.log']];
const LOG_TAIL_CHARS=8*1024*1024;
const redact=line=>line.replace(/github_pat_[A-Za-z0-9_]+|\bgh[pousr]_[A-Za-z0-9]{20,}|\bsk-[A-Za-z0-9_-]{16,}/g,'[redacted]')
 .replace(/\bBearer\s+[^\s"',]+/gi,'Bearer [redacted]').replace(/:\/\/[^\/\s:@]+:[^@\s]+@/g,'://[redacted]@')
 .replace(/\b([\w-]*(?:token|password|passwd|secret|authorization|api[_-]?key|cookie)[\w-]*)(["']?\s*[:=]\s*["']?)[^\s"',&]+/gi,'$1$2[redacted]');
async function collectLogs(){
 const target=path.join(output,'logs');await fs.mkdir(target,{recursive:true});report.logs={};
 for(const [folder,name] of evidenceLogs){const key=folder+'/'+name;let text;
  try{text=await fs.readFile(path.join(profile,folder,name),'utf8');}catch(error){report.logs[key]=error.code==='ENOENT'?'missing':String(error);continue;}
  const truncated=text.length>LOG_TAIL_CHARS;const lines=(truncated?text.slice(-LOG_TAIL_CHARS):text).split(/\r?\n/).map(redact);
  await fs.writeFile(path.join(target,name),lines.join('\n'));report.logs[key]=truncated?'copied (tail)':'copied';
  if(!report.success&&['worker.log','launch.log'].includes(name))console.error(`--- ${name} (last 60 lines) ---\n`+lines.slice(-60).join('\n'));}
}
async function until(fn,message,timeout=120000){const end=Date.now()+timeout;while(Date.now()<end){if(await fn())return;await new Promise(r=>setTimeout(r,500));}throw Error(message);}
// As verify-installed-desktop.mjs: the launcher and the updated app each pick a debugger port.
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
},'Installed desktop debugger and worker did not become ready',300000);}
async function launch(){await fs.rm(path.join(profile,'DevToolsActivePort'),{force:true});
 const child=spawn(exe,['--remote-debugging-port=0'],{windowsHide:true,stdio:['ignore',log.fd,log.fd]});child.on('error',e=>console.error(e));await connect();}
async function close(){if(page&&!page.isClosed())await page.evaluate(()=>window.shuffleDesktop.window.close()).catch(()=>{});
 await until(async()=>{try{await fetch('http://127.0.0.1:8000/health/ready',{signal:AbortSignal.timeout(500)});return false;}catch{return true;}},'Worker did not stop',90000);
 if(browser)await browser.close().catch(()=>{});browser=null;page=null;
 // The next start takes the single-instance lock this app holds until it has exited.
 await new Promise(r=>setTimeout(r,5000));}
const version=()=>page.evaluate(()=>window.shuffleDesktop.version);
try{
 await installPreview(installer,install,()=>console.log('Installing the older published installer…'));
 assert(await exists(exe));
 const buildPath=path.join(resources,'worker/desktop-build.json');
 const buildText=await fs.readFile(buildPath,'utf8');const build=JSON.parse(buildText);
 const target=readLauncher(await fs.readFile(path.join(desktop,'launcher.json'),'utf8')).version;
 report.installerRevision=build.revision;report.installerLauncher=build.launcher??0;report.targetLauncher=target;
 assert(report.installerLauncher<target,'The installer already has this launcher; nothing to replace');
 assert(!await exists(path.join(resources,'app.asar.unpacked')),'An unpacked launcher folder is not handled here');
 ({chromium}=await import(pathToFileURL(path.join(resources,'worker/server/browser-mcp/node_modules/playwright-core/index.mjs')).href));

 // The ephemeral release key, trusted by this installed launcher only.
 const { publicKey, privateKey }=generateKeyPairSync('ed25519');
 const pem=publicKey.export({type:'spki',format:'pem'});
 const unpacked=path.join(temporary,'old-launcher');
 asar.extractAll(appAsar,unpacked);
 assert(await exists(path.join(unpacked,'build/application-update-key.pem')));
 assert(!await exists(path.join(unpacked,'launcher.json')),'The installed launcher already carries launcher.json');
 await fs.writeFile(path.join(unpacked,'build/application-update-key.pem'),pem);
 await fs.rm(appAsar);await asar.createPackage(unpacked,appAsar);
 const oldHash=await fileHash(appAsar);
 await launch();report.installerVersion=await version();await close();
 passed(`Older installer (launcher ${report.installerLauncher}) starts its bundled app with the ephemeral release key`);

 // This checkout as a signed revision: the real archive script over a commit
 // whose only change is the ephemeral public key.
 execFileSync('git',['config','core.autocrlf','false'],{cwd:repo});
 const index=path.join(temporary,'acceptance.index'),gitEnv={...process.env,GIT_INDEX_FILE:index,
  GIT_AUTHOR_NAME:'acceptance',GIT_AUTHOR_EMAIL:'acceptance@invalid',GIT_COMMITTER_NAME:'acceptance',GIT_COMMITTER_EMAIL:'acceptance@invalid'};
 const git=(args,input)=>execFileSync('git',args,{cwd:repo,env:gitEnv,input,encoding:'utf8'}).trim();
 const head=git(['rev-parse','HEAD']);git(['read-tree',head]);
 const blob=git(['hash-object','-w','--stdin'],pem);
 git(['update-index','--cacheinfo',`100644,${blob},desktop/build/application-update-key.pem`]);
 const revision=git(['commit-tree',git(['write-tree']),'-p',head,'-m','Launcher acceptance revision']);
 const zip=path.join(temporary,'acceptance-source.zip');
 const archive=JSON.parse(execFileSync('python',['scripts/source_archive.py',revision,zip],{cwd:repo,encoding:'utf8'}));
 const manifest={format:2,revision,sequence:build.sequence+1,sha256:archive.sha256,size:archive.size,channel:'stable',tree:archive.tree,desktop:{launcher:target}};
 const payload=Buffer.from(JSON.stringify(manifest));
 const signed=JSON.stringify({payload:payload.toString('base64'),signature:sign(null,payload,privateKey).toString('base64')});
 assert.equal(verifySourceManifest(signed,pem).desktop.launcher,target);
 report.revision=revision;report.head=head;

 // Stage it with this checkout's updater, fed locally instead of from the hub.
 const feed={[SOURCE_ORIGIN+'/v1/desktop/manifest']:async()=>new Response(signed),
  [SOURCE_ORIGIN+'/v1/desktop/sources/'+revision]:async()=>new Response(await fs.readFile(zip))};
 const updates=new SourceUpdates({root:sourceRoot,channel:'stable',publicKey:pem,baseline:{revision:build.revision,sequence:build.sequence},
  bootstrapNode:path.join(resources,'worker/node/node.exe'),fetch:async url=>feed[String(url)]?.()??new Response('not found',{status:404})});
 await updates.load();
 let last='';
 const staged=await updates.download((_,text)=>{if(text&&text!==last)console.log(last=text);});
 assert.equal(staged.kind,'ready');
 assert.equal(JSON.parse(await fs.readFile(path.join(sourceRoot,'state.json'),'utf8')).pending,revision);
 passed('A revision that needs launcher '+target+' is built and pending');

 // 1. The old launcher starts it; once healthy, the updated app swaps app.asar.
 await launch();
 assert.equal(await version(),'main@'+revision.slice(0,12));
 await until(async()=>JSON.parse(await fs.readFile(path.join(sourceRoot,'state.json'),'utf8')).active===revision,'The revision did not commit healthy state');
 await until(async()=>{const m=await marker();if(m?.phase==='rejected')throw Error('Self-test rejected the new launcher: '+m.reason);return m?.phase==='trial';},'The updated app did not replace the launcher',300000);
 let record=await marker();report.swap=record;
 assert.deepEqual([record.revision,record.from,record.to,record.starts,record.old],[revision,report.installerLauncher,target,0,oldHash]);
 assert.equal(await fileHash(appAsar),record.new);assert.equal(await fileHash(oldAsar),oldHash);assert(!await exists(newAsar));
 assert.equal(asarLauncher(appAsar),target);
 assert.equal(readAsarMember(require('node:fs'),appAsar,'package.json').toString(),readAsarMember(require('node:fs'),oldAsar,'package.json').toString());
 await close();
 passed(`The updated app replaced launcher ${report.installerLauncher} with ${target}, kept app.asar.old, and the self-test passed`);

 // 2. First start of the new launcher: on trial, then confirmed by the app it started.
 await launch();
 assert.equal(await version(),'main@'+revision.slice(0,12));
 await until(async()=>(await marker())?.phase==='confirmed','The new launcher was not confirmed',60000);
 record=await marker();assert.equal(record.starts,1);
 const host=JSON.parse(await fs.readFile(path.join(sourceRoot,'installed-host.json'),'utf8'));
 assert.equal(host.executable,exe);assert.equal(host.userData,profile);
 await close();
 passed('The new launcher started the updated app with SHUFFLE_LAUNCHER_VERSION '+target+', which confirmed it, in the same profile');

 // 3. Second start: still the new launcher, nothing restored, the installer stamp untouched.
 await launch();
 assert.equal(await version(),'main@'+revision.slice(0,12));
 assert.equal((await marker()).phase,'confirmed');
 assert.equal(await fileHash(appAsar),record.new);assert.equal(asarLauncher(appAsar),target);assert(await exists(oldAsar));
 assert.equal(await fs.readFile(buildPath,'utf8'),buildText);
 await close();
 passed('A second restart keeps launcher '+target+' and app.asar.old; desktop-build.json is unchanged');
 report.gaps=['The release key is ephemeral and the source is staged by this checkout\'s updater from a local feed, not downloaded from the hub by the installed app.',
  'The restore path (a new launcher that fails before the updated app confirms it) is unit-tested only.'];
 report.success=true;
}catch(error){report.error=String(error);console.error(error);process.exitCode=1;
 try{report.sourceState=JSON.parse(await fs.readFile(path.join(sourceRoot,'state.json'),'utf8'));}catch{}
 try{report.marker=await marker();}catch{}
 try{report.files=Object.fromEntries(await Promise.all([appAsar,oldAsar,newAsar].map(async p=>[path.basename(p),await exists(p)?await fileHash(p):null])));}catch{}
 try{const buildLog=await fs.readFile(path.join(sourceRoot,'build.log'),'utf8');report.buildErrors=buildLog.split(/\r?\n/).filter(line=>/error|failed|traceback|cannot|no such|not found|not recognized/i.test(line)).slice(-25).map(line=>redact(line).slice(0,1000));}catch{}}
finally{if(page)await close().catch(()=>{});await log.close();await collectLogs().catch(error=>{report.logsError=String(error);});await fs.writeFile(path.join(output,'acceptance.json'),JSON.stringify(report,null,2));}

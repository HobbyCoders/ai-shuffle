import assert from 'node:assert/strict';
import {promises as fs} from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import {createRequire} from 'node:module';
import {spawn,execFileSync} from 'node:child_process';
import {generateKeyPairSync,createHash,sign} from 'node:crypto';
import {fileURLToPath} from 'node:url';
const repo=path.resolve(fileURLToPath(new URL('../..',import.meta.url)));
const root=path.resolve(process.argv[2]??path.join(os.tmpdir(),'shuffle-desktop-e2e'));
const require=createRequire(path.join(repo,'browser-mcp/package.json'));
const {chromium}=require('playwright-core');
const {isolatedRoot,readProfile}=createRequire(import.meta.url)('./source-profile.cjs');
isolatedRoot(root);
const {isPortFree}=createRequire(import.meta.url)('../dist/main/worker.js');
for(const port of [18080,18081,19228])assert(await isPortFree(port),`Test port ${port} is busy`);
const reuse=process.argv.includes('--reuse');
try{
 await fs.access(root);
 if(!reuse)throw Error('Use a new, empty test root (or --reuse for this harness’s stopped test profile)');
 const previous=JSON.parse(await fs.readFile(path.join(root,'report.json'),'utf8')),profile=readProfile(root);
 assert.equal(previous.root,root);assert.equal(profile?.isolated,true);assert.equal(profile.feedDirectory,path.join(root,'channel'));
 await fs.rm(path.join(root,'profile'),{recursive:true,force:true});
 await fs.rm(path.join(root,'state.json'),{force:true});
 await fs.rm(path.join(root,'launch-profile.json'),{force:true});
}catch(e){if(e.code!=='ENOENT'||reuse)throw e;}
await fs.mkdir(path.join(root,'channel'),{recursive:true});
const report={root,steps:[],screenshots:[]};
const progress=text=>{report.steps.push(text);console.log(text);};
const production=async()=>{const r=await fetch('http://127.0.0.1:8000/health/ready',{signal:AbortSignal.timeout(3000)});assert.equal(r.status,200);return r.status;};
report.productionBefore=await production();
const listenerPids=()=>JSON.parse(execFileSync('powershell.exe',['-NoProfile','-NonInteractive','-Command',"@(Get-NetTCPConnection -State Listen -LocalPort 8000,8001 | Select-Object LocalPort,OwningProcess) | ConvertTo-Json -Compress"],{encoding:'utf8'}));
report.productionListenersBefore=listenerPids();
const files=execFileSync('git',['ls-files','-co','--exclude-standard','-z'],{cwd:repo,encoding:'utf8'}).split('\0').filter(Boolean);
const source=path.join(root,'snapshot');await fs.mkdir(source);
for(const file of new Set(files)){
 if(file.startsWith('.fluid/')||path.basename(file)==='.env')continue;
 const from=path.join(repo,file);let stat;try{stat=await fs.lstat(from);}catch(e){if(e.code==='ENOENT')continue;throw e;}
 if(!stat.isFile())continue;
 await fs.mkdir(path.dirname(path.join(source,file)),{recursive:true});await fs.copyFile(from,path.join(source,file));
}
const {privateKey,publicKey}=generateKeyPairSync('ed25519');const publicPem=publicKey.export({type:'spki',format:'pem'});
await fs.writeFile(path.join(root,'channel/key.pem'),publicPem);
const treeHash=createHash('sha256');
for(const file of [...new Set(files)].sort()){try{const data=await fs.readFile(path.join(source,file));treeHash.update(file).update(data);}catch(e){if(e.code!=='ENOENT'&&e.code!=='EISDIR')throw e;}}
const fingerprint=treeHash.digest('hex');
const revisions=[];
for(const label of ['A','B']){
 const revision=createHash('sha1').update(label+fingerprint).digest('hex');revisions.push(revision);
 const container=path.join(root,'pack-'+label),tree=path.join(container,'ai-shuffle-'+revision);await fs.mkdir(container);
 await fs.rename(source,tree);await fs.writeFile(path.join(tree,'acceptance-revision.txt'),label);
 const archive=path.join(root,'channel',revision+'.zip');
 execFileSync(process.env.PYTHON??'python',[path.join(repo,'desktop/test/helpers/source-archive.py'),'pack',container,archive],{stdio:'inherit'});
 const bytes=await fs.readFile(archive),payload=Buffer.from(JSON.stringify({format:2,revision,sequence:Date.now(),sha256:createHash('sha256').update(bytes).digest('hex'),size:bytes.length}));
 await fs.writeFile(path.join(root,'channel',label+'.json'),JSON.stringify({payload:payload.toString('base64'),signature:sign(null,payload,privateKey).toString('base64')}));
 await fs.rename(tree,source);await fs.rmdir(container);
}
await fs.rm(source,{recursive:true,force:true});
await fs.copyFile(path.join(root,'channel/A.json'),path.join(root,'channel/latest.json'));
report.revisions=revisions;
const log=await fs.open(path.join(root,'launcher.log'),'a');
const child=spawn(process.execPath,[path.join(repo,'desktop/scripts/bootstrap.mjs'),'--root',root,'--isolated','--port','18080','--protected-port','18081','--debug-port','19228','--feed-directory',path.join(root,'channel'),'--public-key',path.join(root,'channel/key.pem')],{cwd:repo,stdio:['ignore',log.fd,log.fd],windowsHide:true});
let exit=null;child.once('exit',(code,signal)=>{exit={code,signal};});
let browser,page,restarted=false;
async function connect(timeout=900000){
 const end=Date.now()+timeout;
 while(Date.now()<end){
  if(!restarted&&exit)throw Error('Launcher exited: '+JSON.stringify(exit));
  try{return await chromium.connectOverCDP('http://127.0.0.1:19228',{timeout:1500});}catch{}
  await new Promise(resolve=>setTimeout(resolve,1000));
 }
 throw Error('Timed out waiting for the real Electron application');
}
async function realPage(){
 const end=Date.now()+120000;
 while(Date.now()<end){
  if(!browser.isConnected())throw Error('Electron disconnected before its worker was ready');
  const pages=browser.contexts().flatMap(c=>c.pages()).filter(p=>!p.isClosed());
  report.lastTargets=pages.map(p=>p.url());
  const ready=pages.find(p=>p.url().startsWith('http://127.0.0.1:18080/'));
  if(ready){page=ready;await page.waitForLoadState('domcontentloaded');await page.waitForFunction(()=>!!window.shuffleDesktop);return page;}
  page=pages.find(p=>p.url().includes('/worker.html'))??pages[0];
  if(page)report.lastPageText=await page.locator('body').innerText().catch(()=>'(target changed)');
  await new Promise(resolve=>setTimeout(resolve,500));
 }
 throw Error('The Electron window did not load its isolated worker');
}
async function shot(name){const file=path.join(root,name+'.png');await page.screenshot({path:file});report.screenshots.push(file);}
async function verifyHeaderWidths(surface){
 for(const width of [1440,940,375]){
  await page.setViewportSize({width,height:900});
  if(!await page.locator('.sb-head').count())await page.getByRole('button',{name:/sidebar/i}).first().click();
  // Trial clicks verify the controls are not covered by the main title bar.
  await page.getByRole('button',{name:'Download update',exact:true}).click({trial:true});
  await page.locator('.sb-head').getByRole('button',{name:'Search',exact:true}).click({trial:true});
  const fits=await page.locator('.sb-head').evaluate(h=>h.scrollWidth<=h.clientWidth);assert(fits);
  await shot(`${surface}-header-${width}`);
 }
 await page.setViewportSize({width:1440,height:900});
}

try{
 progress('Launching the unmodified source launcher in an isolated profile.');
 // Publish B as soon as A is selected, not after CDP attaches (which can be slow).
 const publishNext=(async()=>{
  const deadline=Date.now()+900000;
  while(Date.now()<deadline){
   let state;try{state=JSON.parse(await fs.readFile(path.join(root,'state.json'),'utf8'));}catch{}
   if([state?.pending,state?.booting,state?.active].includes(revisions[0])){
    await fs.copyFile(path.join(root,'channel/B.json'),path.join(root,'channel/latest.json'));return;
   }
   await new Promise(resolve=>setTimeout(resolve,250));
  }
  throw Error('First source revision was not selected before the publication deadline');
 })();
 // Attach a rejection handler immediately while the first build is running.
 publishNext.catch(()=>{});
 browser=await connect();await publishNext;page=await realPage();
 const bridge=await page.evaluate(async()=>({version:window.shuffleDesktop?.version,worker:await window.shuffleDesktop?.worker.getState()}));
 assert.equal(bridge.version,'main@'+revisions[0].slice(0,12));assert.equal(bridge.worker.port,18080);assert.equal(bridge.worker.external,false);
 assert(bridge.worker.logPath.toLowerCase().startsWith(root.toLowerCase()));report.firstWorker=bridge.worker;
 progress('Real Electron preload/IPC and separate worker are ready.');
 await page.getByLabel('Name for this worker').fill('Isolated source update test');
 await page.getByRole('button',{name:'Continue without an account',exact:true}).click();
 const workspace=page.getByLabel('Default workspace folder');if(await workspace.count())await workspace.fill(path.join(root,'workspace'));
 await page.getByRole('button',{name:'Continue',exact:true}).click();
 await page.getByRole('button',{name:'Finish without a provider',exact:true}).click();
 await page.getByRole('button',{name:'More',exact:true}).waitFor({timeout:15000});
 await shot('first-real-desktop');
 const downloadButton=page.getByRole('button',{name:'Download update',exact:true});
 await downloadButton.waitFor({state:'visible',timeout:45000});
 const beforeDownload=await page.evaluate(async()=>(await window.shuffleDesktop.updates.getState()).phase);
 assert.equal(beforeDownload,'available');
 const preState=JSON.parse(await fs.readFile(path.join(root,'state.json'),'utf8'));
 assert.equal(preState.pending,undefined);
 await assert.rejects(fs.access(path.join(root,'versions',revisions[1])));
 const searchBox=await page.locator('.sb-head').getByRole('button',{name:'Search',exact:true}).boundingBox();
 const updateBox=await downloadButton.boundingBox();
 assert(searchBox&&updateBox&&updateBox.x+updateBox.width<=searchBox.x);
 await shot('header-update-available');
 await verifyHeaderWidths('chat');
 progress('Automatic metadata check revealed the sidebar button without downloading or staging B.');
 await page.getByRole('button',{name:'More',exact:true}).click();await page.getByRole('menuitem',{name:'Settings',exact:true}).click();await page.getByRole('button',{name:'Downloads',exact:true}).click();
 assert.equal(await page.getByRole('heading',{name:'AI Shuffle Desktop',exact:true}).count(),0);
 await verifyHeaderWidths('settings');
 await downloadButton.click();
 const downloadingButton=page.getByRole('button',{name:'Downloading update',exact:true});
 await downloadingButton.waitFor({state:'visible'});assert.equal(await downloadingButton.isDisabled(),true);
 await shot('header-update-downloading');
 progress('Clicked the sidebar Download update button through real IPC.');
 const updateError=page.locator('.sb-head').getByRole('alert');
 await Promise.race([
  page.getByRole('button',{name:'Update and restart',exact:true}).waitFor({state:'visible',timeout:900000}),
  updateError.waitFor({state:'visible',timeout:900000}).then(async()=>{throw Error(await updateError.innerText());})
 ]);
 assert.equal(await page.evaluate(async()=>(await window.shuffleDesktop.updates.getState()).phase),'ready');
 await shot('real-update-ready');
 assert.equal(JSON.parse(await fs.readFile(path.join(root,'state.json'),'utf8')).active,revisions[0]);
 const disconnected=new Promise(resolve=>browser.once('disconnected',resolve));
 restarted=true;await page.getByRole('button',{name:'Update and restart',exact:true}).click();
 await Promise.race([disconnected,new Promise((_,reject)=>setTimeout(()=>reject(Error('Old Electron did not exit')),60000))]);
 progress('Old Electron exited; waiting for its actual app.relaunch wrapper.');
 browser=await connect(120000);page=await realPage();
 await page.waitForFunction(()=>window.shuffleDesktop?.version?.startsWith('main@'),{},{timeout:15000});
 const next=await page.evaluate(async()=>({version:window.shuffleDesktop.version,worker:await window.shuffleDesktop.worker.getState(),auth:await(await fetch('/api/v1/auth/status')).json()}));
 assert.equal(next.version,'main@'+revisions[1].slice(0,12));assert.equal(next.worker.port,18080);assert.notEqual(next.worker.pid,bridge.worker.pid);assert.equal(next.auth.setup_required,false);assert.equal(next.auth.onboarding_complete,true);
 const state=JSON.parse(await fs.readFile(path.join(root,'state.json'),'utf8'));assert.equal(state.active,revisions[1]);assert.equal(state.previous,revisions[0]);
 report.secondWorker=next.worker;report.preservedOnboarding=true;
 await page.getByRole('button',{name:'More',exact:true}).waitFor({timeout:15000});
 await page.evaluate(()=>window.shuffleDesktop.updates.check());
 assert.equal(await page.getByRole('button',{name:'Download update',exact:true}).count(),0);
 assert.equal(await page.getByRole('button',{name:'Update and restart',exact:true}).count(),0);
 await shot('restarted-real-desktop');
 report.productionAfter=await production();report.productionListenersAfter=listenerPids();
 assert.deepEqual(report.productionListenersAfter,report.productionListenersBefore);
 report.success=true;progress('Real launcher, IPC update and Electron relaunch passed; original application PIDs unchanged.');
}catch(error){report.success=false;report.error=String(error);if(page&&!page.isClosed()){report.lastUpdateState=await page.evaluate(()=>window.shuffleDesktop.updates.getState()).catch(()=>null);await shot('failure').catch(()=>{});}console.error(error);process.exitCode=1;}
finally{
 if(page&&!page.isClosed())await page.evaluate(()=>window.shuffleDesktop?.window.close()).catch(()=>{});
 if(browser)await browser.close().catch(()=>{});
 await log.close();await fs.writeFile(path.join(root,'report.json'),JSON.stringify(report,null,2));console.log('Report: '+path.join(root,'report.json'));
}

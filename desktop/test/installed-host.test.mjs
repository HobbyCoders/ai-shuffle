import assert from 'node:assert/strict';
import { test } from 'node:test';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
const require=createRequire(import.meta.url);
const {readInstalledHostSync,saveInstalledHost,sourceLaunch,installedLaunchArgs,installedExecutable,requireInstalledElectron,InstallerRequired,launcherVersion}=require('../dist/main/installed-host.js');
test('an explicit Chromium debugging port survives even when Electron omits it from argv',()=>{
 assert.deepEqual(installedLaunchArgs(['aishuffle://callback'],'19229'),['--remote-debugging-port=19229'],'NC-16: no URL scheme is registered, so no link argument is forwarded');
 assert.deepEqual(installedLaunchArgs([]),[]);
 assert.deepEqual(installedLaunchArgs(['--remote-debugging-port=0'],'0'),['--remote-debugging-port=0']);
 assert.deepEqual(installedLaunchArgs(['--remote-debugging-port=19229'],'19229'),['--remote-debugging-port=19229']);
});
test('installed source versions preserve the original profile and EXE across restarts',async t=>{
 const userData=await fs.mkdtemp(path.join(os.tmpdir(),'shuffle-installed-test-'));t.after(()=>fs.rm(userData,{recursive:true,force:true}));
 const root=path.join(userData,'updates');await fs.mkdir(root);
 const host={executable:path.join(userData,'Programs','AI Shuffle.exe'),userData,name:'ai-shuffle-desktop'};
 assert.equal(readInstalledHostSync(root),null);await saveInstalledHost(root,host);assert.deepEqual(readInstalledHostSync(root),host);
 await assert.rejects(saveInstalledHost(root,{...host,userData:path.join(userData,'other')}),/Invalid installed/);
 const app={electron:path.join(root,'electron.exe'),desktopDir:path.join(root,'desktop'),revision:'a'.repeat(40),node:path.join(root,'node.exe'),python:path.join(root,'python.exe')};
 const launch=sourceLaunch(app,root,['aishuffle://callback']);
 assert.equal(launch.executable,app.electron);assert.deepEqual(launch.args,[app.desktopDir,'aishuffle://callback']);
 assert.equal(launch.options.windowsHide,false,'Electron must be allowed to show its splash window');assert.equal(launch.options.detached,true);
 assert.equal(launch.options.env.SHUFFLE_SOURCE_ROOT,root);assert.equal(launch.options.env.SHUFFLE_SOURCE_REVISION,app.revision);
 assert(!launch.args.some(arg=>/bootstrap|\.cmd|powershell/.test(arg)));
});

test('Linux compatibility requires the installed Electron exact pinned version before source activation',async t=>{
 const desktopDir=await fs.mkdtemp(path.join(os.tmpdir(),'shuffle-electron-preflight-'));t.after(()=>fs.rm(desktopDir,{recursive:true,force:true}));
 const application={desktopDir};const file=path.join(desktopDir,'package-lock.json');
 await fs.mkdir(path.join(desktopDir,'dist/main'),{recursive:true});const entry=path.join(desktopDir,'dist/main/managed-entry.js');
 await fs.writeFile(entry,'exports.startManagedSource = startManagedSource; if (require.main === module) start();');
 await fs.writeFile(file,JSON.stringify({packages:{'node_modules/electron':{version:'44.2.0'}}}));
 await requireInstalledElectron(application,'44.2.0');
 await fs.writeFile(entry,'start();');await assert.rejects(requireInstalledElectron(application,'44.2.0'),InstallerRequired);
 await assert.rejects(requireInstalledElectron(application,'44.1.0'),InstallerRequired);
 await fs.writeFile(file,'{}');await assert.rejects(requireInstalledElectron(application,'44.2.0'),InstallerRequired);
});
test('Linux keeps the original AppImage rather than its temporary mount, and deb uses execPath', {skip:process.platform!=='linux'},async t=>{
 const saved=process.env.APPIMAGE;t.after(()=>{if(saved===undefined)delete process.env.APPIMAGE;else process.env.APPIMAGE=saved;});
 process.env.APPIMAGE='/home/me/Applications/AI Shuffle.AppImage';assert.equal(installedExecutable(),process.env.APPIMAGE);
 const userData=await fs.mkdtemp(path.join(os.tmpdir(),'shuffle-linux-host-'));t.after(()=>fs.rm(userData,{recursive:true,force:true}));
 const root=path.join(userData,'updates');await fs.mkdir(root);
 const host={executable:installedExecutable(),userData,name:'AI Shuffle'};
 await saveInstalledHost(root,host);assert.deepEqual(readInstalledHostSync(root),host);
 delete process.env.APPIMAGE;assert.equal(installedExecutable(),process.execPath);
 process.env.APPIMAGE='relative.AppImage';assert.throws(()=>installedExecutable(),/Invalid installed/);
});
test('Linux source Electron launch retains desktop session variables but not AppImage mount overrides', {skip:process.platform!=='linux'},t=>{
 const saved={...process.env};t.after(()=>{for(const k of Object.keys(process.env))if(!(k in saved))delete process.env[k];Object.assign(process.env,saved);});
 Object.assign(process.env,{DISPLAY:':1',WAYLAND_DISPLAY:'wayland-0',XDG_RUNTIME_DIR:'/run/user/1000',DBUS_SESSION_BUS_ADDRESS:'unix:path=/run/user/1000/bus',XAUTHORITY:'/home/me/.Xauthority',APPIMAGE:'/home/me/app.AppImage',APPDIR:'/tmp/.mount_app',LD_LIBRARY_PATH:'/tmp/.mount_app/lib'});
 const application={electron:'/r/electron',desktopDir:'/r/desktop',revision:'b'.repeat(40),node:'/r/node/bin/node',python:'/r/python/bin/python3'};
 const launch=sourceLaunch(application,'/home/me/.config/AI Shuffle/updates',[]);
 assert.equal(launch.executable,'/r/electron');assert.deepEqual(launch.args,['/r/desktop']);
 for(const key of ['DISPLAY','WAYLAND_DISPLAY','XDG_RUNTIME_DIR','DBUS_SESSION_BUS_ADDRESS','XAUTHORITY'])assert.equal(launch.options.env[key],process.env[key]);
 for(const key of ['APPIMAGE','APPDIR','LD_LIBRARY_PATH'])assert.equal(launch.options.env[key],undefined);
});
test('D5: the source-managed relaunch keeps the user\'s home and Claude Code config dir',t=>{
 const saved={...process.env};t.after(()=>{for(const k of Object.keys(process.env))if(!(k in saved))delete process.env[k];Object.assign(process.env,saved);});
 Object.assign(process.env,{HOME:'/home/me',USERPROFILE:'C:\\Users\\me',CLAUDE_CONFIG_DIR:'D:\\claude-config',ANTHROPIC_API_KEY:'sk-not-forwarded'});
 const app={electron:'/r/electron.exe',desktopDir:'/r/desktop',revision:'b'.repeat(40),node:'/r/node.exe',python:'/r/python.exe'};
 const env=sourceLaunch(app,'/r',[]).options.env;
 assert.equal(env.CLAUDE_CONFIG_DIR,'D:\\claude-config');
 assert.equal(env.HOME,'/home/me');assert.equal(env.USERPROFILE,'C:\\Users\\me');
 assert.equal(env.ANTHROPIC_API_KEY,undefined);
});

test('#1801: an accepted agent-access disclosure survives the installed launcher\'s source launch',t=>{
 const saved=process.env.SHUFFLE_DESKTOP_ACCEPT_AGENT_ACCESS;t.after(()=>{if(saved===undefined)delete process.env.SHUFFLE_DESKTOP_ACCEPT_AGENT_ACCESS;else process.env.SHUFFLE_DESKTOP_ACCEPT_AGENT_ACCESS=saved;});
 const app={electron:'/r/electron.exe',desktopDir:'/r/desktop',revision:'c'.repeat(40),node:'/r/node.exe',python:'/r/python.exe'};
 // Without it the updated app shows the first-run dialog and never starts its worker.
 process.env.SHUFFLE_DESKTOP_ACCEPT_AGENT_ACCESS='1';
 assert.equal(sourceLaunch(app,'/r',[]).options.env.SHUFFLE_DESKTOP_ACCEPT_AGENT_ACCESS,'1');
 // Only the exact acceptance value is forwarded; anything else leaves the dialog in place.
 for(const value of ['0','yes',undefined]){
  if(value===undefined)delete process.env.SHUFFLE_DESKTOP_ACCEPT_AGENT_ACCESS;else process.env.SHUFFLE_DESKTOP_ACCEPT_AGENT_ACCESS=value;
  assert.equal('SHUFFLE_DESKTOP_ACCEPT_AGENT_ACCESS' in sourceLaunch(app,'/r',[]).options.env,false,String(value));
 }
});

test('#1821 D5: the launcher passes its own version to the updated app through the allowlisted environment',t=>{
 const saved=process.env.SHUFFLE_LAUNCHER_VERSION;t.after(()=>{if(saved===undefined)delete process.env.SHUFFLE_LAUNCHER_VERSION;else process.env.SHUFFLE_LAUNCHER_VERSION=saved;});
 const app={electron:'/r/electron.exe',desktopDir:'/r/desktop',revision:'d'.repeat(40),node:'/r/node.exe',python:'/r/python.exe'};
 delete process.env.SHUFFLE_LAUNCHER_VERSION;
 assert.equal(sourceLaunch(app,'/r',[],3).options.env.SHUFFLE_LAUNCHER_VERSION,'3');
 // No stamp (an installer built before it) reads as launcher 0.
 assert.equal(sourceLaunch(app,'/r',[]).options.env.SHUFFLE_LAUNCHER_VERSION,'0');
 // An inherited value (the updated app relaunching the EXE) never overrides the launcher's own.
 process.env.SHUFFLE_LAUNCHER_VERSION='9';
 assert.equal(sourceLaunch(app,'/r',[],1).options.env.SHUFFLE_LAUNCHER_VERSION,'1');
});

test('#1821 D5: the updated app reads a missing or malformed launcher version as 0',()=>{
 for(const [value,expected] of [['0',0],['1',1],['42',42],[undefined,0],['',0],['-1',0],['1.5',0],['01',0],[' 2',0],['abc',0],['9999999999',0],[3,0]])
  assert.equal(launcherVersion(value),expected,String(value));
});

test('a corrupt or foreign installed-host.json fails closed instead of choosing a profile',async t=>{
 const userData=await fs.mkdtemp(path.join(os.tmpdir(),'shuffle-installed-host-'));t.after(()=>fs.rm(userData,{recursive:true,force:true}));
 const root=path.join(userData,'updates');await fs.mkdir(root);const file=path.join(root,'installed-host.json');
 const host={executable:path.join(userData,'Programs','AI Shuffle.exe'),userData,name:'AI Shuffle'};
 // Truncated by a crash or an editor: an error, never "no host" (which would start a fresh default profile).
 await fs.writeFile(file,'{"executable":');assert.throws(()=>readInstalledHostSync(root),SyntaxError);
 for(const bad of [
  {...host,userData:path.join(userData,'..','someone-else')},  // a profile other than the one owning these updates
  {...host,executable:'AI Shuffle.exe'},                       // relative executable
  ...(process.platform==='win32'?[{...host,executable:path.join(userData,'run.sh')}]:[]), // Windows requires an EXE
  {...host,name:''},{executable:host.executable,userData},null,[]
 ]){
  await fs.writeFile(file,JSON.stringify(bad));
  assert.throws(()=>readInstalledHostSync(root),/Invalid installed desktop host/,JSON.stringify(bad));
 }
 await fs.writeFile(file,JSON.stringify(host));assert.deepEqual(readInstalledHostSync(root),host);
 // A save leaves no temporary file behind and replaces the record whole.
 await saveInstalledHost(root,{...host,name:'AI Shuffle 2'});
 assert.deepEqual((await fs.readdir(root)).sort(),['installed-host.json']);
 assert.equal(readInstalledHostSync(root).name,'AI Shuffle 2');
});

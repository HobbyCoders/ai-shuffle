import assert from 'node:assert/strict';
import { test } from 'node:test';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
const require=createRequire(import.meta.url);
const {readInstalledHostSync,saveInstalledHost,sourceLaunch,installedLaunchArgs}=require('../dist/main/installed-host.js');
test('an explicit Chromium debugging port survives even when Electron omits it from argv',()=>{
 assert.deepEqual(installedLaunchArgs(['aishuffle://callback'],'19229'),['aishuffle://callback','--remote-debugging-port=19229']);
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

test('D5: the source-managed relaunch keeps the user\'s home and Claude Code config dir',t=>{
 const saved={...process.env};t.after(()=>{for(const k of Object.keys(process.env))if(!(k in saved))delete process.env[k];Object.assign(process.env,saved);});
 Object.assign(process.env,{HOME:'/home/me',USERPROFILE:'C:\\Users\\me',CLAUDE_CONFIG_DIR:'D:\\claude-config',ANTHROPIC_API_KEY:'sk-not-forwarded'});
 const app={electron:'/r/electron.exe',desktopDir:'/r/desktop',revision:'b'.repeat(40),node:'/r/node.exe',python:'/r/python.exe'};
 const env=sourceLaunch(app,'/r',[]).options.env;
 assert.equal(env.CLAUDE_CONFIG_DIR,'D:\\claude-config');
 assert.equal(env.HOME,'/home/me');assert.equal(env.USERPROFILE,'C:\\Users\\me');
 assert.equal(env.ANTHROPIC_API_KEY,undefined);
});

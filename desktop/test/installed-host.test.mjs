import assert from 'node:assert/strict';
import { test } from 'node:test';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
const require=createRequire(import.meta.url);
const {readInstalledHost,saveInstalledHost,sourceLaunch,installedLaunchArgs}=require('../dist/main/installed-host.js');
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
 assert.equal(await readInstalledHost(root),null);await saveInstalledHost(root,host);assert.deepEqual(await readInstalledHost(root),host);
 await assert.rejects(saveInstalledHost(root,{...host,userData:path.join(userData,'other')}),/Invalid installed/);
 const app={electron:path.join(root,'electron.exe'),desktopDir:path.join(root,'desktop'),revision:'a'.repeat(40),node:path.join(root,'node.exe'),python:path.join(root,'python.exe')};
 const launch=sourceLaunch(app,root,['aishuffle://callback']);
 assert.equal(launch.executable,app.electron);assert.deepEqual(launch.args,[app.desktopDir,'aishuffle://callback']);
 assert.equal(launch.options.windowsHide,true);assert.equal(launch.options.detached,true);
 assert.equal(launch.options.env.SHUFFLE_SOURCE_ROOT,root);assert.equal(launch.options.env.SHUFFLE_SOURCE_REVISION,app.revision);
 assert(!launch.args.some(arg=>/bootstrap|\.cmd|powershell/.test(arg)));
});

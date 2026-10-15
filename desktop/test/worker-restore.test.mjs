import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { createRequire } from 'node:module';
const { quiesceRecordedWorker, isProcessAlive, terminateProcessTree } = createRequire(import.meta.url)('../dist/main/worker.js');
async function fixture(t, matches) {
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'shuffle-worker-restore-'));
 const token='a'.repeat(64);
 const child=spawn(process.execPath,['-e',`const http=require('node:http'), crypto=require('node:crypto'); const server=http.createServer((req,res)=>{res.setHeader('content-type','application/json');res.end(JSON.stringify({service:'ai-shuffle',attach:crypto.createHash('sha256').update('${matches?token:'wrong'}').digest('hex')}));});server.listen(0,'127.0.0.1',()=>console.log(server.address().port));`],{detached:process.platform!=='win32',stdio:['ignore','pipe','pipe']});
 t.after(async()=>{await terminateProcessTree(child.pid);await fs.rm(root,{recursive:true,force:true});});
 const port=await new Promise((resolve,reject)=>{child.stdout.once('data',chunk=>resolve(Number(chunk.toString().trim())));child.once('error',reject);});
 await fs.writeFile(path.join(root,'worker-process.json'),JSON.stringify({version:1,pid:child.pid,token,port}));
 return {root,child};
}
test('restore reaps its authenticated orphan worker, not just its database',async t=>{
 const {root,child}=await fixture(t,true);
 await quiesceRecordedWorker(root);
 assert.equal(isProcessAlive(child.pid),false);
});
test('restore refuses an unverified live PID without signalling it',async t=>{
 const {root,child}=await fixture(t,false);
 await assert.rejects(quiesceRecordedWorker(root),/Cannot verify/);
 assert.equal(isProcessAlive(child.pid),true);
});

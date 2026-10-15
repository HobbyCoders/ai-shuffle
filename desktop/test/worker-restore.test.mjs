import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { createRequire } from 'node:module';
const { quiesceRecordedWorker, isProcessAlive, terminateProcessTree } = createRequire(import.meta.url)('../dist/main/worker.js');
const { readProcessIdentity } = createRequire(import.meta.url)('../dist/main/process-identity.js');
async function fixture(t, matches) {
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'shuffle-worker-restore-'));
 const token='a'.repeat(64);
 const child=spawn(process.execPath,['-e',`const http=require('node:http'), crypto=require('node:crypto'); const server=http.createServer((req,res)=>{res.setHeader('content-type','application/json');res.end(JSON.stringify({service:'ai-shuffle',attach:crypto.createHash('sha256').update('${matches?token:'wrong'}').digest('hex')}));});server.listen(0,'127.0.0.1',()=>console.log(server.address().port));`],{detached:process.platform!=='win32',stdio:['ignore','pipe','pipe']});
 t.after(async()=>{await terminateProcessTree(child.pid);await fs.rm(root,{recursive:true,force:true});});
 const port=await new Promise((resolve,reject)=>{child.stdout.once('data',chunk=>resolve(Number(chunk.toString().trim())));child.once('error',reject);});
 await fs.writeFile(path.join(root,'worker-process.json'),JSON.stringify({version:1,pid:child.pid,token,port,identity:await readProcessIdentity(child.pid)}));
 return {root,child};
}
test('restore reaps its authenticated orphan worker, not just its database',async t=>{
 const {root,child}=await fixture(t,true);
 await quiesceRecordedWorker(root);
 assert.equal(isProcessAlive(child.pid),false);
});
test('a separate authenticated responder cannot authorize signalling a reused PID',async t=>{
 const {root,child:server}=await fixture(t,true);
 const victim=spawn(process.execPath,['-e','setInterval(()=>{},1000)'],{detached:process.platform!=='win32',stdio:'ignore'});
 t.after(()=>terminateProcessTree(victim.pid));
 const file=path.join(root,'worker-process.json');const record=JSON.parse(await fs.readFile(file,'utf8'));
 // Same executable, different kernel start identity. Simulate PID reuse in an old record.
 record.pid=victim.pid;record.identity.pid=victim.pid;
 record.identity.started='old-start-identity';
 await fs.writeFile(file,JSON.stringify(record));
 await assert.rejects(quiesceRecordedWorker(root),/identity mismatch/i);
 assert.equal(isProcessAlive(victim.pid),true);assert.equal(isProcessAlive(server.pid),true);
});
test('a legacy record without OS identity never kills a live process, despite a matching responder',async t=>{
 const {root,child}=await fixture(t,true);const file=path.join(root,'worker-process.json');
 const record=JSON.parse(await fs.readFile(file));delete record.identity;await fs.writeFile(file,JSON.stringify(record));
 await assert.rejects(quiesceRecordedWorker(root),/no verified OS process identity/);assert.equal(isProcessAlive(child.pid),true);
});
test('restore refuses an unverified live PID without signalling it',async t=>{
 const {root,child}=await fixture(t,false);
 await assert.rejects(quiesceRecordedWorker(root),/Cannot verify/);
 assert.equal(isProcessAlive(child.pid),true);
});

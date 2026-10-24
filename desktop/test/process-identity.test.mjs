import assert from 'node:assert/strict';
import { promises as fs } from 'node:fs';
import { createRequire } from 'node:module';
import { test } from 'node:test';
const {readProcessIdentity,assertProcessIdentity,execWithIdentity}=createRequire(import.meta.url)('../dist/main/process-identity.js');
test('kernel process identity includes stable start and actual executable, not just PID',async()=>{
 const first=await readProcessIdentity(process.pid),second=await readProcessIdentity(process.pid);
 assert.deepEqual(first,second);assert.equal(first.pid,process.pid);
 assert.equal(first.executable.toLowerCase(),(await fs.realpath(process.execPath)).toLowerCase());
 assert.ok(first.startedAt>0);await assertProcessIdentity(first);
});
test('signal boundary refuses a reused start identity or different executable',async()=>{
 const identity=await readProcessIdentity(process.pid);
 for(const wrong of [{...identity,started:'prior-process'},{...identity,executable:identity.executable+'.wrong'}]){
  await assert.rejects(execWithIdentity(wrong,process.execPath,['-e','process.exit(0)']),/identity mismatch|no process was signalled/i);
 }
 const result=await execWithIdentity(identity,process.execPath,['-e',"console.log('verified child')"]);
 assert.equal(result.code,0);assert.equal(result.stdout.trim(),'verified child');
});
test('the Windows probe defines its kernel32 imports in memory instead of compiling with Add-Type (#597)',async()=>{
 const source=await fs.readFile(new URL('../dist/main/process-identity.js',import.meta.url),'utf8');
 assert.doesNotMatch(source,/Add-Type -TypeDefinition/);assert.match(source,/DefinePInvokeMethod/);
});
test('the Windows probe fails closed for a process it cannot open',{skip:process.platform!=='win32'&&'Windows kernel handle probe'},async()=>{
 await assert.rejects(readProcessIdentity(2147483644),/Cannot open process identity handle/);
});

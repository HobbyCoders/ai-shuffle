import assert from 'node:assert/strict';
import {test} from 'node:test';
import {createRequire} from 'node:module';
import {mkdtempSync,writeFileSync,mkdirSync,rmSync,symlinkSync} from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import {generateKeyPairSync} from 'node:crypto';
const require=createRequire(import.meta.url);
const {createProfile,readProfile,isolatedRoot,localFeed}=require('../scripts/source-profile.cjs');
function directory(t){const root=mkdtempSync(path.join(os.tmpdir(),'shuffle-profile-'));t.after(()=>rmSync(root,{recursive:true,force:true}));return root;}
test('isolation persists ports and never overlaps normal data',t=>{
 const root=directory(t);const p=createProfile(root,{port:18080,protectedPort:18081,debugPort:19228});assert.deepEqual(readProfile(root),p);
 assert.throws(()=>createProfile(root,{port:18082,protectedPort:18083}),/before the first/);
 const normal=path.join(root,'AI Shuffle Source');assert.throws(()=>isolatedRoot(normal,{LOCALAPPDATA:root}),/overlap/);assert.throws(()=>isolatedRoot(root,{LOCALAPPDATA:root}),/overlap/);
 assert.equal(readProfile(path.join(root,'ordinary')),null);
 mkdirSync(normal);const alias=path.join(root,'alias');symlinkSync(normal,alias,'junction');assert.throws(()=>isolatedRoot(alias,{LOCALAPPDATA:root}),/overlap/);
});
test('isolated profiles reject production, duplicate and invalid ports',t=>{
 for(const [port,protectedPort]of [[8000,18081],[18080,8001],[18080,18080],[80,18081],[18080,65536]]){
  const root=directory(t);assert.throws(()=>createProfile(root,{port,protectedPort}),/ports/);
 }
});
test('test feeds are explicit, signed-keyed, local and origin restricted',async t=>{
 const root=directory(t),feed=path.join(root,'channel');mkdirSync(feed);
 const {publicKey}=generateKeyPairSync('ed25519');const key=path.join(feed,'key.pem');writeFileSync(key,publicKey.export({type:'spki',format:'pem'}));
 const profile=createProfile(root,{port:18080,protectedPort:18081,feedDirectory:feed,publicKeyFile:key});writeFileSync(path.join(feed,'latest.json'),'test manifest');
 assert.equal(await(await localFeed(profile)('https://hobbycoders.com/v1/desktop/manifest')).text(),'test manifest');
 await assert.rejects(localFeed(profile)('https://evil.example/v1/desktop/manifest'));
 await assert.rejects(localFeed(profile)('https://hobbycoders.com/v1/desktop/manifest?file=../key.pem'));
 assert.equal(localFeed(null),undefined);
 assert.throws(()=>createProfile(directory(t),{port:18080,protectedPort:18081,publicKeyFile:key}),/requires both/);
 assert.throws(()=>createProfile(directory(t),{port:18080,protectedPort:18081,debugPort:NaN}),/ports/);
});

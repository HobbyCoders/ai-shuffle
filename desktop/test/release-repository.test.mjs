import assert from 'node:assert/strict';
import { test } from 'node:test';
import { releaseRepository } from '../scripts/release-repository.mjs';
test('publication repository identity is pinned, never redirected or guessed',async t=>{
 const original=globalThis.fetch;t.after(()=>globalThis.fetch=original);
 let calls=[];
 globalThis.fetch=async(url,options)=>{calls.push([url,options]);return Response.json({id:42,full_name:'HobbyCoders/ai-shuffle'});};
 const env={RELEASE_GITHUB_REPO_ID:'42'};
 assert.deepEqual(await releaseRepository('fixture',env),{repository:'HobbyCoders/ai-shuffle',id:42});
 assert.equal(calls[0][1].redirect,'manual');
 await assert.rejects(releaseRepository('fixture',{}),/must pin/);
 await assert.rejects(releaseRepository('fixture',{RELEASE_GITHUB_REPO_ID:'7'}),/must pin/);
 await assert.rejects(releaseRepository('fixture',{...env,RELEASE_GITHUB_REPO:'attacker/repo'}),/Unapproved/);
 await assert.rejects(releaseRepository('',env),/credentials/);
 globalThis.fetch=async()=>new Response(null,{status:301,headers:{Location:'https://example.com'}});
 await assert.rejects(releaseRepository('fixture',env),/metadata unavailable/);
});

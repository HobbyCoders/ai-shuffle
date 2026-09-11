// Isolate the real publisher from network, GitHub CLI, and Cloudflare in tests.
import fs from 'node:fs';
import cp from 'node:child_process';
import { syncBuiltinESMExports } from 'node:module';
import { createHash, generateKeyPairSync } from 'node:crypto';
import path from 'node:path';
const root=process.env.RUNNER_TEMP, scenario=process.env.PUBLISH_TEST_CASE;
const bytes=Buffer.from('immutable source ZIP fixture'), revision='a'.repeat(40);
const pair=generateKeyPairSync('ed25519');
const publicKey=pair.publicKey.export({type:'spki',format:'pem'});
process.env.DESKTOP_APPLICATION_SIGNING_KEY=pair.privateKey.export({type:'pkcs8',format:'pem'});
fs.writeFileSync(path.join(root,'public.pem'),publicKey);
const readFile=fs.promises.readFile;
fs.promises.readFile=async(file,...args)=>String(file).endsWith('application-update-key.pem')?publicKey:readFile(file,...args);
let saved,mainCalls=0;
const events=[];
const asset={id:123,name:revision+'.zip',size:bytes.length,state:'uploaded',digest:'sha256:'+createHash('sha256').update(bytes).digest('hex')};
cp.execFileSync=(exe,args)=>{
 if(exe==='gh') { events.push('reuse');fs.writeFileSync(path.join(root,asset.name),bytes);return ''; }
 if(!args.includes('d1')||!args.includes('--remote'))throw Error('Unexpected external command');
 const sql=fs.readFileSync(args[args.indexOf('--file')+1],'utf8');
 if(sql.startsWith('INSERT')) {
  events.push('publish');const match=sql.match(/VALUES\('([a-f0-9]{40})',(\d+),(\d+),'(.*?)',(\d+)\)/);
  if(!match)throw Error('Invalid publisher SQL');
  saved={manifest:match[4],asset_id:Number(match[2])};fs.writeFileSync(path.join(root,'record.json'),JSON.stringify(saved));return '';
 }
 if(sql.startsWith('SELECT'))return JSON.stringify([{results:[saved]}]);
 throw Error('Unexpected SQL');
};
syncBuiltinESMExports();
globalThis.fetch=async(url,init={})=>{
 const target=String(url);events.push(target);
 if(target.endsWith('/commits/main'))return Response.json({sha:scenario==='moved'&&++mainCalls>1?'b'.repeat(40):revision});
 if(target.endsWith('/releases/tags/desktop-sources'))return new Response(null,{status:404});
 if(target.endsWith('/releases')&&init.method==='POST'){
  const body=JSON.parse(init.body);if(!body.prerelease||body.make_latest!=='false')throw Error('Must not replace stable release');return Response.json({id:9});
 }
 if(target.startsWith('https://api.github.com/')&&target.includes('/releases/9/assets?'))return Response.json(scenario==='reuse'?[asset]:[]);
 if(target.endsWith('/zipball/'+revision))return new Response(bytes);
 if(target.startsWith('https://uploads.github.com/')){
  if(init.headers['Content-Length']!==String(bytes.length))throw Error('Wrong upload size');
  let total=0;for await(const chunk of init.body)total+=chunk.length;if(total!==bytes.length)throw Error('Incomplete upload');
  return Response.json({...asset,digest:scenario==='corrupt'?'sha256:'+'0'.repeat(64):asset.digest});
 }
 throw Error('Unexpected network request');
};
process.on('exit',()=>fs.writeFileSync(path.join(root,'events.json'),JSON.stringify(events)));

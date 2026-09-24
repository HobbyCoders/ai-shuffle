// Isolate the real publisher from network, git, Python, and Cloudflare in tests.
import fs from 'node:fs';
import cp from 'node:child_process';
import { syncBuiltinESMExports } from 'node:module';
import { createHash, generateKeyPairSync } from 'node:crypto';
import path from 'node:path';
const root=process.env.RUNNER_TEMP, scenario=process.env.PUBLISH_TEST_CASE;
const bytes=Buffer.from('immutable runtime source ZIP fixture'), revision='a'.repeat(40);
const pair=generateKeyPairSync('ed25519');
const publicKey=pair.publicKey.export({type:'spki',format:'pem'});
process.env.DESKTOP_APPLICATION_SIGNING_KEY=pair.privateKey.export({type:'pkcs8',format:'pem'});
fs.writeFileSync(path.join(root,'public.pem'),publicKey);
const readFile=fs.promises.readFile;
fs.promises.readFile=async(file,...args)=>String(file).endsWith('application-update-key.pem')?publicKey:readFile(file,...args);
let saved,mainCalls=0;
const events=[];
const sha256=createHash('sha256').update(bytes).digest('hex');
const asset={id:123,name:revision+'-runtime.zip',size:bytes.length,state:'uploaded',digest:'sha256:'+sha256};
cp.execFileSync=(exe,args)=>{
 if(exe==='git'){
  if(args[0]==='rev-parse'&&args[1]==='HEAD')return revision+'\n';
  if(args[0]==='rev-parse'&&args[1]==='refs/tags/v1.2.3^{commit}')return revision+'\n';
  if(args[0]==='rev-parse'&&args[1]==='--is-shallow-repository')return 'false\n';
  if(args[0]==='rev-list')return '42\n';
  if(args[0]==='fetch'){events.push('fetch-main');return '';}
  if(args[0]==='merge-base'){events.push('on-main');if(scenario==='offmain')throw Error('not an ancestor');return '';}
  if(args[0]==='cat-file')return 'tag\n';
  if(args[0]==='tag'&&args[1]==='--list')return 'v1.2.3\nv6-phase2\nv1.2.4-rc.1\n'+(scenario==='older'?'v1.2.4\n':'');
  if(args[0]==='tag'&&args[2]==='--format=%(contents:body)')return 'Faster startup.\n-----BEGIN PGP SIGNATURE-----\nabc\n-----END PGP SIGNATURE-----\n';
  throw Error('Unexpected git command '+args.join(' '));
 }
 if(exe==='python'&&args[0]==='docker/update_contract.py')return 'b'.repeat(64)+'\n';
 if(exe==='python'&&args[0]==='android/ci/resolve-version-name.py')return '0.8.18-dev.4+gaaaaaaa\n';
 if(exe==='python'&&args[0]==='scripts/source_archive.py'){
  if(args[1]!==revision)throw Error('Archive built for the wrong revision');
  events.push('archive');fs.writeFileSync(args[2],bytes);
  return JSON.stringify({sha256,size:bytes.length,tree:'c'.repeat(64),files:3});
 }
 if(!args.includes('d1')||!args.includes('--remote'))throw Error('Unexpected external command');
 if(args.includes('--file'))throw Error('Bulk file imports emit progress outside JSON and must not be used for small publisher queries');
 if(!args.includes('--command'))throw Error('Expected D1 query command');
 const sql=args[args.indexOf('--command')+1];
 if(sql.startsWith('INSERT')) {
  events.push('publish');const match=sql.match(/INTO source_releases\(revision,channel,version,asset_id,size,manifest,sequence\) VALUES\('([a-f0-9]{40})','(stable|edge)','([^']+)',(\d+),(\d+),'(.*?)',(\d+)\)/);
  if(!match)throw Error('Invalid publisher SQL');
  saved={channel:match[2],version:match[3],manifest:match[6],asset_id:Number(match[4])};fs.writeFileSync(path.join(root,'record.json'),JSON.stringify(saved));return '';
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
 if(target.startsWith('https://api.github.com/')&&target.includes('/releases/9/assets?')){
  if(scenario==='reuse')events.push('reuse');
  return Response.json(scenario==='reuse'?[asset]:[]);
 }
 if(target.startsWith('https://uploads.github.com/')){
  if(!target.includes(`name=${revision}-runtime.zip`))throw Error('Runtime archives use their own asset name');
  if(init.headers['Content-Length']!==String(bytes.length))throw Error('Wrong upload size');
  let total=0;for await(const chunk of init.body)total+=chunk.length;if(total!==bytes.length)throw Error('Incomplete upload');
  return Response.json({...asset,digest:scenario==='corrupt'?'sha256:'+'0'.repeat(64):asset.digest});
 }
 throw Error('Unexpected network request');
};
process.on('exit',()=>fs.writeFileSync(path.join(root,'events.json'),JSON.stringify(events)));

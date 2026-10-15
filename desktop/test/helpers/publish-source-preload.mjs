// Isolate the real publisher from network, git, Python, and Cloudflare in tests.
import fs from 'node:fs';
import cp from 'node:child_process';
import { syncBuiltinESMExports } from 'node:module';
import { createHash, createPrivateKey, createPublicKey, generateKeyPairSync } from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const realExec = cp.execFileSync;
const controlRoot = path.resolve(process.env.PUBLISH_TEST_CONTROL || fileURLToPath(new URL('../../../', import.meta.url)));
const root=process.env.RUNNER_TEMP, scenario=process.env.PUBLISH_TEST_CASE;
let bytes=Buffer.from('immutable runtime source ZIP fixture');
const revision=process.env.PUBLISH_TEST_REVISION || 'a'.repeat(40);
const keyFile=path.join(root,'private.pem');
const pair=fs.existsSync(keyFile)?{privateKey:createPrivateKey(fs.readFileSync(keyFile))}:generateKeyPairSync('ed25519');
pair.publicKey=createPublicKey(pair.privateKey);
fs.writeFileSync(keyFile,pair.privateKey.export({type:'pkcs8',format:'pem'}));
const publicKey=pair.publicKey.export({type:'spki',format:'pem'});
process.env.DESKTOP_APPLICATION_SIGNING_KEY=pair.privateKey.export({type:'pkcs8',format:'pem'});
fs.writeFileSync(path.join(root,'public.pem'),publicKey);
const readFile=fs.promises.readFile;
fs.promises.readFile=async(file,...args)=>String(file).endsWith('application-update-key.pem')?publicKey:readFile(file,...args);
let saved=fs.existsSync(path.join(root,'record.json'))?JSON.parse(fs.readFileSync(path.join(root,'record.json'),'utf8')):undefined,mainCalls=0;
const events=[];
let sha256=createHash('sha256').update(bytes).digest('hex');
const asset={id:123,name:revision+'-runtime.zip',size:bytes.length,state:'uploaded',digest:'sha256:'+sha256};
cp.execFileSync=(exe,args,options={})=>{
 if(path.resolve(options.cwd || '.')!==controlRoot)throw Error('Executable cwd is not the trusted control checkout');
 if(options.env?.DESKTOP_APPLICATION_SIGNING_KEY)throw Error('Subprocess received signing key');
 if(exe==='git'){
  if(args[0]!=='-C'||!path.isAbsolute(args[1]))throw Error('Git source root must be explicit data');
  if(scenario==='two-checkout')return realExec(exe,args,options);
  args=args.slice(2);
  if(args[0]==='rev-parse'&&args[1]==='HEAD')return revision+'\n';
  if(args[0]==='rev-parse'&&args[1]==='refs/tags/v1.2.3^{commit}')return revision+'\n';
  if(args[0]==='rev-parse'&&args[1]==='--is-shallow-repository')return 'false\n';
  if(args[0]==='rev-list')return '42\n';
  if(args[0]==='show'&&args.includes('--format=%ct'))return '1\n';
  if(args[0]==='show'&&args[1]===`${revision}:desktop/launcher.json`)return fs.readFileSync(new URL('../../launcher.json',import.meta.url),'utf8');
  if(args[0]==='fetch'){events.push('fetch-main');return '';}
  if(args[0]==='merge-base'){events.push('on-main');if(scenario==='offmain')throw Error('not an ancestor');return '';}
  if(args[0]==='cat-file')return 'tag\n';
  if(args[0]==='tag'&&args[1]==='--list')return 'v1.2.3\nv6-phase2\nv1.2.4-rc.1\n'+(scenario==='older'?'v1.2.4\n':'');
  if(args[0]==='tag'&&args[2]==='--format=%(contents:body)')return 'Faster startup.\n-----BEGIN PGP SIGNATURE-----\nabc\n-----END PGP SIGNATURE-----\n';
  throw Error('Unexpected git command '+args.join(' '));
 }
 if(exe==='python') {
  if(args[0]!=='-I'||!args.includes('--source-root')||!path.isAbsolute(args[args.indexOf('--source-root')+1]))throw Error('Python must be isolated with an explicit source root');
  if(options.env.CLOUDFLARE_API_TOKEN)throw Error('Python received publishing credentials');
  if(args[1]===path.join(controlRoot,'scripts/ci/edge_publication_gate.py')) {
   events.push('ancestor-gate');
   return JSON.stringify({ready:!(scenario==='newer-attested'&&++mainCalls>1),revision,published:'b'.repeat(40)});
  }
  if(args[1]!==path.join(controlRoot,'scripts/ci/publication_source.py'))throw Error('Executed candidate or unexpected Python helper');
  if(args[args.indexOf('--revision')+1]!==revision)throw Error('Wrong source revision');
  if(options.env.GH_TOKEN)throw Error('Source reader received GitHub credentials');
  const command=args[2],output=args[args.indexOf('--output')+1];
  events.push('source:'+command);
  if(scenario==='two-checkout') {
   const result=realExec(exe,args,options);
   if(command==='archive') {
    bytes=fs.readFileSync(output);sha256=createHash('sha256').update(bytes).digest('hex');
    Object.assign(asset,{size:bytes.length,digest:'sha256:'+sha256});events.push('archive');
   }
   return result;
  }
  if(command==='signing-floor')return JSON.stringify({floor:1,old_repository_id:null});
  if(command==='sequence')return '42';
  if(command==='contract')return JSON.stringify({runtime:'b'.repeat(64),requires:['base-1'],recommends:[]});
  if(command==='version')return JSON.stringify('0.8.18-dev.4+gaaaaaaa');
  if(command==='archive'){
   events.push('archive');fs.writeFileSync(output,bytes);
   return JSON.stringify({sha256,size:bytes.length,tree:'c'.repeat(64),files:3});
  }
  throw Error('Unexpected source operation');
 }
 if(args[0]!==path.join(controlRoot,'scripts/source-publisher/node_modules/wrangler/bin/wrangler.js'))throw Error('Executed candidate Wrangler');
 if(scenario==='two-checkout')return realExec(exe,args,options);
 if(!args.includes('d1')||!args.includes('--remote'))throw Error('Unexpected external command');
 if(args.includes('--file'))throw Error('Bulk file imports emit progress outside JSON and must not be used for small publisher queries');
 if(!args.includes('--command'))throw Error('Expected D1 query command');
 const sql=args[args.indexOf('--command')+1];
 if(sql.startsWith('SELECT revision,release_repository_id'))return JSON.stringify([{results:[]}]);
 if(sql.startsWith('SELECT revision,sequence'))return JSON.stringify([{results:[{revision:(scenario==='stale-feed'?'c':'b').repeat(40),sequence:1}]}]);
 if(sql.startsWith('SELECT MAX(sequence)'))return JSON.stringify([{results:[{maximum:scenario==='clock-regression'?Number.MAX_SAFE_INTEGER:1}]}]);
 if(sql.startsWith('INSERT')) {
  fs.writeFileSync(path.join(root,'insert.sql'),sql);
  if(scenario==='pointer-race')return '';
  events.push('publish');const match=sql.match(/INTO source_releases\(revision,channel,version,asset_id,size,manifest,sequence,release_repository,release_repository_id\) SELECT '([a-f0-9]{40})','(stable|edge)','([^']+)',(\d+),(\d+),'(.*?)',(\d+),'([^']+)',(\d+) WHERE/);
  if(!match)throw Error('Invalid publisher SQL');
  saved={channel:match[2],version:match[3],manifest:match[6],asset_id:Number(match[4]),release_repository:match[8],release_repository_id:Number(match[9])};fs.writeFileSync(path.join(root,'record.json'),JSON.stringify(saved));return '';
 }
 if(sql.startsWith('SELECT')){events.push('read-publication');return JSON.stringify([{results:saved?[saved]:[]}]);}
 throw Error('Unexpected SQL');
};
syncBuiltinESMExports();
globalThis.fetch=async(url,init={})=>{
 const target=String(url);events.push(target);
 if(target==='https://api.github.com/repos/HobbyCoders/ai-shuffle')return Response.json({id:42,full_name:'HobbyCoders/ai-shuffle'});
 if(target.endsWith('/commits/main'))return Response.json({sha:scenario==='moved'&&++mainCalls>1?'b'.repeat(40):revision});
 if(target.endsWith('/releases/tags/desktop-sources'))return new Response(null,{status:404});
 if(target.endsWith('/releases')&&init.method==='POST'){
  const body=JSON.parse(init.body);if(!body.prerelease||body.make_latest!=='false')throw Error('Must not replace stable release');return Response.json({id:9});
 }
 if(target.startsWith('https://api.github.com/')&&target.includes('/releases/9/assets?')){
  if(scenario==='reuse')events.push('reuse');
  return Response.json(scenario==='reuse'||saved?[asset]:[]);
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

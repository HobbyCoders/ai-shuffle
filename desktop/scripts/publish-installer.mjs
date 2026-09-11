// Register only the exact verified GitHub installer asset for anonymous delivery.
import { createHash } from 'node:crypto';
import { createReadStream } from 'node:fs';
import { readFile, writeFile, unlink } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
const [file, tag] = process.argv.slice(2), repo='HobbyCoders/ai-shuffle';
if(!file || !/^v\d+\.\d+\.\d+$/.test(tag??''))throw Error('Installer file and stable vX.Y.Z tag required');
const token=process.env.GH_TOKEN, database=process.env.ACCOUNT_HUB_DATABASE_ID;
if(!token || !/^[a-f0-9-]{36}$/.test(database??''))throw Error('Publisher configuration missing');
const build=JSON.parse(await readFile(path.join(path.dirname(file),'desktop-build.json'),'utf8'));
if(!/^[a-f0-9]{40}$/.test(build.revision))throw Error('Installer revision missing');
let size=0;const hash=createHash('sha256');for await(const chunk of createReadStream(file)){size+=chunk.length;hash.update(chunk);}const sha256=hash.digest('hex');
const response=await fetch(`https://api.github.com/repos/${repo}/releases/tags/${tag}`,{headers:{Authorization:`Bearer ${token}`,Accept:'application/vnd.github+json','User-Agent':'AI-Shuffle-Installer-Publisher'}});
if(!response.ok)throw Error(`Release lookup failed (${response.status})`);
const release=await response.json(),asset=release.assets.find(a=>a.name===path.basename(file));
if(!asset || asset.state!=='uploaded' || asset.size!==size || asset.digest!==`sha256:${sha256}`)throw Error('Uploaded installer did not match verified bytes');
const config=path.join(process.env.RUNNER_TEMP,'desktop-installer-d1.json');
try{
 await writeFile(config,JSON.stringify({d1_databases:[{binding:'DB',database_name:'shuffle-account-hub',database_id:database}]}));
 const command=sql=>JSON.parse(execFileSync(process.execPath,['android/hub/node_modules/wrangler/bin/wrangler.js','d1','execute','DB','--remote','--config',config,'--command',sql,'--json'],{encoding:'utf8'}));
 const old=command('SELECT version FROM desktop_installers WHERE id=1;').flatMap(r=>r.results??[])[0];
 const version=tag.slice(1),rank=v=>v.split('.').map(Number);
 if(old){const a=rank(version),b=rank(old.version);const first=a.findIndex((n,i)=>n!==b[i]);if(first>=0&&a[first]<b[first])throw Error('Refusing to replace a newer stable installer');}
 command(`INSERT INTO desktop_installers(id,version,revision,asset_id,size,sha256) VALUES(1,'${version}','${build.revision}',${asset.id},${size},'${sha256}') ON CONFLICT(id) DO UPDATE SET version=excluded.version,revision=excluded.revision,asset_id=excluded.asset_id,size=excluded.size,sha256=excluded.sha256;`);
 const saved=command('SELECT asset_id,sha256 FROM desktop_installers WHERE id=1;').flatMap(r=>r.results??[])[0];
 if(saved?.asset_id!==asset.id||saved?.sha256!==sha256)throw Error('Installer pointer verification failed');
 console.log(`Published Windows ${version}: ${size} verified bytes, revision ${build.revision}`);
}finally{await unlink(config).catch(()=>{});}

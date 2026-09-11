import { execFileSync } from 'node:child_process';
import { writeFile } from 'node:fs/promises';
const revision=execFileSync('git',['rev-parse','HEAD'],{encoding:'utf8'}).trim();
if(!/^[a-f0-9]{40}$/.test(revision))throw Error('Invalid installer source revision');
const sequence=Number(execFileSync('git',['show','-s','--format=%ct','HEAD'],{encoding:'utf8'}).trim())*1000;
await writeFile(new URL('../worker/desktop-build.json',import.meta.url),JSON.stringify({revision,sequence}));
console.log('Installer baseline: '+revision);

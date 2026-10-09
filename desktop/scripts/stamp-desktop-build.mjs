import { execFileSync } from 'node:child_process';
import { writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// Also called by build-worker: deterministic and safe if Windows stamps again.
export async function stampDesktopBuild(directory, cwd) {
 const revision=execFileSync('git',['rev-parse','HEAD'],{encoding:'utf8',cwd}).trim();
 if(!/^[a-f0-9]{40}$/.test(revision))throw Error('Invalid installer source revision');
 const sequence=Number(execFileSync('git',['show','-s','--format=%ct','HEAD'],{encoding:'utf8',cwd}).trim())*1000;
 if(!Number.isSafeInteger(sequence)||sequence<=0)throw Error('Invalid installer source sequence');
 await writeFile(path.join(directory,'desktop-build.json'),JSON.stringify({revision,sequence}));
 console.log('Installer baseline: '+revision);
}
if(process.argv[1] && path.resolve(process.argv[1])===fileURLToPath(import.meta.url)) {
 await stampDesktopBuild(fileURLToPath(new URL('../worker',import.meta.url)),fileURLToPath(new URL('../..',import.meta.url)));
}

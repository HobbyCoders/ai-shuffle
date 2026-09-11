import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export async function runtimeContract(root) {
  const contract = JSON.parse(await readFile(path.join(root, 'desktop/runtime-contract.json'), 'utf8'));
  return createHash('sha256').update(JSON.stringify(contract)).digest('hex');
}
export async function pythonDependencies(root) {
  const hash = createHash('sha256');
  for (const name of ['requirements.txt', 'requirements-native.txt', 'fluid/pyproject.toml']) {
    hash.update(name + '\0' + (await readFile(path.join(root, name), 'utf8')).replace(/\r\n/g, '\n') + '\0');
  }
  return hash.digest('hex');
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  console.log(await runtimeContract(path.resolve(fileURLToPath(new URL('../..', import.meta.url)))));
}

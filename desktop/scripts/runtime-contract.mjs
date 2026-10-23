import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export async function runtimeContract(root) {
  const contract = JSON.parse(await readFile(path.join(root, 'desktop/runtime-contract.json'), 'utf8'));
  return createHash('sha256').update(JSON.stringify(contract)).digest('hex');
}
/** Same rule as pythonInputText in src/main/source-build.ts: Fluid's [tool.pytest*] tables do not change the environment. */
export function pythonInputText(name, text) {
  if (name !== 'fluid/pyproject.toml' || text.includes('"""') || text.includes("'''")) return text;
  let skip = false;
  return text.split('\n').filter(line => {
    if (/^\s*\[/.test(line)) skip = /^\s*\[\s*tool\.pytest(\.[A-Za-z0-9_-]+)*\s*\]\s*(#.*)?$/.test(line);
    return !skip;
  }).join('\n');
}
export async function pythonDependencies(root) {
  const hash = createHash('sha256');
  // Same list as PYTHON_INPUTS in src/main/source-build.ts: the hash-pinned locks the runtime installs from.
  for (const name of ['requirements.lock', 'requirements-native.lock', 'requirements-voice.lock', 'fluid/pyproject.toml']) {
    hash.update(name + '\0' + pythonInputText(name, (await readFile(path.join(root, name), 'utf8')).replace(/\r\n/g, '\n')) + '\0');
  }
  return hash.digest('hex');
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  console.log(await runtimeContract(path.resolve(fileURLToPath(new URL('../..', import.meta.url)))));
}

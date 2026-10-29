// tsc compiles only .ts; the flight recorder is plain JS (src/recorder/*.js)
// that browser_record reads at runtime and frontend/tests imports directly.
// Ship it beside the compiled tools so dist/tools/record.js finds ../recorder/.
import { copyFileSync, mkdirSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const from = fileURLToPath(new URL('../src/recorder/', import.meta.url));
const to = fileURLToPath(new URL('../dist/recorder/', import.meta.url));
mkdirSync(to, { recursive: true });
for (const name of readdirSync(from)) {
  if (name.endsWith('.js')) copyFileSync(from + name, to + name);
}

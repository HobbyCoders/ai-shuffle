import { rmSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

// This fixed package-relative directory is the only output removed by a build.
rmSync(fileURLToPath(new URL('../dist/', import.meta.url)), { recursive: true, force: true });

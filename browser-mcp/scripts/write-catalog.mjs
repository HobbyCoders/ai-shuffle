import { writeFile } from 'node:fs/promises';
import { TOOL_CATALOG } from '../dist/catalog.js';
import { SERVER_INSTRUCTIONS } from '../dist/instructions.js';

// Native sessions load the same catalog without spawning a STDIO helper.
await writeFile(new URL('../dist/catalog.json', import.meta.url), JSON.stringify({
  version: 1, instructions: SERVER_INSTRUCTIONS, tools: TOOL_CATALOG,
}));

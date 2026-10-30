// No application/preload code before Job admission; NODE_OPTIONS is excluded.
const fs = require('node:fs');
const go = Buffer.alloc(1);
if (fs.readSync(0, go, 0, 1, null) !== 1 || go[0] !== 0x47) process.exit(70);
process.argv.splice(1, 1);
process.argv[1] = require('node:path').resolve(process.argv[1]);
// Preserve require.main === module, just like the original CLI entrypoint.
require('node:module').runMain(process.argv[1]);

// Offline D1 stand-in executed from the trusted control node_modules in the
// two-checkout regression. The candidate has a different, poisonous executable.
const fs = require('node:fs');
const path = require('node:path');
if (process.env.DESKTOP_APPLICATION_SIGNING_KEY || process.env.GH_TOKEN) throw Error('Wrangler received excess credentials');
const root = process.env.RUNNER_TEMP;
fs.writeFileSync(path.join(root, 'trusted-wrangler-called'), 'yes');
const args = process.argv.slice(2);
const sql = args[args.indexOf('--command') + 1];
const file = path.join(root, 'record.json');
let saved = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, 'utf8')) : null;
let rows = [];
if (sql.startsWith('SELECT revision,release_repository_id')) rows = [];
else if (sql.startsWith('SELECT revision,sequence')) rows = [{revision:'b'.repeat(40),sequence:1}];
else if (sql.startsWith('SELECT MAX(sequence)')) rows = [{maximum:1}];
else if (sql.startsWith('INSERT')) {
 const match = sql.match(/INTO source_releases\(revision,channel,version,asset_id,size,manifest,sequence,release_repository,release_repository_id\) SELECT '([a-f0-9]{40})','(stable|edge)','([^']+)',(\d+),(\d+),'(.*?)',(\d+),'([^']+)',(\d+) WHERE/);
 if (!match) throw Error('Invalid publisher SQL');
 saved = {channel:match[2],version:match[3],manifest:match[6],asset_id:Number(match[4]),release_repository:match[8],release_repository_id:Number(match[9])};
 fs.writeFileSync(file, JSON.stringify(saved));
 fs.writeFileSync(path.join(root,'insert.sql'), sql);
} else if (sql.startsWith('SELECT')) rows = saved ? [saved] : [];
else throw Error('Unexpected query');
console.log(JSON.stringify([{results:rows}]));

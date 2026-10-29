// Embed src/recorder/analyze.js into src/recorder/flight-recorder.js so the
// recorder stays one dependency-free script a page can run on its own.
//
//   node browser-mcp/scripts/sync-recorder.mjs          rewrite the embedded copy
//   node browser-mcp/scripts/sync-recorder.mjs --check  exit 1 if it is stale
//
// test/recorder-sync.test.ts runs the same check, so a stale copy fails CI.
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const BEGIN = '  /* @generated-begin analyze.js: do not edit here; edit analyze.js and run `node browser-mcp/scripts/sync-recorder.mjs` */';
const END = '  /* @generated-end analyze.js */';

export const ANALYZE_PATH = fileURLToPath(new URL('../src/recorder/analyze.js', import.meta.url));
export const RECORDER_PATH = fileURLToPath(new URL('../src/recorder/flight-recorder.js', import.meta.url));

/** analyze.js as a classic-script block: `var ANALYZER = (function () { ... })();`. */
export function renderEmbedded(analyzeSource) {
  const names = [];
  const body = analyzeSource.replace(/\r\n/g, '\n').split('\n').map((line) => {
    const exported = /^export (const|function|let|class) ([A-Za-z_$][\w$]*)/.exec(line);
    if (exported) {
      names.push(exported[2]);
      return line.slice('export '.length);
    }
    if (/^(export|import)\b/.test(line)) {
      throw new Error(`analyze.js may only export declarations (export const|function); found: ${line}`);
    }
    return line;
  });
  while (body.length && body[body.length - 1] === '') body.pop();
  if (!names.length) throw new Error('analyze.js exports nothing');
  const indented = body.map((line) => (line ? `  ${line}` : ''));
  return [
    BEGIN,
    '  var ANALYZER = (function () {',
    ...indented,
    `  return { ${names.join(', ')} };`,
    '  })();',
    END,
  ].join('\n');
}

/** The recorder source with its embedded block replaced. */
export function syncedRecorder(recorderSource, analyzeSource) {
  const text = recorderSource.replace(/\r\n/g, '\n');
  const start = text.indexOf(BEGIN);
  const end = text.indexOf(END);
  if (start < 0 || end < start) throw new Error('flight-recorder.js lost its @generated-begin/@generated-end markers');
  return text.slice(0, start) + renderEmbedded(analyzeSource) + text.slice(end + END.length);
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  const recorder = readFileSync(RECORDER_PATH, 'utf8');
  const next = syncedRecorder(recorder, readFileSync(ANALYZE_PATH, 'utf8'));
  if (process.argv.includes('--check')) {
    if (next !== recorder.replace(/\r\n/g, '\n')) {
      console.error('flight-recorder.js is stale: run node browser-mcp/scripts/sync-recorder.mjs');
      process.exit(1);
    }
    console.log('flight-recorder.js embeds the current analyze.js');
  } else if (next !== recorder) {
    writeFileSync(RECORDER_PATH, next);
    console.log('flight-recorder.js updated');
  } else {
    console.log('flight-recorder.js already current');
  }
}

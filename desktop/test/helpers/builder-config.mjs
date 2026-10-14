import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { log } = require('builder-util');

// Node 22's test children share stdout with the runner's v8 event frames.
// electron-builder writes raw, multi-part messages there; a frame followed by
// a log in the same pipe read can make the runner deserialize the log as v8.
// Keep all builder diagnostics, but send them over the separate stderr pipe.
// This adapter is test-only and each test file has its own isolated process.
log.stream = process.stderr;

export const { getConfig, validateConfiguration } = require('app-builder-lib/out/util/config/config');

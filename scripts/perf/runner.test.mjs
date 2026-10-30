// Fault-inject the actual CLIs without opening a browser, database or network.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { validateOutput } from './browser-helpers.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const FAKE_PLAYWRIGHT = String.raw`
const fs = require('node:fs');
let gotos = 0, content = '';
const page = {
  on() {},
  async goto() {
    if (gotos++ < Number(process.env.PERF_TEST_SETUP_FAILURES || 0)) throw new Error('injected navigation failure');
  },
  async waitForFunction() {}, async waitForTimeout() {},
  locator() { return { async waitFor() {}, async fill(value) { content = value; }, async textContent() { return content; } }; },
  async evaluate(fn) {
    if (String(fn).includes('const P = window.__perf')) return { feeds: [], longtasks: [], dom: 1 };
    if (String(fn).includes('feedBytes: j.feedBytes')) return {
      t0: 0, first: 1, usable: 2, composer: 1, bottom: 2, feedBytes: [], surfaces: [], end: 3,
      composerSame: true, mountedRows: 120, firstIndex: 100, bottomGap: 0
    };
    return 0;
  }
};
const browser = {
  version() { return 'stub'; },
  async newContext() {
    return {
      request: { async get() { return { status() { return Number(process.env.PERF_TEST_AUTH_STATUS || 200); } }; } },
      async storageState() { return {}; }, async addInitScript() {},
      async newPage() {
        if (process.env.PERF_TEST_PAGE_FAILURE) throw new Error('injected page setup failure');
        return page;
      },
      async newCDPSession() { return { async send() {} }; },
      async close() { console.log('@CONTEXT_CLOSED@'); }
    };
  },
  async close() { console.log('@BROWSER_CLOSED@'); }
};
exports.chromium = { async launch() {
  console.log('@BROWSER_LAUNCHED@');
  if (process.env.PERF_TEST_WRITE_FAILURE) {
    const original = fs.writeFileSync;
    fs.writeFileSync = function(file, ...args) {
      if (file === process.env.PERF_TEST_OUT) {
        const error = new Error('injected disk failure'); error.code = 'ENOSPC'; throw error;
      }
      return original.call(this, file, ...args);
    };
  }
  return browser;
} };
`;
const FAKE_PYTHON = String.raw`#!/usr/bin/env node
const readline = require('node:readline');
function ready() {
  console.log('@PERF_APP_SPAWN@ 1');
  console.log('Branch preview access (open once): http://127.0.0.1:1/fake-access');
}
readline.createInterface({input: process.stdin}).on('line', line => {
  if (line === 'restart') ready();
  if (line === 'stop') {
    if (process.env.PERF_TEST_EXIT_SIGNAL) process.kill(process.pid, 'SIGTERM');
    else process.exit(Number(process.env.PERF_TEST_EXIT_CODE || 0));
  }
});
ready();
`;

function fixture(t) {
	const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'shuffle-perf-runner-test-'));
	t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
	const moduleDirectory = path.join(directory, 'node_modules/playwright-core');
	fs.mkdirSync(moduleDirectory, { recursive: true });
	fs.writeFileSync(path.join(directory, 'package.json'), '{}');
	fs.writeFileSync(path.join(moduleDirectory, 'package.json'), '{"main":"index.cjs"}');
	fs.writeFileSync(path.join(moduleDirectory, 'index.cjs'), FAKE_PLAYWRIGHT);
	const python = path.join(directory, 'fake-python.cjs');
	fs.writeFileSync(python, FAKE_PYTHON, { mode: 0o700 });
	return { directory, out: path.join(directory, 'result.json'), python };
}

function run(f, script, flags = [], env = {}, out = f.out) {
	const result = spawnSync(process.execPath, [path.join(ROOT, 'scripts/perf', script), '--out', out, ...flags], {
		cwd: ROOT, encoding: 'utf8', timeout: 10000,
		env: { PATH: process.env.PATH, PERF_ACCESS_URL: 'http://127.0.0.1:1/fake-access',
			PERF_PLAYWRIGHT_CORE: path.join(f.directory, 'package.json'), PERF_PYTHON: f.python,
			PERF_TEST_OUT: out, ...env }
	});
	assert.ifError(result.error);
	return result;
}
const CHAT_FLAGS = ['--journeys', 'warm', '--large', 'large', '--small', 'small', '--runs', '2', '--platform', 'desktop'];
const STARTUP_FLAGS = ['--runs', '1', '--warmup', '1', '--platform', 'desktop', '--seed', 'none'];

for (const [name, env, code, signal] of [
	['nonzero', { PERF_TEST_EXIT_CODE: '7' }, 7, null],
	['signal', { PERF_TEST_EXIT_SIGNAL: '1' }, null, 'SIGTERM']
]) test(`startup treats ${name} supervisor shutdown as failure`, (t) => {
	const f = fixture(t), result = run(f, 'startup-repeat.mjs', STARTUP_FLAGS, env);
	assert.equal(result.status, 1, result.stdout + result.stderr);
	const report = JSON.parse(fs.readFileSync(f.out, 'utf8'));
	assert.equal(report.failed, true);
	assert.equal(report.cleanedUp, false);
	assert.equal(report.previewExitCode, code);
	assert.equal(report.previewExitSignal, signal);
	assert.match(result.stdout, /@BROWSER_CLOSED@/);
});

test('startup successful shutdown remains a success', (t) => {
	const f = fixture(t), result = run(f, 'startup-repeat.mjs', STARTUP_FLAGS);
	assert.equal(result.status, 0, result.stdout + result.stderr);
	const report = JSON.parse(fs.readFileSync(f.out, 'utf8'));
	assert.equal(report.cleanedUp, true);
	assert.equal(report.previewExitCode, 0);
	assert.match(result.stdout, /@BROWSER_CLOSED@/);
});

for (const [script, flags] of [['chat-journeys.mjs', CHAT_FLAGS], ['startup-repeat.mjs', STARTUP_FLAGS]]) {
	test(`${script} validates output before launching resources`, (t) => {
		const f = fixture(t), out = path.join(f.directory, 'missing/result.json');
		const result = run(f, script, flags, {}, out);
		assert.equal(result.status, 1, result.stdout + result.stderr);
		assert.doesNotMatch(result.stdout, /@BROWSER_LAUNCHED@/);
	});
	test(`${script} closes its browser after a post-launch output failure`, (t) => {
		const f = fixture(t), result = run(f, script, flags, { PERF_TEST_WRITE_FAILURE: '1' });
		assert.equal(result.status, 1, result.stdout + result.stderr);
		assert.match(result.stdout, /@BROWSER_LAUNCHED@/);
		assert.match(result.stdout, /@BROWSER_CLOSED@/);
	});
}

for (const journey of ['warm', 'switch', 'scroll']) for (const [name, env] of [
	['navigation', { PERF_TEST_SETUP_FAILURES: '2' }],
	['page creation', { PERF_TEST_PAGE_FAILURE: '1' }]
]) test(`${journey} persists every ${name} setup failure`, (t) => {
	const flags = CHAT_FLAGS.map((flag) => flag === 'warm' ? journey : flag);
	const f = fixture(t), result = run(f, 'chat-journeys.mjs', flags, env);
	assert.equal(result.status, 1, result.stdout + result.stderr);
	const report = JSON.parse(fs.readFileSync(f.out, 'utf8'));
	assert.equal(report.meta.runs, 2);
	const name = `desktop.${journey}.setup`;
	assert.equal(report.cells[name].length, 2);
	assert.ok(report.cells[name].every((sample) => sample.failed && sample.phase === 'setup'));
	assert.equal(report.summary[name].failed, 2);
	assert.equal(report.summary[name].usableMs.n, 0);
	assert.equal((result.stdout.match(/@CONTEXT_CLOSED@/g) || []).length, 3);
	assert.match(result.stdout, /@BROWSER_CLOSED@/);
});

test('output validation preserves earlier report contents', (t) => {
	const f = fixture(t);
	fs.writeFileSync(f.out, 'earlier report');
	validateOutput(f.out);
	assert.equal(fs.readFileSync(f.out, 'utf8'), 'earlier report');
});

test('chat persists global authentication failure', (t) => {
	const f = fixture(t), result = run(f, 'chat-journeys.mjs', CHAT_FLAGS, { PERF_TEST_AUTH_STATUS: '403' });
	assert.equal(result.status, 1, result.stdout + result.stderr);
	const report = JSON.parse(fs.readFileSync(f.out, 'utf8'));
	assert.equal(report.failed, true);
	assert.equal(report.summary.setup.failed, 1);
	assert.equal(report.summary.setup.usableMs.n, 0);
	assert.match(result.stdout, /@BROWSER_CLOSED@/);
});

test('chat keeps setup failures alongside successful timing samples', (t) => {
	const f = fixture(t), result = run(f, 'chat-journeys.mjs', CHAT_FLAGS, { PERF_TEST_SETUP_FAILURES: '1' });
	assert.equal(result.status, 1, result.stdout + result.stderr);
	const report = JSON.parse(fs.readFileSync(f.out, 'utf8'));
	assert.equal(report.summary['desktop.warm.setup'].failed, 1);
	assert.equal(report.summary['desktop.warm.large'].failed, 0);
	assert.equal(report.summary['desktop.warm.large'].usableMs.n, 1);
});

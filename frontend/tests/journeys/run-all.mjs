// Run UI journeys one after another against the running dev instance and
// summarise pass/fail per journey and case.
//
//   node tests/journeys/run-all.mjs                     every journey
//   node tests/journeys/run-all.mjs --changed           the journeys this branch's diff needs (select.mjs)
//   node tests/journeys/run-all.mjs --changed --base <ref>
//   node tests/journeys/run-all.mjs chat-send-stream tool-cards    these journeys
//   node tests/journeys/run-all.mjs --only=phone        pass --only=<substring> to every journey
//   node tests/journeys/run-all.mjs --pair [--changed]  the two-worker journeys, against `up --pair`
//
// Needs `python -m scripts.devloop up` running (see README.md); the
// two-worker journeys (two-worker-*) need `up --pair` and run only with
// --pair (plain runs leave them out). Do not edit
// frontend/src while it runs: Vite hot-reloads the change into the page and
// the recorder reports the reload as a remount.
//
// Writes <JOURNEY_OUT>/summary.json and .md (JOURNEY_OUT defaults to
// <tmp>/journeys). Exit code 1 when a case failed, 0 otherwise (a case
// marked as a known bug that still fails the known way counts as passing).
import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { allJourneys, changedFiles, isPairJourney, selectJourneys } from './select.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const out = process.env.JOURNEY_OUT || join(tmpdir(), 'journeys');
mkdirSync(out, { recursive: true });

const pairMode = args.includes('--pair');
/** The journeys this mode runs: the two-worker ones with --pair, the rest without. */
const runnable = () => allJourneys().filter((name) => isPairJourney(name) === pairMode);

function pickJourneys() {
	const names = args.filter((a) => !a.startsWith('--') && args[args.indexOf(a) - 1] !== '--base');
	if (names.length) {
		const unknown = names.filter((n) => !runnable().includes(n));
		if (unknown.length) {
			throw new Error(
				`unknown journey${pairMode ? '' : ' (two-worker-* journeys need --pair)'}: ${unknown.join(', ')} (have: ${runnable().join(', ')})`
			);
		}
		return { journeys: names, why: 'named on the command line' };
	}
	if (args.includes('--changed')) {
		const baseAt = args.indexOf('--base');
		const base = baseAt >= 0 ? args[baseAt + 1] : 'origin/main';
		const files = changedFiles(base);
		const picked = selectJourneys(files).journeys.filter((name) => isPairJourney(name) === pairMode);
		return { journeys: picked, why: `${files.length} files changed vs ${base}` };
	}
	return { journeys: runnable(), why: pairMode ? 'all two-worker journeys' : 'all journeys' };
}

function runOne(name) {
	const only = args.find((a) => a.startsWith('--only='));
	return new Promise((resolve) => {
		const started = Date.now();
		const child = spawn(process.execPath, [join(here, `${name}.journey.mjs`), ...(only ? [only] : [])], {
			env: { ...process.env, JOURNEY_OUT: out },
			stdio: ['ignore', 'pipe', 'pipe']
		});
		let log = '';
		child.stdout.on('data', (d) => (log += d));
		child.stderr.on('data', (d) => (log += d));
		child.on('close', (code) => {
			writeFileSync(join(out, `${name}.log`), log);
			const summaryPath = join(out, name, 'summary.json');
			let summary = null;
			if (existsSync(summaryPath)) {
				const parsed = JSON.parse(readFileSync(summaryPath, 'utf8'));
				// Only a summary this run wrote counts (not one left from an earlier run).
				if (Date.parse(parsed.finishedAt) >= started) summary = parsed;
			}
			resolve({ name, code, ms: Date.now() - started, summary, tail: log.trim().split('\n').slice(-8).join('\n') });
		});
	});
}

const { journeys, why } = pickJourneys();
console.log(`running ${journeys.length} journeys (${why}); timelines in ${out}`);
if (!journeys.length) {
	console.log('no journey needed');
	process.exit(0);
}

const results = [];
for (const name of journeys) {
	process.stdout.write(`  ${name} … `);
	const result = await runOne(name);
	results.push(result);
	const cases = result.summary?.cases || [];
	const passed = cases.filter((c) => c.status === 'pass').length;
	const known = cases.filter((c) => c.status === 'known-bug').length;
	const failed = cases.filter((c) => !c.ok).length;
	const crashed = !result.summary;
	console.log(
		crashed
			? `CRASHED (exit ${result.code}) in ${Math.round(result.ms / 1000)}s\n${result.tail.replace(/^/gm, '      ')}`
			: `${failed ? 'FAIL' : 'ok'}  ${passed}/${cases.length} passed${known ? `, ${known} known bug` : ''}${failed ? `, ${failed} failed` : ''}  (${Math.round(result.ms / 1000)}s)`
	);
	for (const c of cases.filter((x) => !x.ok)) {
		console.log(`      FAIL ${c.id}: ${c.error || c.anomalies.join(' | ')}`);
	}
}

const rows = results.flatMap((r) =>
	r.summary
		? r.summary.cases.map((c) => ({ journey: r.name, case: c.id, status: c.status || (c.ok ? 'pass' : 'fail'), detail: c.error || c.anomalies.join(' | ') || c.knownBug || '' }))
		: [{ journey: r.name, case: '(whole journey)', status: 'crashed', detail: r.tail.split('\n').pop() }]
);
const bad = rows.filter((r) => r.status === 'fail' || r.status === 'crashed');
writeFileSync(join(out, 'summary.json'), JSON.stringify({ finishedAt: new Date().toISOString(), why, rows }, null, 2) + '\n');
const md = [
	'| journey | case | result | detail |',
	'| --- | --- | --- | --- |',
	...rows.map((r) => `| ${r.journey} | ${r.case} | ${r.status} | ${String(r.detail).replace(/\|/g, '\\|').slice(0, 200)} |`)
].join('\n');
writeFileSync(join(out, 'summary.md'), md + '\n');

const count = (status) => rows.filter((r) => r.status === status).length;
console.log(
	`\n${journeys.length} journeys, ${rows.length} cases: ${count('pass')} passed, ${count('known-bug')} known bug, ${count('fail')} failed, ${count('crashed')} crashed` +
		`\nsummary: ${join(out, 'summary.md')}`
);
process.exitCode = bad.length ? 1 : 0;

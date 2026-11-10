// Exercise the exported validator and real CLI without editing the checkout's map.
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { allJourneys, checkMap, loadMap, selectForCi, selectJourneys } from './select.mjs';

const selector = fileURLToPath(new URL('./select.mjs', import.meta.url));
const journeys = allJourneys();
assert.ok(journeys.length > 0, 'the fixture must cover actual journeys');
const missingGlob = 'frontend/tests/journeys/__selector_missing_glob__/*.missing';
const validMap = () => ({
	all: [],
	rules: [{ why: 'selector contract fixture', journeys: [...journeys], globs: ['frontend/tests/journeys/select.mjs'] }]
});

for (const { name, change, problems, status } of [
	{ name: 'a valid map passes', change: () => {}, problems: [], status: 0 },
	{
		name: 'a glob matching no tracked file fails',
		change: (map) => { map.rules[0].globs = [missingGlob]; },
		problems: [`glob matches no tracked file: ${missingGlob} (selector contract fixture)`],
		status: 2
	},
	{
		name: 'a journey with no rule fails',
		change: (map) => { map.rules[0].journeys = journeys.slice(1); },
		problems: [`journey "${journeys[0]}" has no rule of its own in map.json`],
		status: 2
	},
	{
		name: 'an unknown journey fails',
		change: (map) => { map.rules[0].journeys.push('__selector_unknown_journey__'); },
		problems: ['unknown journey "__selector_unknown_journey__" (selector contract fixture)'],
		status: 2
	},
	{
		name: 'a rule without why fails',
		change: (map) => { delete map.rules[0].why; },
		problems: ['a rule has no "why": ["frontend/tests/journeys/select.mjs"]'],
		status: 2
	}
]) {
	test(name, (t) => {
		const map = validMap();
		change(map);
		assert.deepEqual(checkMap(map), problems);

		const directory = mkdtempSync(join(tmpdir(), 'shuffle-journey-map-'));
		t.after(() => rmSync(directory, { recursive: true, force: true }));
		const fixture = join(directory, 'map.json');
		writeFileSync(fixture, JSON.stringify(map));
		const result = spawnSync(process.execPath, [selector, '--check', '--map', fixture], {
			cwd: directory, encoding: 'utf8', timeout: 10_000
		});
		assert.ifError(result.error);
		assert.equal(result.status, status, result.stderr);
		assert.equal(result.stderr, problems.map((problem) => `map.json: ${problem}\n`).join(''));
		assert.equal(result.stdout, status === 0
			? `map.json ok: ${journeys.length} journeys, every glob matches a tracked file\n`
			: '');
	});
}

test('pull request selection widens only for an uncovered UI source file', () => {
	const map = {
		unmapped_runs_all: { why: 'uncovered UI source', globs: ['frontend/src/**'] },
		all: [{ why: 'harness', globs: ['frontend/tests/journeys/runner.mjs'] }],
		rules: [{ why: 'one surface', journeys: [journeys[0]], globs: ['frontend/src/lib/covered.ts'] }]
	};
	const names = (files) => selectForCi(files, map, journeys).journeys;
	assert.deepEqual(names(['frontend/src/lib/covered.ts']), [journeys[0]]);
	assert.equal(selectForCi(['frontend/src/lib/covered.ts'], map, journeys).all, journeys.length === 1);
	assert.deepEqual(names(['docs/readme.md', 'app/main.py']), []);
	assert.deepEqual(names(['frontend/src/lib/uncovered.test.ts']), []);
	const widened = selectForCi(['frontend/src/lib/covered.ts', 'frontend/src/lib/uncovered.ts'], map, journeys);
	assert.deepEqual(widened.journeys, journeys);
	assert.equal(widened.all, true);
	assert.deepEqual(widened.reasons[journeys[0]], [
		'frontend/src/lib/covered.ts: one surface',
		'frontend/src/lib/uncovered.ts: uncovered UI source'
	]);
	assert.equal(selectForCi(['frontend/tests/journeys/runner.mjs'], map, journeys).all, true);
	// A local selection keeps naming only what the rules name.
	assert.deepEqual(selectJourneys(['frontend/src/lib/uncovered.ts'], map, journeys).journeys, []);
	// Without the key nothing widens.
	assert.deepEqual(selectForCi(['frontend/src/lib/uncovered.ts'], { ...map, unmapped_runs_all: undefined }, journeys).journeys, []);
});

test('the checked-in map widens for UI source and runs everything for its own selection', () => {
	const map = loadMap();
	assert.deepEqual(map.unmapped_runs_all.globs, ['frontend/src/**', 'frontend/static/**']);
	for (const file of ['frontend/tests/journeys/select.mjs', 'frontend/tests/journeys/map.json', '.github/workflows/ci-frontend.yml', 'scripts/devloop/ci.py']) {
		assert.equal(selectForCi([file]).all, true, file);
	}
});

test('--ci --json --files-from reads the listed paths', (t) => {
	const directory = mkdtempSync(join(tmpdir(), 'shuffle-journey-files-'));
	t.after(() => rmSync(directory, { recursive: true, force: true }));
	const listing = join(directory, 'changed.txt');
	const run = (lines) => {
		writeFileSync(listing, lines.join('\n') + '\n');
		const result = spawnSync(process.execPath, [selector, '--ci', '--json', '--files-from', listing], { encoding: 'utf8', timeout: 30_000 });
		assert.equal(result.status, 0, result.stderr);
		return JSON.parse(result.stdout);
	};
	const none = run(['docs/readme.md', 'name with spaces.md']);
	assert.deepEqual([none.journeys, none.all, none.files], [[], false, ['docs/readme.md', 'name with spaces.md']]);
	const everything = run(['frontend/src/lib/__no_rule_covers_this__.ts']);
	assert.deepEqual([everything.journeys, everything.all], [journeys, true]);
});

test('a bad unmapped_runs_all glob fails the map check', () => {
	const map = validMap();
	map.unmapped_runs_all = { why: 'uncovered UI source', globs: [missingGlob] };
	assert.deepEqual(checkMap(map), [`glob matches no tracked file: ${missingGlob} (uncovered UI source)`]);
});

test('an edited map selects what either copy names and never drops a journey', () => {
	const mapFile = 'frontend/tests/journeys/map.json';
	const rule = (name, glob) => ({ why: `rule for ${name}`, journeys: [name], globs: [glob] });
	const base = { all: [{ why: 'the map', globs: [mapFile] }], rules: [rule(journeys[0], 'app/a.py')] };
	const edited = { all: base.all, rules: [rule(journeys.at(-1), 'app/a.py')] };
	// Without the base's copy the edit runs everything.
	assert.equal(selectForCi([mapFile, 'app/a.py'], edited, journeys).all, true);
	const both = selectForCi([mapFile, 'app/a.py'], edited, journeys, base);
	assert.deepEqual(both.journeys, [...new Set([journeys[0], journeys.at(-1)])]);
	assert.deepEqual(selectForCi([mapFile], edited, journeys, base).journeys, []);
	// The base copy is ignored unless the map itself changed.
	assert.deepEqual(selectForCi(['app/a.py'], edited, journeys, base).journeys, [journeys.at(-1)]);
	// An `all` rule in either copy still runs everything.
	const wider = { all: [...base.all, { why: 'everything', globs: ['app/a.py'] }], rules: [] };
	assert.equal(selectForCi([mapFile, 'app/a.py'], edited, journeys, wider).all, true);
	assert.equal(selectForCi([mapFile, 'app/a.py'], wider, journeys, edited).all, true);
});

test('a split journey: its shared cases or either half selects both halves', () => {
	const map = loadMap();
	const journeys = allJourneys();
	for (const stem of ['chat-position-stability', 'stop-with-queued-messages', 'sidebar-truth', 'queue-while-busy']) {
		for (const file of [`${stem}.cases.mjs`, `${stem}.journey.mjs`, `${stem}-2.journey.mjs`]) {
			const picked = selectForCi([`frontend/tests/journeys/${file}`], map, journeys);
			assert.equal(picked.all, false, file);
			assert.deepEqual(picked.journeys.map((j) => j.name ?? j).sort(), [stem, `${stem}-2`], file);
		}
	}
});

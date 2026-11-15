// Exercise the exported validator and real CLI without editing the checkout's map.
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { allJourneys, checkMap, loadMap, selectBeforeQueue, selectForCi, selectJourneys } from './select.mjs';

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

test('before a merge queue a pull request runs only the journeys a rule names', (t) => {
	const mapFile = 'frontend/tests/journeys/map.json';
	const map = {
		unmapped_runs_all: { why: 'uncovered UI source', globs: ['frontend/src/**'] },
		all: [{ why: 'harness', globs: ['frontend/tests/journeys/runner.mjs', mapFile] }],
		rules: [{ why: 'one surface', journeys: [journeys[0]], globs: ['frontend/src/lib/covered.ts'] }]
	};
	const files = ['frontend/src/lib/covered.ts', 'frontend/src/lib/uncovered.ts', 'frontend/tests/journeys/runner.mjs'];
	// The queue's selection, for comparison: the harness and the uncovered file run everything.
	assert.equal(selectForCi(files, map, journeys).all, true);
	const before = selectBeforeQueue(files, map, journeys);
	assert.deepEqual(before.journeys, [journeys[0]]);
	assert.deepEqual(before.reasons[journeys[0]], ['frontend/src/lib/covered.ts: one surface']);
	assert.deepEqual(selectBeforeQueue(files.slice(1), map, journeys).journeys, []);
	// It never names a journey the queue's selection leaves out.
	for (const each of [files, files.slice(0, 1), ['docs/readme.md'], [mapFile, files[0]]]) {
		const whole = selectForCi(each, map, journeys).journeys;
		for (const name of selectBeforeQueue(each, map, journeys).journeys) assert.ok(whole.includes(name), name);
	}
	// An edited map still selects what either copy names for the other files.
	const base = { ...map, rules: [{ why: 'base rule', journeys: [journeys.at(-1)], globs: ['frontend/src/lib/covered.ts'] }] };
	assert.deepEqual(
		selectBeforeQueue([mapFile, 'frontend/src/lib/covered.ts'], map, journeys, base).journeys,
		[...new Set([journeys[0], journeys.at(-1)])]
	);
	// The caller's maps are not changed.
	assert.equal(map.all.length, 1);
	assert.ok(map.unmapped_runs_all);

	// The real CLI, on the checked-in map: the harness alone selects nothing here and everything with --ci.
	const directory = mkdtempSync(join(tmpdir(), 'shuffle-journey-queue-'));
	t.after(() => rmSync(directory, { recursive: true, force: true }));
	const listing = join(directory, 'changed.txt');
	writeFileSync(listing, `frontend/tests/journeys/runner.mjs\nfrontend/src/lib/__no_rule_covers_this__.ts\nfrontend/tests/journeys/${journeys[0]}.journey.mjs\n`);
	const run = (...flags) => {
		const result = spawnSync(process.execPath, [selector, '--ci', ...flags, '--json', '--files-from', listing], { encoding: 'utf8', timeout: 30_000 });
		assert.equal(result.status, 0, result.stderr);
		return JSON.parse(result.stdout);
	};
	assert.equal(run().all, true);
	const narrowed = run('--before-queue');
	assert.equal(narrowed.all, false);
	assert.ok(narrowed.journeys.includes(journeys[0]));
	assert.ok(narrowed.journeys.length < journeys.length);
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

test("an `all` rule's except globs leave a file to the other rules", () => {
	const map = {
		all: [{ why: 'dev instance', globs: ['scripts/devloop/**'], except: ['scripts/devloop/journey_weights.json'] }],
		rules: [{ why: 'one surface', journeys: [journeys[0]], globs: ['scripts/devloop/journey_weights.json'] }]
	};
	assert.equal(selectForCi(['scripts/devloop/ci.py'], map, journeys).all, true);
	assert.deepEqual(selectForCi(['scripts/devloop/journey_weights.json'], map, journeys).journeys, [journeys[0]]);
	// An excepted UI source file with no other rule is uncovered, so the CI selection still widens.
	const ui = { unmapped_runs_all: { why: 'uncovered UI source', globs: ['frontend/src/**'] },
		all: [{ why: 'core', globs: ['frontend/src/lib/**'], except: ['frontend/src/lib/x.ts'] }], rules: [] };
	assert.equal(selectForCi(['frontend/src/lib/x.ts'], ui, journeys).all, true);
	assert.deepEqual(selectJourneys(['frontend/src/lib/x.ts'], ui, journeys).journeys, []);
	// The checked-in exceptions: shard weights, main-only issue reporting and a manual probe run no journey.
	for (const file of ['scripts/devloop/journey_weights.json', 'scripts/devloop/journey_issues.py', 'scripts/devloop/real_claude_hook_limit_probe.py']) {
		assert.deepEqual(selectForCi([file]).journeys, [], file);
	}
	assert.equal(selectForCi(['scripts/devloop/journey_retry.py']).all, true);
	// An except glob must match a tracked file too.
	const checked = validMap();
	checked.all = [{ why: 'with except', globs: ['frontend/tests/journeys/select.mjs'], except: [missingGlob] }];
	assert.deepEqual(checkMap(checked), [`glob matches no tracked file: ${missingGlob} (with except)`]);
});

test('a journey helper module selects the journeys that import it, and every journey when the runner does', () => {
	const map = loadMap();
	const lib = (name) => `frontend/tests/journeys/lib/${name}`;
	// Imported by two journey files directly.
	assert.deepEqual(selectForCi([lib('voice.mjs')]).journeys, ['voice-call', 'voice-provider-endings']);
	// Imported through a shared cases file: both halves of each split journey that uses it.
	const stopQueue = selectForCi([lib('stop-queue.mjs')]);
	assert.equal(stopQueue.all, false);
	for (const name of ['queue-while-busy', 'queue-while-busy-2', 'stop-with-queued-messages', 'stop-with-queued-messages-2']) {
		assert.ok(stopQueue.journeys.includes(name), name);
	}
	// The runner imports app.mjs, and app.mjs imports trace.mjs: every journey.
	for (const file of [lib('app.mjs'), lib('trace.mjs')]) {
		const every = selectForCi([file]);
		assert.equal(every.all, true, file);
		assert.match(every.reasons[journeys[0]][0], /runner\.mjs imports it/);
		// Before a merge queue that is left to the queue, like an `all` rule.
		assert.deepEqual(selectBeforeQueue([file]).journeys, [], file);
	}
	assert.deepEqual(selectBeforeQueue([lib('voice.mjs')]).journeys, ['voice-call', 'voice-provider-endings']);
	// A module nothing imports, and a unit test beside them, select nothing.
	assert.deepEqual(selectForCi([lib('__nothing_imports_this__.mjs')]).journeys, []);
	assert.deepEqual(selectForCi([lib('verdict.test.mjs')]).journeys, []);
	// Without roots the map check fails, and every root must be a tracked file.
	const broken = { ...validMap(), by_import: { why: 'helpers', globs: map.by_import.globs } };
	assert.deepEqual(checkMap(broken), ['by_import has no "roots" (helpers)']);
	broken.by_import.roots = ['frontend/tests/journeys/__missing_root__.mjs'];
	assert.deepEqual(checkMap(broken), ['by_import root is not a tracked file: frontend/tests/journeys/__missing_root__.mjs']);
});

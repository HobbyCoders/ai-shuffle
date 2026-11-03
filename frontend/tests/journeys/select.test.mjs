// Exercise the exported validator and real CLI without editing the checkout's map.
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { allJourneys, checkMap } from './select.mjs';

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

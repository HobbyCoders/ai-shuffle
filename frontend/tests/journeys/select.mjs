// Which UI journeys does a change need? Reads map.json (changed file globs →
// journeys) and prints the journey names, one per line.
//
//   node tests/journeys/select.mjs                  changes vs origin/main (committed + working tree)
//   node tests/journeys/select.mjs --base <ref>     changes vs another base
//   node tests/journeys/select.mjs --files a b …    these repo-relative paths instead of git
//   node tests/journeys/select.mjs --why            say which rule picked each journey
//   node tests/journeys/select.mjs --json           { journeys, reasons, all, files }
//   node tests/journeys/select.mjs --files-from <f> the paths listed in a file, one per line
//   node tests/journeys/select.mjs --ci             pull request selection: also run every journey
//                                                   when a changed UI source file has no rule
//                                                   (map.json "unmapped_runs_all")
//   node tests/journeys/select.mjs --ci --previous-map <f>  the base's map.json: an edited map then
//                                                   selects what either map names for the other
//                                                   changed files, instead of every journey
//   node tests/journeys/select.mjs --check          validate map.json: every glob matches a
//                                                   tracked file, every journey is reachable
//   node tests/journeys/select.mjs --check --map <path>  validate another map against this checkout
//
// Exit code 0; 2 on a bad map or a git error. Files no rule covers select
// nothing (a docs or backend-only change needs no journey). With --ci an
// uncovered UI source file selects everything instead: the map names what a
// file needs, it does not prove an unlisted UI file needs nothing.
import { execFileSync } from 'node:child_process';
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
export const REPO = join(here, '..', '..', '..');

/** Every journey file's name (without .journey.mjs). */
export function allJourneys() {
	return readdirSync(here)
		.filter((f) => f.endsWith('.journey.mjs'))
		.map((f) => f.replace(/\.journey\.mjs$/, ''))
		.sort();
}

export function loadMap() {
	return JSON.parse(readFileSync(join(here, 'map.json'), 'utf8'));
}

/** A glob (** any depth, * within one path segment, ? one character) as a RegExp. */
export function globToRegExp(glob) {
	let out = '';
	for (let i = 0; i < glob.length; i++) {
		const c = glob[i];
		if (c === '*' && glob[i + 1] === '*') {
			// "**/" matches zero or more directories; a trailing "**" anything.
			if (glob[i + 2] === '/') {
				out += '(?:.*/)?';
				i += 2;
			} else {
				out += '.*';
				i += 1;
			}
		} else if (c === '*') out += '[^/]*';
		else if (c === '?') out += '[^/]';
		else out += c.replace(/[.+^${}()|[\]\\]/g, '\\$&');
	}
	return new RegExp(`^${out}$`);
}

/** Unit test files (foo.test.ts) never need a journey: the code they test does. */
const isTestFile = (file) => /\.test\.[a-z]+$/.test(file);

/**
 * The journeys `files` need: { journeys: [...], reasons: { journey: [why …] } }.
 * A file matching an `all` rule selects every journey.
 */
export function selectJourneys(files, map = loadMap(), journeys = allJourneys()) {
	const picked = new Map();
	const add = (name, why) => {
		if (!journeys.includes(name)) return;
		if (!picked.has(name)) picked.set(name, new Set());
		picked.get(name).add(why);
	};
	for (const file of files) {
		if (isTestFile(file)) continue;
		for (const rule of map.all) {
			if (rule.globs.some((g) => globToRegExp(g).test(file))) {
				for (const name of journeys) add(name, `${file}: ${rule.why}`);
			}
		}
		for (const rule of map.rules) {
			if (!rule.globs.some((g) => globToRegExp(g).test(file))) continue;
			for (const name of rule.journeys) {
				if (name === '$self') {
					const self = file.match(/([^/]+)\.journey\.mjs$/)?.[1];
					if (self) add(self, `${file}: ${rule.why}`);
				} else add(name, `${file}: ${rule.why}`);
			}
		}
	}
	const names = journeys.filter((name) => picked.has(name));
	return { journeys: names, reasons: Object.fromEntries(names.map((n) => [n, [...picked.get(n)]])) };
}

/** Does any rule of the map (an `all` rule or a journey rule) cover `file`? */
export function isMapped(file, map = loadMap()) {
	return [...map.all, ...map.rules].some((rule) => rule.globs.some((g) => globToRegExp(g).test(file)));
}

export const MAP_FILE = 'frontend/tests/journeys/map.json';

/**
 * What a pull request runs: selectJourneys, plus every journey when a changed
 * file under map.unmapped_runs_all has no rule. `all` says the result is the
 * whole suite.
 *
 * Editing map.json changes which journeys are chosen, not how the app behaves.
 * Given the base's map (`previousMap`), an edited map is therefore not itself
 * a reason to run everything: the other changed files select what EITHER map
 * names, so an edit can add journeys to this run but never drop one the base
 * map requires. Without the base's map an edited map runs every journey.
 */
export function selectForCi(files, map = loadMap(), journeys = allJourneys(), previousMap = null) {
	if (previousMap && files.includes(MAP_FILE)) {
		const others = files.filter((file) => file !== MAP_FILE);
		const picked = new Map();
		for (const each of [map, previousMap]) {
			const part = selectForCi(others, each, journeys);
			for (const name of part.journeys) picked.set(name, [...(picked.get(name) ?? []), ...part.reasons[name]]);
		}
		const names = journeys.filter((name) => picked.has(name));
		return {
			journeys: names,
			reasons: Object.fromEntries(names.map((n) => [n, [...new Set(picked.get(n))]])),
			all: names.length === journeys.length
		};
	}
	const result = selectJourneys(files, map, journeys);
	const fallback = map.unmapped_runs_all;
	const picked = new Map(result.journeys.map((name) => [name, [...result.reasons[name]]]));
	for (const file of files) {
		if (isTestFile(file) || !fallback || isMapped(file, map)) continue;
		if (!fallback.globs.some((g) => globToRegExp(g).test(file))) continue;
		for (const name of journeys) picked.set(name, [...(picked.get(name) ?? []), `${file}: ${fallback.why}`]);
	}
	const names = journeys.filter((name) => picked.has(name));
	return {
		journeys: names,
		reasons: Object.fromEntries(names.map((n) => [n, [...new Set(picked.get(n))]])),
		all: names.length === journeys.length
	};
}

function git(args) {
	return execFileSync('git', args, { cwd: REPO, encoding: 'utf8' });
}

/** Repo-relative files changed vs `base` (merge base), plus uncommitted and untracked ones. */
export function changedFiles(base = 'origin/main') {
	const mergeBase = git(['merge-base', base, 'HEAD']).trim();
	const lines = [
		...git(['diff', '--name-only', mergeBase]).split('\n'),
		...git(['ls-files', '--others', '--exclude-standard']).split('\n')
	];
	return [...new Set(lines.map((l) => l.trim()).filter(Boolean))].sort();
}

/** Problems with map.json against the checkout: [message …]. */
export function checkMap(map = loadMap(), journeys = allJourneys()) {
	const problems = [];
	// Tracked plus new (untracked, not ignored) files: a map in a branch may name files it adds.
	const tracked = [...git(['ls-files']).split('\n'), ...git(['ls-files', '--others', '--exclude-standard']).split('\n')].filter(Boolean);
	const rules = [...map.all.map((r) => ({ ...r, journeys: ['(all)'] })), ...map.rules];
	if (map.unmapped_runs_all) rules.push({ ...map.unmapped_runs_all, journeys: ['(all)'] });
	for (const rule of rules) {
		if (!rule.why) problems.push(`a rule has no "why": ${JSON.stringify(rule.globs)}`);
		for (const glob of rule.globs) {
			const re = globToRegExp(glob);
			if (!tracked.some((f) => re.test(f))) problems.push(`glob matches no tracked file: ${glob} (${rule.why})`);
		}
		for (const name of rule.journeys) {
			if (name !== '(all)' && name !== '$self' && !journeys.includes(name)) problems.push(`unknown journey "${name}" (${rule.why})`);
		}
	}
	const named = new Set(map.rules.flatMap((r) => r.journeys));
	for (const name of journeys) {
		if (!named.has(name)) problems.push(`journey "${name}" has no rule of its own in map.json`);
	}
	return problems;
}

if (process.argv[1] && relative(process.argv[1], fileURLToPath(import.meta.url)) === '') {
	const args = process.argv.slice(2);
	const flag = (name) => args.includes(name);
	try {
		if (flag('--check')) {
			const mapAt = args.indexOf('--map');
			const map = mapAt < 0 ? loadMap() : JSON.parse(readFileSync(args[mapAt + 1], 'utf8'));
			const problems = checkMap(map);
			for (const p of problems) console.error(`map.json: ${p}`);
			if (problems.length) process.exit(2);
			console.log(`map.json ok: ${allJourneys().length} journeys, every glob matches a tracked file`);
			process.exit(0);
		}
		const baseAt = args.indexOf('--base');
		const filesAt = args.indexOf('--files');
		const fromAt = args.indexOf('--files-from');
		const files =
			fromAt >= 0
				? readFileSync(args[fromAt + 1], 'utf8').split('\n').map((l) => l.trim()).filter(Boolean)
				: filesAt >= 0
					? args.slice(filesAt + 1).filter((a) => !a.startsWith('--'))
					: changedFiles(baseAt >= 0 ? args[baseAt + 1] : 'origin/main');
		const previousAt = args.indexOf('--previous-map');
		const previous = previousAt >= 0 ? JSON.parse(readFileSync(args[previousAt + 1], 'utf8')) : null;
		const selected = flag('--ci') ? selectForCi(files, loadMap(), allJourneys(), previous) : selectJourneys(files);
		const result = { ...selected, all: selected.journeys.length === allJourneys().length };
		if (flag('--json')) console.log(JSON.stringify({ ...result, files }, null, 2));
		else if (flag('--why')) {
			for (const name of result.journeys) console.log(`${name}\n${result.reasons[name].map((r) => `    ${r}`).join('\n')}`);
			if (!result.journeys.length) console.log(`(no journey needed for ${files.length} changed files)`);
		} else for (const name of result.journeys) console.log(name);
	} catch (error) {
		console.error(`select.mjs: ${error.message}`);
		process.exit(2);
	}
}

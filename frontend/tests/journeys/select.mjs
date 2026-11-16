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
//   node tests/journeys/select.mjs --ci --before-queue  a pull request that a merge queue checks
//                                                   again before main: only the journeys a rule
//                                                   names; the `all` rules and the unmapped
//                                                   fallback are left to the queue's run
//   node tests/journeys/select.mjs --check          validate map.json: every glob matches a
//                                                   tracked file, every journey is reachable
//   node tests/journeys/select.mjs --check --map <path>  validate another map against this checkout
//
// map.json's `all` rules select every journey (minus their `except` globs);
// `by_import` files (the helpers in lib/) select the journeys that import them,
// or every journey when one of its `roots` (the runner) does or when an
// import cannot be followed (the walk only errs toward more).
//
// Exit code 0; 2 on a bad map or a git error. Files no rule covers select
// nothing (a docs or backend-only change needs no journey). With --ci an
// uncovered UI source file selects everything instead: the map names what a
// file needs, it does not prove an unlisted UI file needs nothing.
import { execFileSync } from 'node:child_process';
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join, posix, relative } from 'node:path';
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

const matches = (globs, file) => (globs ?? []).some((g) => globToRegExp(g).test(file));

/** Does this `all` rule cover `file`: one of its globs and none of its `except` globs? */
const allRuleCovers = (rule, file) => matches(rule.globs, file) && !matches(rule.except, file);

const JOURNEY_DIR = 'frontend/tests/journeys';
const BEFORE_QUEUE = Symbol('before the merge queue');
// Whitespace and comments may sit between `import`, `(`, `from` and the specifier.
const GAP = String.raw`(?:\s|\/\*[\s\S]*?\*\/|\/\/[^\n]*(?:\n|$))*`;
// import x from '…', export … from '…', import '…' and import('…') with a plain string.
const LITERAL_IMPORT = new RegExp(String.raw`(?:\bfrom|\bimport(?:${GAP}\()?)${GAP}(['"])([^'"\n]+)\1`, 'g');
// Every dynamic import call; one whose argument is not a plain string cannot be followed.
const DYNAMIC_IMPORT = new RegExp(String.raw`\bimport${GAP}\(${GAP}`, 'g');
const PLAIN_STRING = /^(['"])[^'"\n]+\1/;
const readRepoFile = (file) => readFileSync(join(REPO, file), 'utf8');

/**
 * `source` with every comment replaced by spaces (same length, so offsets line
 * up). Strings and template literals are copied as they are, so a `//` in a URL
 * is not a comment. Only tells imports in code from imports quoted in comments;
 * edges are still taken from the whole source.
 */
function withoutComments(source) {
	let out = '';
	for (let i = 0; i < source.length;) {
		const c = source[i];
		if (c === '/' && (source[i + 1] === '/' || source[i + 1] === '*')) {
			const close = source[i + 1] === '/' ? '\n' : '*/';
			const end = source.indexOf(close, i + 2);
			const stop = end === -1 ? source.length : end + (close === '*/' ? 2 : 0);
			out += source.slice(i, stop).replace(/[^\n]/g, ' ');
			i = stop;
		} else if (c === '"' || c === "'" || c === '`') {
			let j = i + 1;
			while (j < source.length && source[j] !== c && !(c !== '`' && source[j] === '\n')) j += source[j] === '\\' ? 2 : 1;
			out += source.slice(i, j + 1);
			i = j + 1;
		} else {
			out += c;
			i++;
		}
	}
	return out;
}

/**
 * Every repo-relative module `entry` reaches through relative imports (static,
 * re-exports and plain-string dynamic imports), itself included: { files, uncertain }.
 * `uncertain` names the first thing that kept the walk from being complete: an
 * import in code whose target cannot be read (deleted, renamed, unreadable) or a
 * dynamic import of a computed path. The caller then treats `entry` as reaching
 * every module. An import quoted in a comment is followed when its target
 * exists, which can only widen the answer, and ignored when it does not.
 */
function reachable(entry, cache, read = readRepoFile) {
	if (cache.has(entry)) return cache.get(entry);
	const files = new Set();
	let uncertain = null;
	const queue = [[entry, true]];
	while (queue.length) {
		const [file, inCode] = queue.pop();
		if (files.has(file)) continue;
		let source;
		try {
			source = read(file);
		} catch (error) {
			if (inCode) uncertain ??= `${file} cannot be read (${error.code ?? error.message})`;
			continue;
		}
		files.add(file);
		const code = withoutComments(source);
		for (const match of source.matchAll(LITERAL_IMPORT)) {
			const spec = match[2];
			if (!spec.startsWith('./') && !spec.startsWith('../')) continue;
			// In code when the specifier survives comment removal.
			const at = match.index + match[0].length - spec.length - 1;
			queue.push([posix.join(posix.dirname(file), spec), code.slice(at, at + spec.length) === spec]);
		}
		for (const call of code.matchAll(DYNAMIC_IMPORT)) {
			if (!PLAIN_STRING.test(code.slice(call.index + call[0].length))) {
				uncertain ??= `${file} imports a computed path`;
			}
		}
	}
	const result = { files, uncertain };
	cache.set(entry, result);
	return result;
}

/**
 * The journeys `files` need: { journeys: [...], reasons: { journey: [why …] } }.
 * A file matching an `all` rule (and none of its `except` globs) selects every
 * journey. A file matching `by_import` selects the journeys whose files import
 * it, directly or through other modules, and every journey when one of its
 * `roots` (what every journey runs under: the runner) does.
 */
export function selectJourneys(files, map = loadMap(), journeys = allJourneys(), { read } = {}) {
	const picked = new Map();
	const add = (name, why) => {
		if (!journeys.includes(name)) return;
		if (!picked.has(name)) picked.set(name, new Set());
		picked.get(name).add(why);
	};
	const imports = new Map();
	for (const file of files) {
		if (isTestFile(file)) continue;
		for (const rule of map.all) {
			if (allRuleCovers(rule, file)) {
				for (const name of journeys) add(name, `${file}: ${rule.why}`);
			}
		}
		if (map.by_import && matches(map.by_import.globs, file)) {
			const why = `${file}: ${map.by_import.why}`;
			// Every journey when a root reaches the file, or when any import walk is
			// incomplete: an unknown import graph never narrows the selection.
			const graphs = [...(map.by_import.roots ?? []), ...journeys.map((name) => `${JOURNEY_DIR}/${name}.journey.mjs`)]
				.map((entry) => [entry, reachable(entry, imports, read)]);
			const unsure = graphs.find(([, graph]) => graph.uncertain);
			const root = (map.by_import.roots ?? []).find((entry) => reachable(entry, imports, read).files.has(file));
			const everyRun = unsure ? `the journeys' imports are not fully known: ${unsure[1].uncertain}`
				: root && `${root.split('/').pop()} imports it`;
			// Before a merge queue, every journey is left to the queue's run, like an
			// `all` rule; with an incomplete walk the journeys it did see still run.
			if (root && !unsure && map.by_import[BEFORE_QUEUE]) continue;
			const every = everyRun && !map.by_import[BEFORE_QUEUE];
			for (const name of journeys) {
				if (every) add(name, `${why} (${everyRun})`);
				else if (reachable(`${JOURNEY_DIR}/${name}.journey.mjs`, imports, read).files.has(file)) add(name, why);
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

/** Does any rule of the map (an `all` rule, `by_import` or a journey rule) cover `file`? */
export function isMapped(file, map = loadMap()) {
	return map.all.some((rule) => allRuleCovers(rule, file))
		|| matches(map.by_import?.globs, file)
		|| map.rules.some((rule) => matches(rule.globs, file));
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
export function selectForCi(files, map = loadMap(), journeys = allJourneys(), previousMap = null, options = {}) {
	if (previousMap && files.includes(MAP_FILE)) {
		const others = files.filter((file) => file !== MAP_FILE);
		const picked = new Map();
		for (const each of [map, previousMap]) {
			const part = selectForCi(others, each, journeys, null, options);
			for (const name of part.journeys) picked.set(name, [...(picked.get(name) ?? []), ...part.reasons[name]]);
		}
		const names = journeys.filter((name) => picked.has(name));
		return {
			journeys: names,
			reasons: Object.fromEntries(names.map((n) => [n, [...new Set(picked.get(n))]])),
			all: names.length === journeys.length
		};
	}
	const result = selectJourneys(files, map, journeys, options);
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

/**
 * What a pull request runs when a merge queue checks the merged result again
 * before main moves: the journeys a rule names for the changed files, and no
 * more. The `all` rules, the unmapped fallback and a `by_import` module one of
 * its roots reaches are left to the queue's run, which selects with selectForCi
(an incomplete import walk still runs the journeys it followed). Every journey the change needs still passes
 * before main moves, once per pull request instead of once per push.
 */
export function selectBeforeQueue(files, map = loadMap(), journeys = allJourneys(), previousMap = null, options = {}) {
	const named = (each) => each && {
		...each,
		all: [],
		unmapped_runs_all: undefined,
		by_import: each.by_import && { ...each.by_import, [BEFORE_QUEUE]: true }
	};
	return selectForCi(files, named(map), journeys, named(previousMap), options);
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
	if (map.by_import) {
		rules.push({ ...map.by_import, journeys: ['(all)'] });
		const roots = map.by_import.roots ?? [];
		if (!roots.length) problems.push(`by_import has no "roots" (${map.by_import.why})`);
		for (const root of roots) if (!tracked.includes(root)) problems.push(`by_import root is not a tracked file: ${root}`);
	}
	for (const rule of rules) {
		if (!rule.why) problems.push(`a rule has no "why": ${JSON.stringify(rule.globs)}`);
		for (const glob of [...rule.globs, ...(rule.except ?? [])]) {
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
		const forCi = flag('--before-queue') ? selectBeforeQueue : selectForCi;
		const selected = flag('--ci') ? forCi(files, loadMap(), allJourneys(), previous) : selectJourneys(files);
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

/**
 * #599: a status colour mixed with a theme neutral must not be mixed in a
 * polar space (oklch, lch, hsl, hwb).
 *
 * The neutrals carry an explicit hue at (near) zero chroma: the light theme's
 * --card, --background and --popover are oklch(1 0 0), hue 0, and the dark
 * theme's sit at hue 240. A polar mix interpolates the hue towards that
 * meaningless angle, so `color-mix(in oklch, var(--success) 6%, var(--card))`
 * turns pink in the light theme and blue-grey in the dark one. Mixing in oklab
 * keeps the status colour's own hue. Mixes with `transparent`, `white` or
 * `black` are fine in oklch (those keywords have no hue to pull towards), and
 * so is a deliberate blend of two chromatic colours.
 *
 * This scans every stylesheet and component in the web and mobile apps.
 */
import { describe, expect, it } from 'vitest';
import { readdirSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join, relative, resolve } from 'node:path';

const repo = resolve(dirname(fileURLToPath(import.meta.url)), '../../../..');
const ROOTS = ['frontend/src', 'mobile/src'];
const EXTENSIONS = /\.(svelte|css|ts|js)$/;
const SKIP_DIRS = new Set(['node_modules', '.svelte-kit', 'build', 'docs']);

/** Theme colours that carry a status or brand meaning (and so a hue). */
const STATUS_TOKEN =
	/^var\(--(primary|success|warning|destructive|info|selection|ring|orb|st-primary|emerald|rose|blue|status-[a-z-]+|tool-[a-z]+|feature-[a-z]+)\b/;
/** Operands with no hue for a polar mix to drift towards. */
const HUELESS = new Set(['transparent', 'white', 'black', 'currentcolor']);

/**
 * Files whose status tints are converted by another open pull request.
 * Each entry must still contain a polar mix, so a fixed file can't linger here.
 */
const PENDING: Record<string, string> = {};

function* sourceFiles(dir: string): Generator<string> {
	for (const entry of readdirSync(dir, { withFileTypes: true })) {
		if (entry.isDirectory()) {
			if (!SKIP_DIRS.has(entry.name)) yield* sourceFiles(join(dir, entry.name));
		} else if (EXTENSIONS.test(entry.name) && !entry.name.includes('.test.')) {
			yield join(dir, entry.name);
		}
	}
}

/** The comma-separated top-level arguments of each `color-mix(...)` call. */
function* colorMixes(text: string): Generator<{ index: number; args: string[] }> {
	const opener = /color-mix\(/g;
	for (let m = opener.exec(text); m; m = opener.exec(text)) {
		const args: string[] = [];
		let depth = 1;
		let current = '';
		let i = m.index + m[0].length;
		for (; i < text.length && depth > 0; i++) {
			const ch = text[i];
			if (ch === '(') depth++;
			if (ch === ')') depth--;
			if (depth === 0) break;
			if (ch === ',' && depth === 1) {
				args.push(current.trim());
				current = '';
			} else {
				current += ch;
			}
		}
		args.push(current.trim());
		yield { index: m.index, args };
		opener.lastIndex = i;
	}
}

const colour = (operand: string) => operand.replace(/\s+[\d.]+%$/, '').trim();

/** Polar-space mixes of a status colour with another theme token, per file. */
function findStatusTintsInPolarSpace(): Map<string, string[]> {
	const found = new Map<string, string[]>();
	for (const root of ROOTS) {
		for (const file of sourceFiles(join(repo, root))) {
			const text = readFileSync(file, 'utf8');
			for (const { index, args } of colorMixes(text)) {
				const [space, first = '', second = ''] = args;
				if (!/^in\s+(oklch|lch|hsl|hwb)\b/i.test(space)) continue;
				const a = colour(first);
				const b = colour(second);
				const status = STATUS_TOKEN.test(a) ? b : STATUS_TOKEN.test(b) ? a : null;
				if (status === null || HUELESS.has(status.toLowerCase()) || !status.startsWith('var(')) continue;
				const line = text.slice(0, index).split('\n').length;
				const rel = relative(repo, file).split('\\').join('/');
				found.set(rel, [...(found.get(rel) ?? []), `${rel}:${line}: color-mix(${args.join(', ')})`]);
			}
		}
	}
	return found;
}

describe('status tints keep their hue (#599)', () => {
	const found = findStatusTintsInPolarSpace();

	it('mixes status colours with theme neutrals in oklab, not a polar space', () => {
		const offenders = [...found.entries()]
			.filter(([file]) => !(file in PENDING))
			.flatMap(([, lines]) => lines);
		expect(offenders, 'use color-mix(in oklab, …) for status tints; see app.css "Status tints"').toEqual([]);
	});

	it('lists only pending files that still need converting', () => {
		const stale = Object.keys(PENDING).filter((file) => !found.has(file));
		expect(stale, 'remove fixed files from PENDING').toEqual([]);
	});

	it('detects the pattern it guards against', () => {
		const sample = 'a { background: color-mix(in oklch, var(--success) 6%, var(--card)); }';
		const mixes = [...colorMixes(sample)];
		expect(mixes).toHaveLength(1);
		expect(mixes[0].args).toEqual(['in oklch', 'var(--success) 6%', 'var(--card)']);
		expect(STATUS_TOKEN.test(colour(mixes[0].args[1]))).toBe(true);
	});
});

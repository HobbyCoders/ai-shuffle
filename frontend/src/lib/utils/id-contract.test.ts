import { describe, expect, it } from 'vitest';
import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import ts from 'typescript';

const repo = resolve(dirname(fileURLToPath(import.meta.url)), '../../../..');
const HELPER = 'frontend/src/lib/utils/id.ts';

function* sourceFiles(dir: string): Generator<string> {
	for (const entry of readdirSync(dir, { withFileTypes: true })) {
		if (entry.isDirectory()) yield* sourceFiles(join(dir, entry.name));
		else if (/\.(svelte|[cm]?[jt]s)$/.test(entry.name) && !/\.(test|spec)\./.test(entry.name)) {
			yield join(dir, entry.name);
		}
	}
}

/** Parse code, not comments/strings; follow aliases before checking UUID reads. */
function hasNativeUUIDAccess(source: string, path: string): boolean {
	const scripts = path.endsWith('.svelte')
		? Array.from(source.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g), (match) => match[1]).join('\n')
		: source;
	const file = ts.createSourceFile(path, scripts, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
	const nodes: ts.Node[] = [];
	const visit = (node: ts.Node) => { nodes.push(node); ts.forEachChild(node, visit); };
	visit(file);
	const aliases = new Set(['crypto']);
	const unwrap = (node: ts.Node): ts.Node => {
		while (ts.isParenthesizedExpression(node) || ts.isAsExpression(node) || ts.isNonNullExpression(node)) node = node.expression;
		return node;
	};
	const key = (node: ts.Node): string | undefined => {
		if (ts.isComputedPropertyName(node)) return key(node.expression);
		return ts.isIdentifier(node) || ts.isStringLiteral(node) ? node.text : undefined;
	};
	const member = (node: ts.Node) => ts.isPropertyAccessExpression(node) ? key(node.name)
		: ts.isElementAccessExpression(node) && node.argumentExpression ? key(node.argumentExpression) : undefined;
	const isGlobal = (node: ts.Node) => ts.isIdentifier(unwrap(node)) && ['globalThis', 'window', 'self'].includes(unwrap(node).getText(file));
	const isCrypto = (node: ts.Node): boolean => {
		node = unwrap(node);
		return ts.isIdentifier(node) ? aliases.has(node.text)
			: (ts.isPropertyAccessExpression(node) || ts.isElementAccessExpression(node)) && member(node) === 'crypto' && isGlobal(node.expression);
	};
	// A fixed point also catches chained aliases declared/assigned in a different order.
	let previousSize = -1;
	while (previousSize !== aliases.size) {
		previousSize = aliases.size;
		for (const node of nodes) {
			if (ts.isVariableDeclaration(node) && node.initializer) {
				if (ts.isIdentifier(node.name) && isCrypto(node.initializer)) aliases.add(node.name.text);
				if (ts.isObjectBindingPattern(node.name) && isGlobal(node.initializer)) {
					for (const binding of node.name.elements) {
						if (key(binding.propertyName ?? binding.name) === 'crypto' && ts.isIdentifier(binding.name)) aliases.add(binding.name.text);
					}
				}
			}
			if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.EqualsToken && ts.isIdentifier(node.left) && isCrypto(node.right)) aliases.add(node.left.text);
		}
	}
	return nodes.some((node) => {
		if ((ts.isPropertyAccessExpression(node) || ts.isElementAccessExpression(node)) && member(node) === 'randomUUID' && isCrypto(node.expression)) return true;
		return ts.isVariableDeclaration(node) && node.initializer && isCrypto(node.initializer)
			&& ts.isObjectBindingPattern(node.name) && node.name.elements.some((binding) => key(binding.propertyName ?? binding.name) === 'randomUUID');
	});
}

function offenders(files: Iterable<[string, string]>): string[] {
	return Array.from(files).filter(([path, source]) => path !== HELPER && hasNativeUUIDAccess(source, path)).map(([path]) => path);
}

describe.each(['frontend/src', 'mobile/src'])('%s UUID guard', (root) => {
	it.each([
		'crypto.randomUUID()',
		'window.crypto?.randomUUID?.()',
		"globalThis.crypto['randomUUID']()",
		'const c = globalThis.crypto; c.randomUUID()',
		'let c; c = window.crypto; const c2 = c; c2.randomUUID()',
		'const { crypto: c } = globalThis; c.randomUUID()',
		'const { randomUUID: uuid } = globalThis.crypto; uuid.call(globalThis.crypto)',
		'const c = crypto; const { randomUUID } = c; randomUUID()',
		"const { ['randomUUID']: uuid } = crypto; uuid()"
	])('rejects native UUID access: %s', (source) => {
		const path = `${root}/uuid-example.ts`;
		expect(offenders([[path, source]])).toEqual([path]);
	});

	it.each([
		"import { randomUUID } from '$lib/utils/id'; randomUUID()",
		"import { randomUUID as uuid } from '$lib/utils/id'; uuid()",
		"import * as ids from '$lib/utils/id'; ids.randomUUID()",
		"const { randomUUID } = helperIds; randomUUID()",
		'crypto.getRandomValues(new Uint8Array(16))',
		'// crypto.randomUUID() is not allowed here',
		"const message = 'crypto.randomUUID()';"
	])('permits helper imports and non-native references: %s', (source) => {
		expect(offenders([[`${root}/uuid-example.ts`, source]])).toEqual([]);
	});

	it('checks Svelte script bodies too', () => {
		const path = `${root}/Example.svelte`;
		expect(offenders([[path, '<script lang="ts">const c = crypto; c.randomUUID()</script><p>Example</p>']])).toEqual([path]);
	});
});

it('permits native access only inside the shared helper', () => {
	expect(offenders([[HELPER, 'crypto.randomUUID()']])).toEqual([]);
});

it('keeps secure-context-only UUID access in the shared HTTP-safe helper', () => {
	const files: [string, string][] = [];
	for (const root of ['frontend/src', 'mobile/src']) {
		for (const path of sourceFiles(join(repo, root))) {
			files.push([relative(repo, path).split('\\').join('/'), readFileSync(path, 'utf8')]);
		}
	}
	expect(offenders(files)).toEqual([]);
});

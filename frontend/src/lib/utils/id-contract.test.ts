import { expect, it } from 'vitest';
import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const repo = resolve(dirname(fileURLToPath(import.meta.url)), '../../../..');

function* sourceFiles(dir: string): Generator<string> {
	for (const entry of readdirSync(dir, { withFileTypes: true })) {
		if (entry.isDirectory()) yield* sourceFiles(join(dir, entry.name));
		else if (/\.(svelte|[cm]?[jt]s)$/.test(entry.name) && !/\.(test|spec)\./.test(entry.name)) {
			yield join(dir, entry.name);
		}
	}
}

it('keeps secure-context-only UUID access in the shared HTTP-safe helper', () => {
	const offenders: string[] = [];
	for (const root of ['frontend/src', 'mobile/src']) {
		for (const path of sourceFiles(join(repo, root))) {
			if (path === join(repo, 'frontend/src/lib/utils/id.ts')) continue;
			const source = readFileSync(path, 'utf8');
			if (/\bcrypto\s*(?:\??\.\s*randomUUID|\[\s*['"]randomUUID['"]\s*\])/.test(source)) {
				offenders.push(relative(repo, path));
			}
		}
	}
	expect(offenders).toEqual([]);
});

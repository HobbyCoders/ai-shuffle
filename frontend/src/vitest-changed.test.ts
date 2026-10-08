// CI's changed-only test selection (scripts/vitest-changed.mjs) must keep
// the tests that read a changed file as text, which Vitest's import graph
// cannot see.
import { describe, expect, it } from 'vitest';
import { resolve } from 'node:path';
import { testFiles, textReferences } from '../scripts/vitest-changed.mjs';

describe('vitest-changed selection', () => {
	const sources: Record<string, string> = {
		'/repo/frontend/src/a.test.ts': "readFileSync('src/lib/components/chat/MessageArea.svelte')",
		'/repo/frontend/src/b.test.ts': "import { x } from './b';",
		'/repo/frontend/src/c.test.ts': "readFileSync('../fluid/fluid/context/peer.py')"
	};
	const tests = Object.keys(sources);
	const read = (path: string) => sources[path];

	it('adds tests that name a changed file', () => {
		const changed = ['/repo/frontend/src/lib/components/chat/MessageArea.svelte'];
		expect(textReferences(changed, tests, read)).toEqual(['/repo/frontend/src/a.test.ts']);
	});

	it('follows files outside the package, such as Python sources', () => {
		expect(textReferences(['/repo/fluid/fluid/context/peer.py'], tests, read)).toEqual([
			'/repo/frontend/src/c.test.ts'
		]);
	});

	it('adds nothing when nothing changed', () => {
		expect(textReferences([], tests, read)).toEqual([]);
	});

	it('finds this test file with forward slashes', () => {
		const found = testFiles(resolve('src'));
		expect(found.some((path: string) => path.endsWith('/src/vitest-changed.test.ts'))).toBe(true);
		expect(found.every((path: string) => !path.includes('\\'))).toBe(true);
	});
});

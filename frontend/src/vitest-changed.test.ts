// CI's changed-only test selection (scripts/vitest-changed.mjs) must keep
// the tests that read a changed file as text, which Vitest's import graph
// cannot see.
import { describe, expect, it, vi } from 'vitest';
import { resolve } from 'node:path';
import { runRelated, testFiles, textReferences } from '../scripts/vitest-changed.mjs';
import type { spawnSync } from 'node:child_process';

describe('vitest-changed selection', () => {
	const sources: Record<string, string> = {
		'/repo/frontend/src/a.test.ts': "readFileSync('src/lib/components/chat/MessageArea.svelte')",
		'/repo/frontend/src/b.test.ts': "import { x } from './b';",
		'/repo/frontend/src/c.test.ts': "readFileSync('../fluid/fluid/context/peer.py')"
	};
	const tests = Object.keys(sources);
	const read = (path: string) => sources[path];

	it('runs long worktree paths without the Windows command shell', () => {
		const related = Array.from({ length: 100 }, (_, i) => `${'long worktree/'.repeat(8)}${i}.ts`);
		expect(related.join(' ').length).toBeGreaterThan(8192);
		const spawn = vi.fn().mockReturnValue({ status: 0 });
		expect(runRelated(related, spawn as unknown as typeof spawnSync).status).toBe(0);
		const [command, args, options] = spawn.mock.calls[0];
		expect(command).toBe(process.execPath);
		expect(args[0].replaceAll('\\', '/')).toMatch(/\/vitest\/vitest\.mjs$/);
		expect(args.slice(1)).toEqual(['related', '--run', '--passWithNoTests', ...related]);
		expect(options).toEqual({ stdio: 'inherit', shell: false });
	});

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

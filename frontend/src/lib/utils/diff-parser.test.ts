/**
 * Tests for diff-parser utilities.
 *
 * Covers: parseDiff, toSideBySide, countChanges, and the new
 * effectiveDiffMode helper that switches to unified on mobile.
 */

import { describe, it, expect } from 'vitest';
import { parseDiff, toSideBySide, countChanges } from './diff-parser';

// ── Sample diffs ────────────────────────────────────────────────────────

const SIMPLE_DIFF = `diff --git a/src/foo.ts b/src/foo.ts
index abc1234..def5678 100644
--- a/src/foo.ts
+++ b/src/foo.ts
@@ -1,3 +1,4 @@
 const a = 1;
-const b = 2;
+const b = 22;
+const c = 3;
 const d = 4;`;

const NEW_FILE_DIFF = `diff --git a/src/new.ts b/src/new.ts
new file mode 100644
index 0000000..abc1234
--- /dev/null
+++ b/src/new.ts
@@ -0,0 +1,5 @@
+export function hello() {
+  console.log('hello');
+}
+
+export default hello;`;

const LARGE_ADDITION_DIFF = `diff --git a/src/big.ts b/src/big.ts
index abc1234..def5678 100644
--- a/src/big.ts
+++ b/src/big.ts
@@ -1,2 +1,22 @@
 import { something } from './something';
+
+export class BigClass {
+  private field1: string;
+  private field2: number;
+  private field3: boolean;
+
+  constructor(f1: string, f2: number, f3: boolean) {
+    this.field1 = f1;
+    this.field2 = f2;
+    this.field3 = f3;
+  }
+
+  getField1(): string { return this.field1; }
+  getField2(): number { return this.field2; }
+  getField3(): boolean { return this.field3; }
+
+  toString(): string {
+    return \`\${this.field1}:\${this.field2}:\${this.field3}\`;
+  }
+}
 export default something;`;

// ── parseDiff ───────────────────────────────────────────────────────────

describe('parseDiff', () => {
	it('parses a simple modification diff', () => {
		const result = parseDiff(SIMPLE_DIFF);
		expect(result.hunks).toHaveLength(1);
		expect(result.oldFile).toBe('src/foo.ts');
		expect(result.newFile).toBe('src/foo.ts');
	});

	it('parses a new file diff', () => {
		const result = parseDiff(NEW_FILE_DIFF);
		expect(result.oldFile).toBeNull();
		expect(result.newFile).toBe('src/new.ts');
		expect(result.hunks).toHaveLength(1);
	});

	it('counts additions and deletions correctly', () => {
		const result = parseDiff(SIMPLE_DIFF);
		const counts = countChanges(result);
		expect(counts.additions).toBe(2);
		expect(counts.deletions).toBe(1);
	});

	it('handles large addition blocks', () => {
		const result = parseDiff(LARGE_ADDITION_DIFF);
		const counts = countChanges(result);
		expect(counts.additions).toBe(20);
		expect(counts.deletions).toBe(0);
	});
});

// ── toSideBySide ────────────────────────────────────────────────────────

describe('toSideBySide', () => {
	it('pairs deletions and additions in changed rows', () => {
		const parsed = parseDiff(SIMPLE_DIFF);
		const pairs = toSideBySide(parsed.hunks);

		// Should have: header, context(a=1), changed(b=2 -> b=22), addition(c=3), context(d=4)
		const changedPairs = pairs.filter(p => p.type === 'changed');
		expect(changedPairs.length).toBeGreaterThanOrEqual(1);

		// First changed pair: deletion on left, addition on right
		expect(changedPairs[0].left).not.toBeNull();
		expect(changedPairs[0].left!.type).toBe('deletion');
		expect(changedPairs[0].right).not.toBeNull();
		expect(changedPairs[0].right!.type).toBe('addition');
	});

	it('fills blank side when additions outnumber deletions', () => {
		const parsed = parseDiff(SIMPLE_DIFF);
		const pairs = toSideBySide(parsed.hunks);

		// The extra addition (const c = 3) should have null on left
		const additionOnly = pairs.filter(p => p.type === 'changed' && p.left === null);
		expect(additionOnly.length).toBe(1);
		expect(additionOnly[0].right!.content).toContain('const c = 3');
	});

	it('creates many blank-left rows for large additions', () => {
		const parsed = parseDiff(LARGE_ADDITION_DIFF);
		const pairs = toSideBySide(parsed.hunks);

		// All 20 additions should appear as changed pairs with null left
		const additionOnly = pairs.filter(p => p.type === 'changed' && p.left === null);
		expect(additionOnly.length).toBe(20);
	});
});

// ── effectiveDiffMode ───────────────────────────────────────────────────

describe('effectiveDiffMode', () => {
	// This function should exist in diff-parser.ts to determine the
	// effective rendering mode based on viewport width.
	// On mobile (<640px), side-by-side should downgrade to unified.

	it('returns unified when viewport is narrow and requested mode is side-by-side', async () => {
		const { effectiveDiffMode } = await import('./diff-parser');
		expect(effectiveDiffMode('side-by-side', 400)).toBe('unified');
	});

	it('returns side-by-side on desktop when requested', async () => {
		const { effectiveDiffMode } = await import('./diff-parser');
		expect(effectiveDiffMode('side-by-side', 1200)).toBe('side-by-side');
	});

	it('returns unified on any viewport when unified is requested', async () => {
		const { effectiveDiffMode } = await import('./diff-parser');
		expect(effectiveDiffMode('unified', 400)).toBe('unified');
		expect(effectiveDiffMode('unified', 1200)).toBe('unified');
	});

	it('uses 640px as the breakpoint', async () => {
		const { effectiveDiffMode } = await import('./diff-parser');
		expect(effectiveDiffMode('side-by-side', 639)).toBe('unified');
		expect(effectiveDiffMode('side-by-side', 640)).toBe('side-by-side');
	});
});

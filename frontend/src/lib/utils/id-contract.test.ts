import { describe, expect, it } from 'vitest';
import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const repo = resolve(dirname(fileURLToPath(import.meta.url)), '../../../..');
const TOKEN_ALLOWLIST = new Set([
	// The shared helper owns native access and the plain-HTTP-safe fallback.
	'frontend/src/lib/utils/id.ts',
	// Imports and calls the shared helper for banked reset idempotency keys.
	'frontend/src/lib/components/settings/providers/banked-resets.svelte.ts',
	// Imports and calls the shared helper for first-run session IDs.
	'frontend/src/lib/services/first-agent-run.ts',
	// Imports and calls the shared helper for typed voice-message IDs.
	'frontend/src/lib/services/voiceAudioStream.ts',
	// Imports and calls the shared helper for the persistent pairing device ID.
	'frontend/src/routes/pair/+page.svelte',
	// Imports and calls the shared helper for preference write IDs.
	'frontend/src/lib/services/preference-sync.ts',
	// Only the comment names the native API; IDs already use getRandomValues.
	'frontend/src/lib/stores/session/pending-queue.ts'
]);

function isCheckedSource(path: string): boolean {
	return /\.(svelte|ts|js|mjs)$/.test(path) && !/\.(test|spec)\./.test(path);
}

function* sourceFiles(dir: string): Generator<string> {
	for (const entry of readdirSync(dir, { withFileTypes: true })) {
		if (entry.isDirectory()) yield* sourceFiles(join(dir, entry.name));
		else if (isCheckedSource(entry.name)) yield join(dir, entry.name);
	}
}

function offenders(files: Iterable<[string, string]>): string[] {
	// Check the whole file, including Svelte markup, comments and helper imports.
	return Array.from(files)
		.filter(([path, source]) => isCheckedSource(path) && !TOKEN_ALLOWLIST.has(path) && /\brandomUUID\b/.test(source))
		.map(([path]) => path);
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
		"const { ['randomUUID']: uuid } = crypto; uuid()",
		'let uuid; ({ randomUUID: uuid } = crypto); uuid.call(crypto)',
		"import { randomUUID } from '$lib/utils/id'; randomUUID()",
		"import { randomUUID as uuid } from '$lib/utils/id'; uuid()",
		"import * as ids from '$lib/utils/id'; ids.randomUUID()",
		'// crypto.randomUUID() is not allowed here',
		"const message = 'crypto.randomUUID()';"
	])('requires an explicit exception for the token: %s', (source) => {
		const path = `${root}/uuid-example.ts`;
		expect(offenders([[path, source]])).toEqual([path]);
	});

	it.each([
		'<button onclick={() => crypto.randomUUID()}>Create</button>',
		'<script lang="ts">const c = crypto; c.randomUUID()</script><p>Example</p>',
		'<!-- randomUUID is mentioned in markup -->'
	])('checks the entire Svelte file: %s', (source) => {
		const path = `${root}/Example.svelte`;
		expect(offenders([[path, source]])).toEqual([path]);
	});

	it.each(['ts', 'js', 'mjs', 'svelte'])('checks .%s source files', (extension) => {
		const path = `${root}/uuid-example.${extension}`;
		expect(offenders([[path, 'crypto.randomUUID()']])).toEqual([path]);
	});

	it.each(['test.ts', 'spec.ts', 'test.js', 'spec.mjs'])('excludes .%s test files', (extension) => {
		expect(offenders([[`${root}/uuid-example.${extension}`, 'crypto.randomUUID()']])).toEqual([]);
	});

	it.each([
		'crypto.getRandomValues(new Uint8Array(16))',
		'const randomUUIDFactory = () => "example";'
	])('permits source without the whole token: %s', (source) => {
		expect(offenders([[`${root}/uuid-example.ts`, source]])).toEqual([]);
	});

	it('documents the accepted limit: computed names without the token are not detected', () => {
		expect(offenders([[`${root}/uuid-example.ts`, "crypto['random'+'UUID']()"]])).toEqual([]);
	});
});

it.each([...TOKEN_ALLOWLIST])('permits the explicitly documented exception: %s', (path) => {
	expect(offenders([[path, 'randomUUID']])).toEqual([]);
});

it('keeps UUID tokens confined to explicitly reviewed source files', () => {
	const files: [string, string][] = [];
	for (const root of ['frontend/src', 'mobile/src']) {
		for (const path of sourceFiles(join(repo, root))) {
			files.push([relative(repo, path).split('\\').join('/'), readFileSync(path, 'utf8')]);
		}
	}
	expect(offenders(files)).toEqual([]);
});

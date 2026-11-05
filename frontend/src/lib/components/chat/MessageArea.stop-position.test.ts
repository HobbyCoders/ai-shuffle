/** The app positions live-edge followers itself. Native anchoring must not
 * remember a composer's temporary clamp and restore it when Stop adds rows.
 * The strict stop-holds-the-reader journey measures the actual painted row. */
import { expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const source = readFileSync(resolve(process.cwd(), 'src/lib/components/chat/MessageArea.svelte'), 'utf8');
const binding = source.match(/style:overflow-anchor=\{([^}]+)\}/)?.[1];

it.each([[true, 'none'], [false, 'auto']])('gives native anchoring the correct owner when following=%s', (following, expected) => {
	expect(binding, 'the transcript must bind anchoring to its follow owner').toBeDefined();
	const anchoring = new Function('shouldAutoScroll', `return (${binding});`);
	expect(anchoring(following)).toBe(expected);
});

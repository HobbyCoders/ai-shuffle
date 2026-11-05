/** The app positions live-edge followers itself. Native anchoring must not
 * remember a composer's temporary clamp and restore it when Stop adds rows.
 * The strict stop-holds-the-reader journey measures the actual painted row. */
import { expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { restingOnNotSentRows } from '../../utils/not-sent-hold';

const source = readFileSync(resolve(process.cwd(), 'src/lib/components/chat/MessageArea.svelte'), 'utf8');
const binding = source.match(/style:overflow-anchor=\{([^}]+)\}/)?.[1];

it.each([[true, 'none'], [false, 'auto']])('gives native anchoring the correct owner when following=%s', (following, expected) => {
	expect(binding, 'the transcript must bind anchoring to its follow owner').toBeDefined();
	const anchoring = new Function('shouldAutoScroll', `return (${binding});`);
	expect(anchoring(following)).toBe(expected);
});

const followStart = source.indexOf('function followGrowthNow() {');
const followBody = source.slice(followStart, source.indexOf('async function pinBelowCurrentReserve()', followStart));
const stopped = { role: 'user', metadata: { not_sent: true } };
const fresh = { role: 'user', metadata: {} };

function followWrites(options: Record<string, unknown>) {
	const env = { following: true, anchor: null, error: null, selected: false, messages: [stopped], ...options };
	return new Function('restingOnNotSentRows', 'env', `
		const containerRef = { scrollTop: 3279, clientHeight: 641 }, tailSpacerRef = {};
		const shouldAutoScroll = env.following, currentTurnAnchorId = env.anchor, selectionBlocksNavigation = env.selected;
		const notSentHold = { messages: env.messages }, $activeSession = { error: env.error };
		const renderedFollowEnd = () => 4156;
		const resolveAnchoredRevealDelta = (top, height, end) => Math.max(0, end - top - height);
		const writes = [], setProgrammaticScrollTop = value => writes.push(value);
		${followBody}
		followGrowthNow();
		return writes;
	`)(restingOnNotSentRows, env);
}

it.each([
	['stopped follower', {}, []],
	['stopped current-turn anchor', { anchor: 'turn' }, []],
	['visible error follow', { error: 'API Error: 500' }, [3515]],
	['fresh stream after older Not-sent history', { messages: [stopped, fresh] }, [3515]],
	['ordinary live growth', { messages: [fresh] }, [3515]],
	['scrolled-up reader', { following: false, messages: [fresh] }, []],
	['native text selection', { selected: true, messages: [fresh] }, []]
] as const)('gives automatic overflow ownership to the %s', (_name, options, expected) => {
	expect(followWrites(options)).toEqual(expected);
});

it('routes boundary, height and mutation growth through the same follow guard', () => {
	const boundary = source.slice(source.indexOf("let layoutBoundary = '';"), source.indexOf('// Rendered geometry has one lifecycle'));
	const mutation = source.slice(source.indexOf('const observer = new MutationObserver('), source.indexOf('// Re-pin to the bottom once a late-loading'));
	expect(boundary).toContain('followGrowthNow();');
	expect(mutation).toContain('followGrowthNow();');
	expect(source).toContain('if (shouldAutoScroll) followGrowthNow();');
});

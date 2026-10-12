/**
 * #1282: streaming must not re-evaluate every historic transcript row.
 *
 * Each streaming fold publishes a new `$activeSession` object and a new
 * `transcript` model object. A row prop that reads either one directly is
 * re-evaluated in EVERY mounted row on EVERY fold; on a 900-turn session that
 * was each AssistantMessage re-routing its rendered HTML and each user row
 * re-running its actions, roughly 200 times per streamed reply. Rows read
 * values that only change when the value itself changes instead.
 *
 * Mounting MessageArea pulls in the whole session graph (see
 * MessageArea.a11y.test.ts), so the row template is pinned at the source level.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const source = readFileSync(resolve(process.cwd(), 'src/lib/components/chat/MessageArea.svelte'), 'utf8');

function between(start: string, end: string): string {
	const from = source.indexOf(start);
	const to = source.indexOf(end, from);
	expect(from).toBeGreaterThan(-1);
	expect(to).toBeGreaterThan(from);
	return source.slice(from, to);
}

describe('MessageArea per-row props', () => {
	const rowLoop = between('{#each visibleItems as item', '{/each}');
	const rowBody = between('{#snippet messageBody(', '{/snippet}');

	it('never reads the per-fold session store or transcript model inside a row', () => {
		for (const row of [rowLoop, rowBody]) {
			expect(row).not.toMatch(/\$activeSession\b/);
			expect(row).not.toMatch(/\btranscript\./);
		}
	});

	it('passes the session id from a store that only emits when the id changes', () => {
		expect(rowBody).toContain('sessionId={activeSessionId}');
		expect(source).toMatch(/const activeSessionIdStore = derivedStore\(activeSession, \(\$session\) => \$session\.sessionId\);/);
		expect(source).toContain('const activeSessionId = $derived($activeSessionIdStore);');
	});

	it('reads rewind bookkeeping through deriveds that keep identity on the streaming fast path', () => {
		expect(rowBody).toContain('rewindEligible={rewindEligible[messageIndex]}');
		expect(source).toContain('const promptOccurrence = $derived(transcript.promptOccurrence);');
		expect(source).toContain('const rewindEligible = $derived(transcript.rewindEligible);');
		expect(source).toMatch(/function userPromptOccurrence\(messageIndex: number\): number \{\s*return promptOccurrence\[messageIndex\] \?\? 0;/);
	});
});

describe('MessageArea hand-off row', () => {
	it('renders a hand-off as one card on its anchor, read through a derived that keeps identity', () => {
		const rowBody = between('{#snippet messageBody(', '{/snippet}');
		expect(rowBody).toContain('{@const handoff = handoffAnchors.get(message)}');
		expect(rowBody).toContain('<HandoffCard episode={handoff} />');
		expect(source).toMatch(/const handoffAnchors = \$derived\(\s*settleByOwner\(transcript\.handoffs, \{ deviceId: handoffOwnerId, deviceName: handoffOwnerName \}, openSessionId, handoffPendingView\)\.byAnchor\s*\);/);
	});
});

describe('MessageArea settle hold', () => {
	it('groups the transcript with the held lifecycle, not the raw signals', () => {
		expect(source).toContain("import { createSettleHold, createTurnHandoffHold } from '$lib/utils/settle-hold';");
		// A send during the hold opens a new turn; the interrupted turn keeps
		// its hold through the hand-off set instead of folding at once.
		expect(source).toContain('onDestroy(() => turnHandoff.dispose());');
		expect(source).toContain('heldTurnKeys: heldKeys,');
		expect(source).toMatch(/const latestTurnUnsettled = \$derived\.by\(\(\) => \{\s*void settleHoldTick;\s*return settleHold\.update\(\{/);
		expect(source).toContain('unsettled: rawLatestTurnUnsettled,');
		expect(source).toContain("parked: $activeSession.turnParked === true");
		expect(source).toContain('const latestIsUnsettled = latestTurnUnsettled;');
		expect(source).toContain('onDestroy(() => settleHold.dispose());');
	});
});

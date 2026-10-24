/**
 * #524: MessageArea is where the store's unconfirmed-queue flag reaches the
 * pending bubbles and the announcer. Mounting MessageArea pulls in the whole
 * session graph, so this pins the wiring; behaviour is covered by
 * PendingMessages.test.ts, PendingStatusAnnouncer.test.ts and
 * stores/session/queue-switch-restore.test.ts. The mobile shell renders this
 * same MessageArea (mobile/ compiles ../frontend/src/lib).
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const source = readFileSync(resolve(process.cwd(), 'src/lib/components/chat/MessageArea.svelte'), 'utf8');

describe('MessageArea pending-queue wiring (#524)', () => {
	it('passes the stale flag and the remote owner to the pending bubbles', () => {
		const block = source.slice(source.indexOf('<PendingMessages'), source.indexOf('/>', source.indexOf('<PendingMessages')));
		expect(block).toContain('stale={!!$activeSession.queueStale}');
		expect(block).toContain('ownerName={$activeSession.isRemote ? $activeSession.ownerDeviceName : null}');
	});

	it('announces the stale transition through the out-of-log announcer', () => {
		expect(source).toMatch(/<PendingStatusAnnouncer[^>]*stale=\{!!\$activeSession\.queueStale\}/);
	});
});

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
import { transpile } from 'typescript';
import { createQueuedPromptTracker } from '$lib/stores/session/auto-scroll';

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

	it('projects peer queues into the keyed transcript before grouping instead of a separate tail', () => {
		expect(source).toContain('const agentMessageProjection = createAgentMessageProjection();');
		expect(source).toContain('pendingPeerMessages: $activeSession.pendingPeerMessages ?? []');
		expect(source).toContain('const messages = projectedMessages;');
		expect(source).toContain('const model = buildTranscript(messages, {');
		expect(source).not.toContain('PendingPeerMessages');
	});

	it('follows a new send after Stop, without following older queued prompts or another chat', () => {
		// Execute the component's queue tracking and turn decision, not a second copy.
		const tracking = source.slice(source.indexOf('const queuedPrompts ='), source.indexOf('// Watch for message changes and streaming content updates'));
		const decision = source.slice(source.indexOf('const { fromQueue, followsStop } ='), source.indexOf('const newUserCommand ='));
		const release = source.slice(source.indexOf('function disengageAutoScroll()'), source.indexOf('// Wheel/trackpad:'));
		const make = new Function('createQueuedPromptTracker', transpile(`
			let $activeSession; let pre;
			let shouldAutoScroll = false, pendingScrollToBottom = false;
			const cancelCurrentTurnAnchor = () => {};
			${release}
			const $effect = { pre: (callback) => { pre = callback; } };
			${tracking}
			return {
				release: disengageAutoScroll,
				update: (state) => { $activeSession = state; pre(); },
				follows: (content, shouldAutoScroll = false, commandId = undefined, turnId = content) => {
					const hasNewMessage = true;
					const newestMessage = { role: 'user', content, commandId, turnId };
					const isNavigableUserTurn = () => true;
					${decision}
					return newUserMessage;
				}
			};
		`));
		const trackingState = make(createQueuedPromptTracker);
		const update = (sessionId: string, prompts: string[], stopRequestedTurnId: string | null = null) => trackingState.update({ sessionId, queuedMessages: prompts.map(content => ({ id: content, content })), stopRequestedTurnId });
		update('chat-a', []);
		update('chat-a', ['older queued prompt']);
		update('chat-a', ['older queued prompt', 'fresh after Stop'], 'turn-a');
		update('chat-a', []);
		expect(trackingState.follows('older queued prompt')).toBe(false);
		expect(trackingState.follows('fresh after Stop')).toBe(true);
		update('chat-a', ['send then read earlier'], 'turn-b');
		trackingState.release();
		expect(trackingState.follows('send then read earlier')).toBe(false);
		update('chat-a', ['another send'], 'turn-b');
		update('chat-b', ['another send'], 'turn-c');
		expect(trackingState.follows('another send')).toBe(false);

		// A withdrawn prompt's words, sent again with no queue step, are a new send.
		const withdrawn = (clientMessageId: string, stop: string | null) => trackingState.update({ sessionId: 'chat-c', stopRequestedTurnId: stop, queuedMessages: clientMessageId ? [{ id: clientMessageId, clientMessageId, content: 'Continue' }] : [] });
		withdrawn('', null);
		withdrawn('send-1', null);
		withdrawn('', null);
		expect(trackingState.follows('Continue', false, 'send-2', 'turn-2')).toBe(true);
		// The withdrawn entry itself, delivered after all, still does not pull the reader.
		expect(trackingState.follows('Continue', false, 'send-1', 'turn-1')).toBe(false);
	});

	it('uses one AgentMessage dispatch for queued and received peer rows', () => {
		const block = source.slice(source.indexOf('{:else if delivery}'), source.indexOf("{:else if message.type === 'command'}"));
		expect(block).toContain('<AgentMessage');
		expect(block).toContain('sender={sender.name}');
		expect(block).toContain('content={delivery.content}');
		expect(block).toContain("queued={message.systemData?.delivery === 'queued'}");
		expect(block).not.toContain('<ToolUseMessage');
	});
});

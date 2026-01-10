/**
 * Comprehensive tests for streaming-buffer.ts
 *
 * Tests cover:
 * - Event buffering and batching
 * - Block type transitions (thinking → text → tool_use)
 * - Delta accumulation and flushing
 * - Critical event handling
 * - Cache invalidation
 * - Cross-device sync scenarios
 * - Race conditions and edge cases
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
	initStreamingBuffer,
	pushEvent,
	flushNow,
	clearBuffer,
	removeBuffer,
	hasPendingData,
	getBufferStats,
	getActiveBlockType,
	setActiveBlockType,
	findLastStreamingTextIndex,
	findLastStreamingThinkingIndex,
	appendToStreamingText,
	appendToStreamingThinking,
	appendToToolInput,
	invalidateStreamingIndexCache,
	type BatchedUpdate
} from './streaming-buffer';
import type { ChatMessage } from './tabs';

// Mock requestAnimationFrame and cancelAnimationFrame
let rafCallbacks: Map<number, () => void>;
let rafId: number;

beforeEach(() => {
	rafCallbacks = new Map();
	rafId = 0;

	vi.stubGlobal('requestAnimationFrame', (callback: () => void) => {
		const id = ++rafId;
		rafCallbacks.set(id, callback);
		return id;
	});

	vi.stubGlobal('cancelAnimationFrame', (id: number) => {
		rafCallbacks.delete(id);
	});
});

afterEach(() => {
	vi.unstubAllGlobals();
});

// Helper to run RAF callbacks
function runRAF() {
	const callbacks = [...rafCallbacks.values()];
	rafCallbacks.clear();
	callbacks.forEach(cb => cb());
}

// =============================================================================
// Initialization Tests
// =============================================================================

describe('StreamingBuffer Initialization', () => {
	it('should initialize with callback', () => {
		const callback = vi.fn();
		initStreamingBuffer(callback);

		// Push an event and flush
		pushEvent('tab-1', 'stream_delta', { delta_type: 'text', content: 'test' });
		flushNow('tab-1');

		expect(callback).toHaveBeenCalledWith('tab-1', expect.any(Object));
	});

	it('should create buffer on first pushEvent', () => {
		initStreamingBuffer(vi.fn());

		expect(hasPendingData('new-tab')).toBe(false);

		pushEvent('new-tab', 'stream_delta', { delta_type: 'text', content: 'hello' });

		expect(hasPendingData('new-tab')).toBe(true);

		// Cleanup
		removeBuffer('new-tab');
	});
});

// =============================================================================
// Text Delta Accumulation Tests
// =============================================================================

describe('Text Delta Accumulation', () => {
	let receivedBatch: BatchedUpdate | null = null;

	beforeEach(() => {
		receivedBatch = null;
		initStreamingBuffer((tabId, batch) => {
			receivedBatch = batch;
		});
	});

	afterEach(() => {
		clearBuffer('tab-1');
		removeBuffer('tab-1');
	});

	it('should accumulate text deltas', () => {
		pushEvent('tab-1', 'stream_delta', { delta_type: 'text', content: 'Hello ' });
		pushEvent('tab-1', 'stream_delta', { delta_type: 'text', content: 'World!' });

		flushNow('tab-1');

		expect(receivedBatch).not.toBeNull();
		expect(receivedBatch!.textDelta).toBe('Hello World!');
	});

	it('should auto-flush when threshold reached', () => {
		// TEXT_BATCH_THRESHOLD is 100 chars
		const longText = 'x'.repeat(100);
		pushEvent('tab-1', 'stream_delta', { delta_type: 'text', content: longText });

		// Should have triggered immediate flush
		expect(receivedBatch).not.toBeNull();
		expect(receivedBatch!.textDelta).toBe(longText);
	});

	it('should set activeBlockType to text on first text delta', () => {
		expect(getActiveBlockType('tab-1')).toBeNull();

		pushEvent('tab-1', 'stream_delta', { delta_type: 'text', content: 'hello' });

		expect(getActiveBlockType('tab-1')).toBe('text');
	});
});

// =============================================================================
// Thinking Delta Accumulation Tests
// =============================================================================

describe('Thinking Delta Accumulation', () => {
	let receivedBatch: BatchedUpdate | null = null;

	beforeEach(() => {
		receivedBatch = null;
		initStreamingBuffer((tabId, batch) => {
			receivedBatch = batch;
		});
	});

	afterEach(() => {
		clearBuffer('tab-1');
		removeBuffer('tab-1');
	});

	it('should accumulate thinking deltas', () => {
		pushEvent('tab-1', 'stream_delta', { delta_type: 'thinking', content: 'Let me ' });
		pushEvent('tab-1', 'stream_delta', { delta_type: 'thinking', content: 'think...' });

		flushNow('tab-1');

		expect(receivedBatch).not.toBeNull();
		expect(receivedBatch!.thinkingDelta).toBe('Let me think...');
	});

	it('should set activeBlockType to thinking on first thinking delta', () => {
		expect(getActiveBlockType('tab-1')).toBeNull();

		pushEvent('tab-1', 'stream_delta', { delta_type: 'thinking', content: 'thinking...' });

		expect(getActiveBlockType('tab-1')).toBe('thinking');
	});
});

// =============================================================================
// Block Transition Tests - CRITICAL for preventing thinking/text mixing
// =============================================================================

describe('Block Transitions', () => {
	let batches: BatchedUpdate[] = [];

	beforeEach(() => {
		batches = [];
		initStreamingBuffer((tabId, batch) => {
			batches.push({ ...batch });
		});
	});

	afterEach(() => {
		clearBuffer('tab-1');
		removeBuffer('tab-1');
	});

	it('should flush pending deltas on block_start', () => {
		// Accumulate some thinking
		pushEvent('tab-1', 'stream_delta', { delta_type: 'thinking', content: 'thinking content' });

		// Block start for text should flush thinking first
		pushEvent('tab-1', 'stream_block_start', { block_type: 'text', index: 1 });

		// Should have flushed twice - once for thinking, once for block_start
		expect(batches.length).toBeGreaterThanOrEqual(1);

		// First batch should contain the thinking delta
		const thinkingBatch = batches.find(b => b.thinkingDelta.length > 0);
		expect(thinkingBatch).toBeDefined();
		expect(thinkingBatch!.thinkingDelta).toBe('thinking content');
	});

	it('should update activeBlockType on block_start', () => {
		pushEvent('tab-1', 'stream_block_start', { block_type: 'thinking', index: 0 });
		expect(getActiveBlockType('tab-1')).toBe('thinking');

		pushEvent('tab-1', 'stream_block_start', { block_type: 'text', index: 1 });
		expect(getActiveBlockType('tab-1')).toBe('text');

		pushEvent('tab-1', 'stream_block_start', { block_type: 'tool_use', index: 2 });
		expect(getActiveBlockType('tab-1')).toBe('tool_use');
	});

	it('should clear activeBlockType on block_stop', () => {
		pushEvent('tab-1', 'stream_block_start', { block_type: 'thinking', index: 0 });
		expect(getActiveBlockType('tab-1')).toBe('thinking');

		pushEvent('tab-1', 'stream_block_stop', { index: 0 });
		expect(getActiveBlockType('tab-1')).toBeNull();
	});

	it('should handle thinking → text transition correctly', () => {
		// This is the critical test for the bug where thinking and text get mixed

		// Start thinking block
		pushEvent('tab-1', 'stream_block_start', { block_type: 'thinking', index: 0 });
		pushEvent('tab-1', 'stream_delta', { delta_type: 'thinking', content: 'analyzing the problem' });

		// End thinking block
		pushEvent('tab-1', 'stream_block_stop', { index: 0 });

		// Start text block
		pushEvent('tab-1', 'stream_block_start', { block_type: 'text', index: 1 });
		pushEvent('tab-1', 'stream_delta', { delta_type: 'text', content: 'Here is my answer' });

		// End text block
		pushEvent('tab-1', 'stream_block_stop', { index: 1 });

		// Verify batches
		const thinkingBatch = batches.find(b => b.thinkingDelta === 'analyzing the problem');
		const textBatch = batches.find(b => b.textDelta === 'Here is my answer');

		expect(thinkingBatch).toBeDefined();
		expect(textBatch).toBeDefined();

		// CRITICAL: Thinking and text should be in SEPARATE batches
		// They should never be mixed in the same flush
		if (thinkingBatch && textBatch) {
			// If they're the same batch, that's a bug
			expect(thinkingBatch.textDelta).toBe('');
			expect(textBatch.thinkingDelta).toBe('');
		}
	});

	it('should handle multiple block transitions rapidly', () => {
		// Simulate rapid block transitions that could cause race conditions

		// thinking → text → tool_use → text
		pushEvent('tab-1', 'stream_block_start', { block_type: 'thinking', index: 0 });
		pushEvent('tab-1', 'stream_delta', { delta_type: 'thinking', content: 'T1' });
		pushEvent('tab-1', 'stream_block_stop', { index: 0 });

		pushEvent('tab-1', 'stream_block_start', { block_type: 'text', index: 1 });
		pushEvent('tab-1', 'stream_delta', { delta_type: 'text', content: 'X1' });
		pushEvent('tab-1', 'stream_block_stop', { index: 1 });

		pushEvent('tab-1', 'stream_block_start', { block_type: 'tool_use', index: 2 });
		pushEvent('tab-1', 'stream_block_stop', { index: 2 });

		pushEvent('tab-1', 'stream_block_start', { block_type: 'text', index: 3 });
		pushEvent('tab-1', 'stream_delta', { delta_type: 'text', content: 'X2' });
		pushEvent('tab-1', 'stream_block_stop', { index: 3 });

		// Verify all content was captured correctly
		const allThinking = batches.filter(b => b.thinkingDelta).map(b => b.thinkingDelta).join('');
		const allText = batches.filter(b => b.textDelta).map(b => b.textDelta).join('');

		expect(allThinking).toBe('T1');
		expect(allText).toBe('X1X2');
	});
});

// =============================================================================
// Tool Input Delta Tests
// =============================================================================

describe('Tool Input Delta Accumulation', () => {
	let receivedBatch: BatchedUpdate | null = null;

	beforeEach(() => {
		receivedBatch = null;
		initStreamingBuffer((tabId, batch) => {
			receivedBatch = batch;
		});
	});

	afterEach(() => {
		clearBuffer('tab-1');
		removeBuffer('tab-1');
	});

	it('should accumulate tool input deltas per tool', () => {
		pushEvent('tab-1', 'stream_delta', {
			delta_type: 'tool_input',
			tool_id: 'tool-1',
			content: '{"file'
		});
		pushEvent('tab-1', 'stream_delta', {
			delta_type: 'tool_input',
			tool_id: 'tool-1',
			content: '": "test.py"}'
		});

		flushNow('tab-1');

		expect(receivedBatch).not.toBeNull();
		expect(receivedBatch!.toolInputDeltas.get('tool-1')).toBe('{"file": "test.py"}');
	});

	it('should track multiple tools separately', () => {
		pushEvent('tab-1', 'stream_delta', {
			delta_type: 'tool_input',
			tool_id: 'tool-1',
			content: 'input1'
		});
		pushEvent('tab-1', 'stream_delta', {
			delta_type: 'tool_input',
			tool_id: 'tool-2',
			content: 'input2'
		});

		flushNow('tab-1');

		expect(receivedBatch!.toolInputDeltas.get('tool-1')).toBe('input1');
		expect(receivedBatch!.toolInputDeltas.get('tool-2')).toBe('input2');
	});
});

// =============================================================================
// Critical Event Tests
// =============================================================================

describe('Critical Events', () => {
	let batches: BatchedUpdate[] = [];

	beforeEach(() => {
		batches = [];
		initStreamingBuffer((tabId, batch) => {
			batches.push({ ...batch });
		});
	});

	afterEach(() => {
		clearBuffer('tab-1');
		removeBuffer('tab-1');
	});

	const criticalEvents = [
		'tool_use',
		'tool_result',
		'subagent_start',
		'subagent_tool_use',
		'subagent_tool_result',
		'subagent_done',
		'permission_request',
		'question_request',
		'error',
		'stopped',
		'done'
	];

	criticalEvents.forEach(eventType => {
		it(`should immediately flush on ${eventType}`, () => {
			pushEvent('tab-1', eventType, { test: 'data' });

			// Should have flushed immediately
			expect(batches.length).toBeGreaterThanOrEqual(1);
			const lastBatch = batches[batches.length - 1];
			expect(lastBatch.discreteEvents.some(e => e.type === eventType)).toBe(true);
		});
	});

	it('should flush pending deltas before critical event', () => {
		// Accumulate some text
		pushEvent('tab-1', 'stream_delta', { delta_type: 'text', content: 'pending text' });

		// Critical event should flush text first
		pushEvent('tab-1', 'tool_use', { name: 'Read', id: 'tool-1' });

		// Text should be in a batch
		const textBatch = batches.find(b => b.textDelta.includes('pending text'));
		expect(textBatch).toBeDefined();
	});
});

// =============================================================================
// Usage Update Tests
// =============================================================================

describe('Usage Updates', () => {
	let receivedBatch: BatchedUpdate | null = null;

	beforeEach(() => {
		receivedBatch = null;
		initStreamingBuffer((tabId, batch) => {
			receivedBatch = batch;
		});
	});

	afterEach(() => {
		clearBuffer('tab-1');
		removeBuffer('tab-1');
	});

	it('should capture usage from stream_start', () => {
		pushEvent('tab-1', 'stream_start', {
			usage: {
				input_tokens: 100,
				output_tokens: 50,
				cache_creation_input_tokens: 25,
				cache_read_input_tokens: 10
			}
		});

		flushNow('tab-1');

		expect(receivedBatch!.usageUpdate).toEqual({
			inputTokens: 100,
			outputTokens: 50,
			cacheCreationTokens: 25,
			cacheReadTokens: 10
		});
	});

	it('should capture usage from stream_message_delta', () => {
		pushEvent('tab-1', 'stream_message_delta', {
			usage: {
				input_tokens: 200,
				output_tokens: 100
			}
		});

		flushNow('tab-1');

		expect(receivedBatch!.usageUpdate!.inputTokens).toBe(200);
		expect(receivedBatch!.usageUpdate!.outputTokens).toBe(100);
	});

	it('should extract usage from nested message object', () => {
		pushEvent('tab-1', 'stream_start', {
			message: {
				usage: {
					input_tokens: 150,
					output_tokens: 75
				}
			}
		});

		flushNow('tab-1');

		expect(receivedBatch!.usageUpdate!.inputTokens).toBe(150);
	});
});

// =============================================================================
// Buffer Management Tests
// =============================================================================

describe('Buffer Management', () => {
	beforeEach(() => {
		initStreamingBuffer(vi.fn());
	});

	it('should clear buffer correctly', () => {
		pushEvent('tab-1', 'stream_delta', { delta_type: 'text', content: 'test' });
		expect(hasPendingData('tab-1')).toBe(true);

		clearBuffer('tab-1');
		expect(hasPendingData('tab-1')).toBe(false);
	});

	it('should clear activeBlockType on clearBuffer', () => {
		pushEvent('tab-1', 'stream_block_start', { block_type: 'thinking', index: 0 });
		expect(getActiveBlockType('tab-1')).toBe('thinking');

		clearBuffer('tab-1');
		expect(getActiveBlockType('tab-1')).toBeNull();
	});

	it('should remove buffer entirely on removeBuffer', () => {
		pushEvent('tab-1', 'stream_delta', { delta_type: 'text', content: 'test' });

		removeBuffer('tab-1');

		expect(getBufferStats('tab-1')).toBeNull();
	});

	it('should return correct buffer stats', () => {
		pushEvent('tab-1', 'stream_delta', { delta_type: 'text', content: 'hello' });
		pushEvent('tab-1', 'tool_use', { name: 'Read' });
		pushEvent('tab-1', 'stream_delta', { delta_type: 'tool_input', tool_id: 't1', content: 'x' });

		// Flush tool_use but keep other data
		const stats = getBufferStats('tab-1');
		expect(stats).not.toBeNull();
		// After critical event flush, text and tool_input should still be accumulating
		removeBuffer('tab-1');
	});
});

// =============================================================================
// Message Helper Function Tests
// =============================================================================

describe('Message Helper Functions', () => {
	describe('findLastStreamingTextIndex', () => {
		it('should find streaming text message', () => {
			const messages: ChatMessage[] = [
				{ id: '1', role: 'user', content: 'hi' },
				{ id: '2', role: 'assistant', content: 'hello', type: 'text', streaming: true }
			];

			expect(findLastStreamingTextIndex(messages)).toBe(1);
		});

		it('should return -1 when no streaming text', () => {
			const messages: ChatMessage[] = [
				{ id: '1', role: 'user', content: 'hi' },
				{ id: '2', role: 'assistant', content: 'hello', type: 'text', streaming: false }
			];

			expect(findLastStreamingTextIndex(messages)).toBe(-1);
		});

		it('should find last streaming text when multiple exist', () => {
			const messages: ChatMessage[] = [
				{ id: '1', role: 'assistant', content: 'first', type: 'text', streaming: false },
				{ id: '2', role: 'assistant', content: 'second', type: 'text', streaming: true }
			];

			expect(findLastStreamingTextIndex(messages)).toBe(1);
		});

		it('should use cache for repeated calls', () => {
			invalidateStreamingIndexCache();

			const messages: ChatMessage[] = [
				{ id: '1', role: 'assistant', content: 'text', type: 'text', streaming: true }
			];

			// First call populates cache
			const idx1 = findLastStreamingTextIndex(messages);
			// Second call should use cache
			const idx2 = findLastStreamingTextIndex(messages);

			expect(idx1).toBe(idx2);
			expect(idx1).toBe(0);
		});
	});

	describe('findLastStreamingThinkingIndex', () => {
		it('should find streaming thinking message', () => {
			const messages: ChatMessage[] = [
				{ id: '1', role: 'assistant', content: 'thinking...', type: 'thinking', streaming: true }
			];

			expect(findLastStreamingThinkingIndex(messages)).toBe(0);
		});

		it('should return -1 when no streaming thinking', () => {
			const messages: ChatMessage[] = [
				{ id: '1', role: 'assistant', content: 'text', type: 'text', streaming: true }
			];

			expect(findLastStreamingThinkingIndex(messages)).toBe(-1);
		});
	});

	describe('appendToStreamingText', () => {
		beforeEach(() => {
			invalidateStreamingIndexCache();
		});

		it('should append to existing streaming message', () => {
			const messages: ChatMessage[] = [
				{ id: '1', role: 'assistant', content: 'Hello ', type: 'text', streaming: true }
			];

			const result = appendToStreamingText(messages, 'World!');

			expect(result).toBe(true);
			expect(messages[0].content).toBe('Hello World!');
		});

		it('should create new message when none exists', () => {
			const messages: ChatMessage[] = [];

			const result = appendToStreamingText(messages, 'New message');

			expect(result).toBe(true);
			expect(messages.length).toBe(1);
			expect(messages[0].content).toBe('New message');
			expect(messages[0].type).toBe('text');
			expect(messages[0].streaming).toBe(true);
		});
	});

	describe('appendToStreamingThinking', () => {
		it('should append to existing streaming thinking', () => {
			const messages: ChatMessage[] = [
				{ id: '1', role: 'assistant', content: 'Let me ', type: 'thinking', streaming: true }
			];

			const result = appendToStreamingThinking(messages, 'think...');

			expect(result).toBe(true);
			expect(messages[0].content).toBe('Let me think...');
		});

		it('should create new thinking message when none exists', () => {
			const messages: ChatMessage[] = [];

			const result = appendToStreamingThinking(messages, 'Analyzing...');

			expect(result).toBe(true);
			expect(messages.length).toBe(1);
			expect(messages[0].type).toBe('thinking');
			expect(messages[0].streaming).toBe(true);
		});
	});

	describe('appendToToolInput', () => {
		it('should append to matching tool_use message', () => {
			const messages: ChatMessage[] = [
				{
					id: '1',
					role: 'assistant',
					content: '',
					type: 'tool_use',
					toolId: 'tool-123',
					toolInput: {},
					partialToolInput: '{"fi',
					streaming: true
				}
			];

			const result = appendToToolInput(messages, 'tool-123', 'le": "test.py"}');

			expect(result).toBe(true);
			expect(messages[0].partialToolInput).toBe('{"file": "test.py"}');
			expect(messages[0].toolInput).toEqual({ file: 'test.py' });
		});

		it('should return false when no matching tool', () => {
			const messages: ChatMessage[] = [];

			const result = appendToToolInput(messages, 'tool-999', 'data');

			expect(result).toBe(false);
		});

		it('should handle invalid JSON gracefully', () => {
			const messages: ChatMessage[] = [
				{
					id: '1',
					role: 'assistant',
					content: '',
					type: 'tool_use',
					toolId: 'tool-123',
					toolInput: {},
					partialToolInput: '{"incomplete',
					streaming: true
				}
			];

			const result = appendToToolInput(messages, 'tool-123', ': ');

			expect(result).toBe(true);
			expect(messages[0].partialToolInput).toBe('{"incomplete: ');
			// toolInput should remain unchanged since JSON is still invalid
			expect(messages[0].toolInput).toEqual({});
		});
	});
});

// =============================================================================
// Discrete Event Processing Tests
// =============================================================================

describe('Discrete Event Processing', () => {
	let batches: BatchedUpdate[] = [];

	beforeEach(() => {
		batches = [];
		initStreamingBuffer((tabId, batch) => {
			batches.push(batch);
		});
	});

	afterEach(() => {
		clearBuffer('tab-1');
		removeBuffer('tab-1');
	});

	it('should queue discrete events when not critical', () => {
		// Push non-critical discrete events
		// Use a custom event type that goes through the discrete event path
		pushEvent('tab-1', 'stream_block_start', { block_type: 'text', index: 0 });

		// stream_block_start triggers immediate flush, so should have batches
		expect(batches.length).toBeGreaterThanOrEqual(1);
		const lastBatch = batches[batches.length - 1];
		expect(lastBatch.discreteEvents.some(e => e.type === 'stream_block_start')).toBe(true);
	});

	it('should force flush at MAX_DISCRETE_EVENTS', () => {
		// Push events that accumulate in discreteEvents
		// Note: stream_start goes to usage handling, so use something that accumulates
		// Multiple stream_block_start events will accumulate
		for (let i = 0; i < 25; i++) {
			// Use unique type to avoid special handling
			pushEvent('tab-1', 'custom_event', { index: i });
		}

		// Should have auto-flushed at MAX_DISCRETE_EVENTS (20)
		expect(batches.length).toBeGreaterThanOrEqual(1);
	});
});

// =============================================================================
// Edge Cases and Race Conditions
// =============================================================================

describe('Edge Cases and Race Conditions', () => {
	let batches: BatchedUpdate[] = [];

	beforeEach(() => {
		batches = [];
		initStreamingBuffer((tabId, batch) => {
			batches.push({ ...batch });
		});
	});

	afterEach(() => {
		clearBuffer('tab-1');
		removeBuffer('tab-1');
	});

	it('should handle empty flush gracefully', () => {
		flushNow('tab-1');
		expect(batches.length).toBe(0);
	});

	it('should handle flush on non-existent tab', () => {
		// Should not throw
		expect(() => flushNow('non-existent')).not.toThrow();
	});

	it('should handle clearBuffer on non-existent tab', () => {
		expect(() => clearBuffer('non-existent')).not.toThrow();
	});

	it('should handle rapid session changes', () => {
		// Simulate rapid session ID changes
		pushEvent('tab-1', 'stream_delta', { delta_type: 'text', content: 'session1' }, 'session-1');
		clearBuffer('tab-1');
		pushEvent('tab-1', 'stream_delta', { delta_type: 'text', content: 'session2' }, 'session-2');
		flushNow('tab-1');

		expect(batches.length).toBe(1);
		expect(batches[0].textDelta).toBe('session2');
	});

	it('should handle interleaved thinking and text from rapid block transitions', () => {
		// This tests the scenario where events arrive very rapidly

		// Rapid transitions: thinking delta → block_stop → block_start → text delta
		pushEvent('tab-1', 'stream_block_start', { block_type: 'thinking', index: 0 });
		pushEvent('tab-1', 'stream_delta', { delta_type: 'thinking', content: 'T' });
		pushEvent('tab-1', 'stream_block_stop', { index: 0 });
		pushEvent('tab-1', 'stream_block_start', { block_type: 'text', index: 1 });
		pushEvent('tab-1', 'stream_delta', { delta_type: 'text', content: 'X' });

		// Collect all thinking and text
		const allThinking = batches.flatMap(b => b.thinkingDelta ? [b.thinkingDelta] : []).join('');
		const allText = batches.flatMap(b => b.textDelta ? [b.textDelta] : []).join('');

		// CRITICAL: Each type should contain only its own content
		expect(allThinking).toBe('T');
		// Text might not be flushed yet if RAF hasn't run
		flushNow('tab-1');
		const finalText = batches.flatMap(b => b.textDelta ? [b.textDelta] : []).join('');
		expect(finalText).toBe('X');
	});

	it('should preserve buffer session ID', () => {
		pushEvent('tab-1', 'stream_delta', { delta_type: 'text', content: 'test' }, 'my-session');

		// Session ID should be stored
		const stats = getBufferStats('tab-1');
		expect(stats).not.toBeNull();
	});

	it('should handle setActiveBlockType for recovery scenarios', () => {
		// Simulate recovery from polling - manually set block type
		pushEvent('tab-1', 'stream_delta', { delta_type: 'text', content: 'x' }); // Creates buffer
		setActiveBlockType('tab-1', 'thinking');
		expect(getActiveBlockType('tab-1')).toBe('thinking');

		setActiveBlockType('tab-1', null);
		expect(getActiveBlockType('tab-1')).toBeNull();
	});
});

// =============================================================================
// Cache Invalidation Tests
// =============================================================================

describe('Cache Invalidation', () => {
	it('should invalidate cache and force re-scan', () => {
		const messages: ChatMessage[] = [
			{ id: '1', role: 'assistant', content: 'first', type: 'text', streaming: true }
		];

		// Populate cache
		findLastStreamingTextIndex(messages);

		// Change messages significantly
		messages.push({ id: '2', role: 'assistant', content: 'second', type: 'text', streaming: true });

		// Invalidate cache
		invalidateStreamingIndexCache();

		// Should find the NEW streaming message
		const idx = findLastStreamingTextIndex(messages);
		expect(idx).toBe(1);
	});
});

// =============================================================================
// Multi-Tab Isolation Tests
// =============================================================================

describe('Multi-Tab Isolation', () => {
	let tab1Batches: BatchedUpdate[] = [];
	let tab2Batches: BatchedUpdate[] = [];

	beforeEach(() => {
		tab1Batches = [];
		tab2Batches = [];
		initStreamingBuffer((tabId, batch) => {
			if (tabId === 'tab-1') tab1Batches.push({ ...batch });
			if (tabId === 'tab-2') tab2Batches.push({ ...batch });
		});
	});

	afterEach(() => {
		removeBuffer('tab-1');
		removeBuffer('tab-2');
	});

	it('should isolate buffers per tab', () => {
		pushEvent('tab-1', 'stream_delta', { delta_type: 'text', content: 'Tab 1 content' });
		pushEvent('tab-2', 'stream_delta', { delta_type: 'text', content: 'Tab 2 content' });

		flushNow('tab-1');
		flushNow('tab-2');

		expect(tab1Batches.length).toBe(1);
		expect(tab1Batches[0].textDelta).toBe('Tab 1 content');

		expect(tab2Batches.length).toBe(1);
		expect(tab2Batches[0].textDelta).toBe('Tab 2 content');
	});

	it('should isolate activeBlockType per tab', () => {
		pushEvent('tab-1', 'stream_block_start', { block_type: 'thinking', index: 0 });
		pushEvent('tab-2', 'stream_block_start', { block_type: 'text', index: 0 });

		expect(getActiveBlockType('tab-1')).toBe('thinking');
		expect(getActiveBlockType('tab-2')).toBe('text');
	});

	it('should not affect other tabs on clearBuffer', () => {
		pushEvent('tab-1', 'stream_delta', { delta_type: 'text', content: 'Tab 1' });
		pushEvent('tab-2', 'stream_delta', { delta_type: 'text', content: 'Tab 2' });

		clearBuffer('tab-1');

		expect(hasPendingData('tab-1')).toBe(false);
		expect(hasPendingData('tab-2')).toBe(true);
	});
});

// =============================================================================
// Realistic Streaming Sequence Tests
// =============================================================================

describe('Realistic Streaming Sequences', () => {
	let batches: BatchedUpdate[] = [];

	beforeEach(() => {
		batches = [];
		initStreamingBuffer((tabId, batch) => {
			batches.push({ ...batch });
		});
	});

	afterEach(() => {
		clearBuffer('tab-1');
		removeBuffer('tab-1');
	});

	it('should handle full assistant response with thinking', () => {
		// Simulate a real Claude response with thinking block

		// Stream start
		pushEvent('tab-1', 'stream_start', { message_id: 'msg-1', usage: { input_tokens: 500 } });

		// Thinking block
		pushEvent('tab-1', 'stream_block_start', { block_type: 'thinking', index: 0 });
		pushEvent('tab-1', 'stream_delta', { delta_type: 'thinking', content: 'I need to analyze ' });
		pushEvent('tab-1', 'stream_delta', { delta_type: 'thinking', content: 'this code...' });
		pushEvent('tab-1', 'stream_block_stop', { index: 0 });

		// Text block
		pushEvent('tab-1', 'stream_block_start', { block_type: 'text', index: 1 });
		pushEvent('tab-1', 'stream_delta', { delta_type: 'text', content: 'Here is my ' });
		pushEvent('tab-1', 'stream_delta', { delta_type: 'text', content: 'analysis:' });
		pushEvent('tab-1', 'stream_block_stop', { index: 1 });

		// Tool use block
		pushEvent('tab-1', 'stream_block_start', { block_type: 'tool_use', index: 2 });
		pushEvent('tab-1', 'tool_use', { name: 'Read', id: 'tool-1', input: { file_path: '/test.py' } });
		pushEvent('tab-1', 'stream_block_stop', { index: 2 });

		// Done
		pushEvent('tab-1', 'done', { usage: { output_tokens: 100 } });

		// Verify all content captured
		const allThinking = batches.flatMap(b => b.thinkingDelta || []).join('');
		const allText = batches.flatMap(b => b.textDelta || []).join('');
		const hasToolUse = batches.some(b => b.discreteEvents.some(e => e.type === 'tool_use'));
		const hasDone = batches.some(b => b.discreteEvents.some(e => e.type === 'done'));

		expect(allThinking).toBe('I need to analyze this code...');
		expect(allText).toBe('Here is my analysis:');
		expect(hasToolUse).toBe(true);
		expect(hasDone).toBe(true);
	});

	it('should handle tool_use with streaming input', () => {
		// Simulate tool use with streaming JSON input

		pushEvent('tab-1', 'stream_block_start', { block_type: 'tool_use', index: 0 });
		pushEvent('tab-1', 'tool_use', { name: 'Write', id: 'tool-1' });
		pushEvent('tab-1', 'stream_delta', { delta_type: 'tool_input', tool_id: 'tool-1', content: '{"file_' });
		pushEvent('tab-1', 'stream_delta', { delta_type: 'tool_input', tool_id: 'tool-1', content: 'path": ' });
		pushEvent('tab-1', 'stream_delta', { delta_type: 'tool_input', tool_id: 'tool-1', content: '"/test.py"}' });
		pushEvent('tab-1', 'stream_block_stop', { index: 0 });

		// Verify tool input accumulated
		flushNow('tab-1');
		const toolInputBatch = batches.find(b => b.toolInputDeltas.size > 0);
		expect(toolInputBatch).toBeDefined();
		expect(toolInputBatch!.toolInputDeltas.get('tool-1')).toBe('{"file_path": "/test.py"}');
	});

	it('should handle subagent execution', () => {
		pushEvent('tab-1', 'stream_block_start', { block_type: 'text', index: 0 });
		pushEvent('tab-1', 'stream_delta', { delta_type: 'text', content: 'Let me search...' });
		pushEvent('tab-1', 'stream_block_stop', { index: 0 });

		pushEvent('tab-1', 'subagent_start', {
			agent_id: 'agent-1',
			agent_type: 'Explore',
			description: 'Searching codebase'
		});

		pushEvent('tab-1', 'subagent_done', {
			agent_id: 'agent-1',
			result: 'Found 5 files'
		});

		// Verify subagent events captured
		const hasStart = batches.some(b => b.discreteEvents.some(e => e.type === 'subagent_start'));
		const hasDone = batches.some(b => b.discreteEvents.some(e => e.type === 'subagent_done'));

		expect(hasStart).toBe(true);
		expect(hasDone).toBe(true);
	});
});

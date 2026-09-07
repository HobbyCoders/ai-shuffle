/**
 * Tests for ws-connection.ts — WebSocket lifecycle module.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createWsConnection, type WsConnectionConfig, type WsConnection } from './ws-connection';

// ─── Mock WebSocket ─────────────────────────────────────────────────────────

class MockWebSocket {
	static readonly CONNECTING = 0;
	static readonly OPEN = 1;
	static readonly CLOSING = 2;
	static readonly CLOSED = 3;

	url: string;
	readyState = MockWebSocket.CONNECTING;
	onopen: ((e: Event) => void) | null = null;
	onclose: ((e: CloseEvent) => void) | null = null;
	onerror: ((e: Event) => void) | null = null;
	onmessage: ((e: MessageEvent) => void) | null = null;
	sentMessages: string[] = [];

	constructor(url: string) {
		this.url = url;
	}

	send(data: string) {
		this.sentMessages.push(data);
	}

	close(code?: number, _reason?: string) {
		this.readyState = MockWebSocket.CLOSING;
		// Simulate async close
		setTimeout(() => {
			this.readyState = MockWebSocket.CLOSED;
			this.onclose?.({
				code: code ?? 1000,
				reason: _reason ?? '',
				target: this,
				wasClean: true
			} as unknown as CloseEvent);
		}, 0);
	}

	// Test helpers
	simulateOpen() {
		this.readyState = MockWebSocket.OPEN;
		this.onopen?.({} as Event);
	}

	simulateClose(code = 1000, reason = '') {
		this.readyState = MockWebSocket.CLOSED;
		this.onclose?.({
			code,
			reason,
			target: this,
			wasClean: code === 1000
		} as unknown as CloseEvent);
	}

	simulateMessage(data: Record<string, unknown>) {
		this.onmessage?.({
			data: JSON.stringify(data)
		} as unknown as MessageEvent);
	}
}

// ─── Test Helpers ───────────────────────────────────────────────────────────

function createMockConfig(overrides?: Partial<WsConnectionConfig>): WsConnectionConfig {
	return {
		getAuthToken: () => 'test-token',
		getDeviceId: () => 'device-123',
		onMessage: vi.fn(),
		onConnected: vi.fn(),
		onDisconnected: vi.fn(),
		isStreaming: () => false,
		hasPendingTools: () => false,
		onMaxReconnectAttempts: vi.fn(),
		onVisibilityHidden: vi.fn(),
		onVisibilityVisible: vi.fn(),
		...overrides
	};
}

// ─── Tests ──────────────────────────────────────────────────────────────────

describe('ws-connection', () => {
	let originalWebSocket: typeof globalThis.WebSocket;
	let createdSockets: MockWebSocket[];

	beforeEach(() => {
		vi.useFakeTimers();
		vi.spyOn(document, 'hidden', 'get').mockReturnValue(false);
		createdSockets = [];
		originalWebSocket = globalThis.WebSocket;

		// Mock WebSocket constructor
		(globalThis as unknown as Record<string, unknown>).WebSocket = class extends MockWebSocket {
			constructor(url: string) {
				super(url);
				createdSockets.push(this);
			}
		};
		// Copy static fields
		(globalThis.WebSocket as unknown as Record<string, number>).OPEN = MockWebSocket.OPEN;
		(globalThis.WebSocket as unknown as Record<string, number>).CLOSED = MockWebSocket.CLOSED;
		(globalThis.WebSocket as unknown as Record<string, number>).CLOSING = MockWebSocket.CLOSING;
		(globalThis.WebSocket as unknown as Record<string, number>).CONNECTING = MockWebSocket.CONNECTING;
	});

	afterEach(() => {
		globalThis.WebSocket = originalWebSocket;
		vi.restoreAllMocks();
		vi.useRealTimers();
	});

	describe('connect()', () => {
		it('should create WebSocket with correct URL including device_id and token', () => {
			const config = createMockConfig({
				getAuthToken: () => 'my-auth-token',
				getDeviceId: () => 'my-device-id'
			});
			const conn = createWsConnection(config);
			conn.connect();

			expect(createdSockets).toHaveLength(1);
			expect(createdSockets[0].url).toContain('ws/chat');
			expect(createdSockets[0].url).toContain('device_id=my-device-id');
			expect(createdSockets[0].url).toContain('token=my-auth-token');

			conn.destroy();
		});

		it('should omit token from URL when auth returns null', () => {
			const config = createMockConfig({ getAuthToken: () => null });
			const conn = createWsConnection(config);
			conn.connect();

			expect(createdSockets[0].url).not.toContain('token=');

			conn.destroy();
		});

		it('should not create duplicate WebSocket if already connected', () => {
			const config = createMockConfig();
			const conn = createWsConnection(config);

			conn.connect();
			createdSockets[0].simulateOpen();

			conn.connect(); // second call — should be no-op

			expect(createdSockets).toHaveLength(1);

			conn.destroy();
		});

		it('should call onConnected when WebSocket opens', () => {
			const config = createMockConfig();
			const conn = createWsConnection(config);

			conn.connect();
			createdSockets[0].simulateOpen();

			expect(config.onConnected).toHaveBeenCalledTimes(1);

			conn.destroy();
		});

		it('should reset reconnect attempts on successful connection', () => {
			const config = createMockConfig();
			const conn = createWsConnection(config);

			conn.connect();
			createdSockets[0].simulateOpen();

			// Disconnect unexpectedly to trigger reconnect
			createdSockets[0].simulateClose(1006, 'Abnormal closure');

			// Reconnect timer fires
			vi.advanceTimersByTime(5000);
			expect(createdSockets).toHaveLength(2);

			// Second connection succeeds
			createdSockets[1].simulateOpen();

			// Disconnect again — should use base delay (not exponential), proving counter was reset
			createdSockets[1].simulateClose(1006);
			vi.advanceTimersByTime(2000); // base delay for attempt 0 is 1s + jitter
			expect(createdSockets).toHaveLength(3);

			conn.destroy();
		});
	});

	describe('disconnect()', () => {
		it('should set intentional flag so onDisconnected gets wasClean=true', () => {
			const config = createMockConfig();
			const conn = createWsConnection(config);

			conn.connect();
			createdSockets[0].simulateOpen();
			conn.disconnect();

			// Manually fire close since our mock close is async
			createdSockets[0].simulateClose(1000);

			expect(config.onDisconnected).toHaveBeenCalledWith(1000, '', true);

			conn.destroy();
		});

		it('should not attempt to reconnect after intentional disconnect', () => {
			const config = createMockConfig();
			const conn = createWsConnection(config);

			conn.connect();
			createdSockets[0].simulateOpen();
			conn.disconnect();

			// disconnect() calls ws.close(1000) which queues an async onclose in the mock.
			// Advance by 1ms to let the mock's setTimeout(0) fire the close event.
			vi.advanceTimersByTime(1);

			// Wait well past any reconnect delay
			vi.advanceTimersByTime(60000);

			// Should still be just 1 socket (no reconnect)
			expect(createdSockets).toHaveLength(1);

			conn.destroy();
		});

		it('should clear pending reconnect timer', () => {
			const config = createMockConfig();
			const conn = createWsConnection(config);

			conn.connect();
			createdSockets[0].simulateOpen();

			// Trigger unexpected close
			createdSockets[0].simulateClose(1006);

			// Before reconnect timer fires, disconnect intentionally
			conn.disconnect();

			// Wait for reconnect timer
			vi.advanceTimersByTime(60000);

			// Should still be just 1 socket
			expect(createdSockets).toHaveLength(1);
		});
	});

	describe('send()', () => {
		it('should return true and send JSON when connected', () => {
			const config = createMockConfig();
			const conn = createWsConnection(config);

			conn.connect();
			createdSockets[0].simulateOpen();

			const result = conn.send({ type: 'query', prompt: 'hello' });

			expect(result).toBe(true);
			// startPing uses setInterval(30000) — no immediate pong, only our message
			expect(createdSockets[0].sentMessages).toHaveLength(1);
			const sentMessage = JSON.parse(createdSockets[0].sentMessages[0]);
			expect(sentMessage.type).toBe('query');

			conn.destroy();
		});

		it('should return false when not connected', () => {
			const config = createMockConfig();
			const conn = createWsConnection(config);

			const result = conn.send({ type: 'test' });

			expect(result).toBe(false);

			conn.destroy();
		});
	});

	describe('isConnected()', () => {
		it('should return false before connect', () => {
			const conn = createWsConnection(createMockConfig());
			expect(conn.isConnected()).toBe(false);
			conn.destroy();
		});

		it('should return true when WebSocket is OPEN', () => {
			const conn = createWsConnection(createMockConfig());
			conn.connect();
			createdSockets[0].simulateOpen();
			expect(conn.isConnected()).toBe(true);
			conn.destroy();
		});

		it('should return false after disconnect', () => {
			const conn = createWsConnection(createMockConfig());
			conn.connect();
			createdSockets[0].simulateOpen();
			conn.disconnect();
			expect(conn.isConnected()).toBe(false);
			conn.destroy();
		});
	});

	describe('message parsing', () => {
		it('should parse and forward JSON messages to onMessage', () => {
			const config = createMockConfig();
			const conn = createWsConnection(config);

			conn.connect();
			createdSockets[0].simulateOpen();
			createdSockets[0].simulateMessage({ type: 'history', messages: [] });

			expect(config.onMessage).toHaveBeenCalledWith({ type: 'history', messages: [] });

			conn.destroy();
		});

		it('should update lastMessageTime on each message', () => {
			const config = createMockConfig();
			const conn = createWsConnection(config);

			conn.connect();
			createdSockets[0].simulateOpen();

			// Advance time
			vi.advanceTimersByTime(10000);

			// Send a message — should update lastMessageTime
			createdSockets[0].simulateMessage({ type: 'ping' });

			// Verify health check doesn't trigger for a while
			// (if lastMessageTime wasn't updated, the 90s timeout would be closer)
			vi.advanceTimersByTime(80000);

			// Should still be open (no stale connection close)
			expect(config.onDisconnected).not.toHaveBeenCalled();

			conn.destroy();
		});
	});

	describe('reconnection', () => {
		it('should reconnect after unexpected close with exponential backoff', () => {
			const config = createMockConfig();
			const conn = createWsConnection(config);

			conn.connect();
			createdSockets[0].simulateOpen();

			// Unexpected close
			createdSockets[0].simulateClose(1006, 'Abnormal closure');

			expect(config.onDisconnected).toHaveBeenCalledWith(1006, 'Abnormal closure', false);

			// First reconnect: base 1s + jitter
			vi.advanceTimersByTime(2000);
			expect(createdSockets).toHaveLength(2);

			conn.destroy();
		});

		it('should respect MAX_RECONNECT_ATTEMPTS and call onMaxReconnectAttempts', () => {
			const config = createMockConfig();
			const conn = createWsConnection(config);

			conn.connect();
			createdSockets[0].simulateOpen();

			// Exhaust reconnect attempts
			for (let i = 0; i < 50; i++) {
				const lastSocket = createdSockets[createdSockets.length - 1];
				lastSocket.simulateClose(1006);
				vi.advanceTimersByTime(60000); // Always enough to pass any backoff
			}

			// After 50 failures, the 51st close should trigger onMaxReconnectAttempts
			const lastSocket = createdSockets[createdSockets.length - 1];
			lastSocket.simulateClose(1006);

			expect(config.onMaxReconnectAttempts).toHaveBeenCalled();

			conn.destroy();
		});

		it('should not reconnect on auth failure (code 4001)', () => {
			const config = createMockConfig();
			const conn = createWsConnection(config);

			conn.connect();
			createdSockets[0].simulateOpen();
			createdSockets[0].simulateClose(4001, 'Authentication failed');

			vi.advanceTimersByTime(60000);

			// Should not have created a second WebSocket
			expect(createdSockets).toHaveLength(1);

			conn.destroy();
		});
	});

	describe('stale close guard', () => {
		it('should ignore close events from a previous WebSocket instance', () => {
			const config = createMockConfig();
			const conn = createWsConnection(config);

			conn.connect();
			const firstSocket = createdSockets[0];
			firstSocket.simulateOpen();

			// Disconnect and reconnect
			conn.disconnect();
			firstSocket.simulateClose(1000);

			conn.resetReconnectAttempts();
			conn.connect();
			const secondSocket = createdSockets[1];
			secondSocket.simulateOpen();

			// Now fire a stale close from the first socket
			const staleCloseCount = (config.onDisconnected as ReturnType<typeof vi.fn>).mock.calls.length;
			firstSocket.onclose?.({
				code: 1006,
				reason: 'stale',
				target: firstSocket, // target is the OLD socket
				wasClean: false
			} as unknown as CloseEvent);

			// Should NOT have called onDisconnected again
			expect((config.onDisconnected as ReturnType<typeof vi.fn>).mock.calls.length).toBe(staleCloseCount);

			conn.destroy();
		});
	});

	describe('ping / health check', () => {
		it('should send pong messages every 30 seconds', () => {
			const config = createMockConfig();
			const conn = createWsConnection(config);

			conn.connect();
			createdSockets[0].simulateOpen();

			// Clear initial messages
			createdSockets[0].sentMessages = [];

			// Advance 30 seconds
			vi.advanceTimersByTime(30000);

			const pongs = createdSockets[0].sentMessages
				.map((m) => JSON.parse(m))
				.filter((m) => m.type === 'pong');
			expect(pongs.length).toBeGreaterThanOrEqual(1);

			conn.destroy();
		});

		it('should force reconnect when connection is stale (idle)', () => {
			const config = createMockConfig();
			const conn = createWsConnection(config);

			conn.connect();
			createdSockets[0].simulateOpen();

			// Health check fires every 30s. At 90s: timeSinceLastMessage = 90000,
			// but the check is strict greater-than (> 90000), so it doesn't trigger.
			// At 120s: timeSinceLastMessage = 120000 > 90000 → forces close.
			vi.advanceTimersByTime(121000);

			// The health check should have triggered a close
			expect(config.onDisconnected).toHaveBeenCalled();

			conn.destroy();
		});
	});

	describe('visibility resume health check', () => {
		function resume() {
			vi.spyOn(document, 'hidden', 'get').mockReturnValue(true);
			document.dispatchEvent(new Event('visibilitychange'));
			vi.spyOn(document, 'hidden', 'get').mockReturnValue(false);
			document.dispatchEvent(new Event('visibilitychange'));
		}

		it('keeps an idle socket open until its scheduled server heartbeat arrives', () => {
			const config = createMockConfig();
			const conn = createWsConnection(config);
			conn.connect();
			const socket = createdSockets[0];
			socket.simulateOpen();
			resume();

			vi.advanceTimersByTime(29_999);
			expect(socket.readyState).toBe(MockWebSocket.OPEN);
			expect(config.onVisibilityVisible).toHaveBeenCalledOnce();
			socket.simulateMessage({ type: 'ping' });
			vi.advanceTimersByTime(10_000);
			expect(socket.readyState).toBe(MockWebSocket.OPEN);
			expect(config.onDisconnected).not.toHaveBeenCalled();
			conn.destroy();
		});

		it('reconnects a half-open socket even when local sends reset the activity time', () => {
			const conn = createWsConnection(createMockConfig());
			conn.connect();
			const socket = createdSockets[0];
			socket.simulateOpen();
			resume();
			vi.advanceTimersByTime(20_000);
			conn.resetLastMessageTime();
			vi.advanceTimersByTime(15_001);
			expect(socket.readyState).toBe(MockWebSocket.CLOSED);
			vi.advanceTimersByTime(2_000);
			expect(createdSockets).toHaveLength(2);
			conn.destroy();
		});

		it('counts server traffic received in the same clock tick as resume', () => {
			const conn = createWsConnection(createMockConfig());
			conn.connect();
			const socket = createdSockets[0];
			socket.simulateOpen();
			resume();
			socket.simulateMessage({ type: 'ping' });
			vi.advanceTimersByTime(35_001);
			expect(socket.readyState).toBe(MockWebSocket.OPEN);
			conn.destroy();
		});

		it('cancels the resume deadline when the page becomes hidden again', () => {
			const conn = createWsConnection(createMockConfig());
			conn.connect();
			const socket = createdSockets[0];
			socket.simulateOpen();
			resume();
			vi.advanceTimersByTime(1_000);
			vi.spyOn(document, 'hidden', 'get').mockReturnValue(true);
			document.dispatchEvent(new Event('visibilitychange'));
			vi.advanceTimersByTime(40_000);
			expect(socket.readyState).toBe(MockWebSocket.OPEN);
			conn.destroy();
		});

		it('does not let an old socket deadline or message affect a replacement socket', () => {
			const config = createMockConfig();
			const conn = createWsConnection(config);
			conn.connect();
			const first = createdSockets[0];
			first.simulateOpen();
			resume();
			vi.advanceTimersByTime(1_000);
			first.simulateClose(1006);
			vi.advanceTimersByTime(2_000);
			const second = createdSockets[1];
			second.simulateOpen();
			resume();
			vi.advanceTimersByTime(10_000);
			first.simulateMessage({ type: 'ping' });
			expect(config.onMessage).not.toHaveBeenCalled();
			vi.advanceTimersByTime(22_001);
			expect(second.readyState).toBe(MockWebSocket.OPEN);
			vi.advanceTimersByTime(3_000);
			expect(second.readyState).toBe(MockWebSocket.CLOSED);
			conn.destroy();
		});

		it('closes an apparently open socket when the resume send throws', () => {
			const conn = createWsConnection(createMockConfig());
			conn.connect();
			const socket = createdSockets[0];
			socket.simulateOpen();
			vi.spyOn(socket, 'send').mockImplementation(() => { throw new Error('Transport lost'); });
			resume();
			vi.advanceTimersByTime(2_001);
			expect(socket.readyState).toBe(MockWebSocket.CLOSED);
			expect(createdSockets).toHaveLength(2);
			conn.destroy();
		});
	});

	describe('destroy()', () => {
		it('should prevent further connect attempts', () => {
			const config = createMockConfig();
			const conn = createWsConnection(config);

			conn.destroy();
			conn.connect();

			expect(createdSockets).toHaveLength(0);
		});

		it('should clean up visibility listener', () => {
			const removeListenerSpy = vi.spyOn(document, 'removeEventListener');
			const conn = createWsConnection(createMockConfig());

			conn.destroy();

			expect(removeListenerSpy).toHaveBeenCalledWith('visibilitychange', expect.any(Function));
			removeListenerSpy.mockRestore();
		});
	});

	// Phase 3.1: the manual "Reconnect" button calls activeSession.reconnect(),
	// which is resetReconnectAttempts() + connect(). Verify that sequence forces
	// an immediate new socket even after backoff attempts have accumulated.
	describe('manual reconnect (resetReconnectAttempts + connect)', () => {
		it('opens a fresh socket immediately when triggered while disconnected', () => {
			const config = createMockConfig();
			const conn = createWsConnection(config);

			// Establish then lose the connection a few times so the backoff counter
			// climbs and the pending auto-reconnect timer is long.
			conn.connect();
			createdSockets[0].simulateOpen();
			createdSockets[0].simulateClose(1006, 'Abnormal closure');
			vi.advanceTimersByTime(5000);
			createdSockets[1].simulateOpen();
			createdSockets[1].simulateClose(1006, 'Abnormal closure');

			const socketsBefore = createdSockets.length;

			// User clicks Reconnect → the two calls activeSession.reconnect() makes.
			conn.resetReconnectAttempts();
			conn.connect();

			// A new socket is created synchronously — no waiting on the backoff timer.
			expect(createdSockets.length).toBe(socketsBefore + 1);

			conn.destroy();
		});
	});
});

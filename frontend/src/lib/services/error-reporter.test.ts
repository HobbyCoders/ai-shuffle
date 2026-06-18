/**
 * Tests for error-reporter.ts — auth gate, dedupe, payload shape, global handlers.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

const mocks = vi.hoisted(() => ({
	post: vi.fn(),
	authenticated: { value: true },
	sessionId: { value: 'sess-123' as string | null },
}));

vi.mock('$lib/api/client', () => ({
	api: { post: mocks.post },
}));
vi.mock('$lib/stores/auth.svelte', () => ({
	auth: {
		get current() {
			return { status: { authenticated: mocks.authenticated.value } };
		},
	},
}));
vi.mock('$lib/stores/active-session', () => ({
	activeSession: {
		get current() {
			return { sessionId: mocks.sessionId.value };
		},
	},
}));

import { reportClientError, errorDetail, installGlobalErrorHandlers } from './error-reporter';

beforeEach(() => {
	mocks.post.mockReset();
	mocks.post.mockResolvedValue({ ok: true, id: 'x' });
	mocks.authenticated.value = true;
	mocks.sessionId.value = 'sess-123';
});

describe('reportClientError', () => {
	it('POSTs to /logs/client-error with the expected payload', () => {
		reportClientError({ source: 'frontend', message: 'unique-payload-msg', status_code: 500 });

		expect(mocks.post).toHaveBeenCalledTimes(1);
		const [path, payload, opts] = mocks.post.mock.calls[0];
		expect(path).toBe('/logs/client-error');
		expect(payload).toMatchObject({
			source: 'frontend',
			message: 'unique-payload-msg',
			level: 'ERROR',
			status_code: 500,
			session_id: 'sess-123',
		});
		// url is filled from location.href by default
		expect(typeof payload.url).toBe('string');
		expect(opts).toMatchObject({ keepalive: true });
	});

	it('does not POST when unauthenticated', () => {
		mocks.authenticated.value = false;
		reportClientError({ message: 'unique-unauth-msg' });
		expect(mocks.post).not.toHaveBeenCalled();
	});

	it('drops empty messages', () => {
		reportClientError({ message: '   ' });
		expect(mocks.post).not.toHaveBeenCalled();
	});

	it('dedupes repeats of the same (source, message, status) within the window', () => {
		reportClientError({ source: 'ws', message: 'unique-dedupe-msg', status_code: 429 });
		reportClientError({ source: 'ws', message: 'unique-dedupe-msg', status_code: 429 });
		expect(mocks.post).toHaveBeenCalledTimes(1);
	});

	it('treats a different status as a distinct error (not deduped)', () => {
		reportClientError({ source: 'ws', message: 'unique-status-msg', status_code: 500 });
		reportClientError({ source: 'ws', message: 'unique-status-msg', status_code: 503 });
		expect(mocks.post).toHaveBeenCalledTimes(2);
	});

	it('prefers an explicit session_id over the active session', () => {
		reportClientError({ message: 'unique-explicit-sess', session_id: 'explicit-1' });
		expect(mocks.post.mock.calls[0][1]).toMatchObject({ session_id: 'explicit-1' });
	});

	it('never throws even if api.post rejects synchronously', () => {
		mocks.post.mockImplementation(() => {
			throw new Error('network down');
		});
		expect(() => reportClientError({ message: 'unique-throw-msg' })).not.toThrow();
	});
});

describe('errorDetail', () => {
	it('returns the stack for an Error', () => {
		const e = new Error('boom');
		expect(errorDetail(e)).toContain('boom');
	});

	it('passes through a string', () => {
		expect(errorDetail('plain string')).toBe('plain string');
	});

	it('serializes an object', () => {
		expect(errorDetail({ a: 1 })).toBe('{"a":1}');
	});

	it('returns undefined for null/number', () => {
		expect(errorDetail(null)).toBeUndefined();
		expect(errorDetail(42)).toBeUndefined();
	});
});

describe('installGlobalErrorHandlers', () => {
	it('reports unhandledrejection and restores prior state on teardown', () => {
		const prevOnError = window.onerror;
		const teardown = installGlobalErrorHandlers();

		window.dispatchEvent(
			new PromiseRejectionEvent('unhandledrejection', {
				promise: Promise.reject('x').catch(() => {}) as unknown as Promise<unknown>,
				reason: new Error('rejected-unique-msg'),
			})
		);
		expect(mocks.post).toHaveBeenCalledTimes(1);
		expect(mocks.post.mock.calls[0][1]).toMatchObject({
			source: 'frontend',
			message: 'rejected-unique-msg',
		});

		teardown();
		expect(window.onerror).toBe(prevOnError);
	});

	it('chains a pre-existing window.onerror', () => {
		const prev = vi.fn();
		window.onerror = prev;
		const teardown = installGlobalErrorHandlers();

		window.onerror?.('chained-unique-msg', 'app.js', 1, 1, new Error('chained-unique-msg'));

		expect(prev).toHaveBeenCalledTimes(1);
		expect(mocks.post).toHaveBeenCalledTimes(1);

		teardown();
		window.onerror = null;
	});
});

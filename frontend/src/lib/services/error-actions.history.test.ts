/**
 * Error notifications loaded from history after a reload keep "Send report to
 * support", built by the same helper as the live WS event, and only for a
 * login that may send reports. Uses the real notification and support stores.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const transport = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), delete: vi.fn(), put: vi.fn(), patch: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: transport }));
vi.mock('$lib/stores/auth.svelte', () => ({ auth: { current: { status: { authenticated: true } } } }));
vi.mock('./fix-with-ai', () => ({ startFixChat: vi.fn() }));

import { historyNotificationActions, serverNotificationActions } from './error-actions';
import { notify, notificationHistory } from '$lib/stores/notifications';
import { supportCaps, supportReportDialog } from '$lib/stores/support-report.svelte';

const ERROR_ROW = {
	id: 'err-1',
	severity: 'error',
	category: 'api_error',
	message: 'Provider returned 500',
	session_id: 's-1',
	action: { kind: 'report', payload: { status_code: 500 } }
};
const RATE_LIMIT_ROW = { id: 'rl-1', severity: 'warning', category: 'api_error', message: 'Rate limited', action: { kind: 'x', payload: { status_code: 429 } } };
const PLAIN_ROW = { id: 'info-1', severity: 'error', category: 'system', message: 'Something else' };

let capabilities: () => Promise<unknown>;

beforeEach(() => {
	vi.clearAllMocks();
	notify.clearAll();
	supportCaps.reset();
	supportReportDialog.close();
	capabilities = async () => ({ basic_report: true, fix_with_ai: false });
	transport.post.mockResolvedValue({});
	transport.delete.mockResolvedValue({});
	transport.get.mockImplementation(async (path: string) => {
		if (path === '/support/capabilities') return capabilities();
		if (path === '/notifications') return { notifications: [ERROR_ROW, RATE_LIMIT_ROW, PLAIN_ROW] };
		return {};
	});
	notify.setHistoryActions(historyNotificationActions);
});
afterEach(() => notify.setHistoryActions(null));

const byId = (id: string) => notificationHistory.current.find((n) => n.id === id);

describe('hydrated api_error notifications', () => {
	it('keep "Send report to support" after a reload', async () => {
		await notify.loadHistory();
		const item = byId('err-1');
		expect(item?.action?.label).toBe('Send report to support');
		item!.action!.onClick();
		expect(supportReportDialog.request?.source).toEqual({
			error: { message: 'Provider returned 500', source: 'ws', status_code: 500 }
		});
	});

	it('leave warnings (429) and other categories without a button', async () => {
		await notify.loadHistory();
		expect(byId('rl-1')).toBeDefined();
		expect(byId('rl-1')?.action).toBeUndefined();
		expect(byId('info-1')?.action).toBeUndefined();
	});

	it('get no button when this login cannot send support reports', async () => {
		capabilities = async () => {
			throw Object.assign(new Error('Owner access required'), { status: 403 });
		};
		await notify.loadHistory();
		expect(supportCaps.status).toBe('forbidden');
		expect(byId('err-1')).toBeDefined();
		expect(byId('err-1')?.action).toBeUndefined();
	});
});

describe('serverNotificationActions', () => {
	it('is what the live event and history both use', () => {
		expect(serverNotificationActions({ ...ERROR_ROW, severity: 'error' } as never).action?.label).toBe('Send report to support');
		expect(serverNotificationActions(RATE_LIMIT_ROW as never)).toEqual({});
		expect(serverNotificationActions(PLAIN_ROW as never)).toEqual({});
	});
});

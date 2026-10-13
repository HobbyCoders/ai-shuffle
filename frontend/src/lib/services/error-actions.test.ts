import { beforeEach, describe, expect, it, vi } from 'vitest';

const caps = vi.hoisted(() => ({ send: true, fix: false }));
vi.mock('$lib/stores/support-report.svelte', () => ({
	canSendSupportReport: () => caps.send,
	canFixWithAi: () => caps.fix,
	openSupportReport: vi.fn()
}));
vi.mock('./fix-with-ai', () => ({ startFixChat: vi.fn() }));

import { errorActions, toSupportError } from './error-actions';
import { openSupportReport } from '$lib/stores/support-report.svelte';
import { startFixChat } from './fix-with-ai';

const ERR = { message: 'boom', source: 'ws', status_code: 500, session_id: 's-1', detail: 'stack' };

beforeEach(() => {
	vi.clearAllMocks();
	caps.send = true;
	caps.fix = false;
});

describe('errorActions', () => {
	it('offers only the support report on a customer install', () => {
		const { action, secondaryAction } = errorActions(ERR);
		expect(action?.label).toBe('Send report to support');
		expect(secondaryAction).toBeUndefined();
		action!.onClick();
		expect(openSupportReport).toHaveBeenCalledWith({ error: { message: 'boom', source: 'ws', status_code: 500, detail: 'stack' } });
	});

	it('adds Fix with AI on a source checkout', () => {
		caps.fix = true;
		const { action, secondaryAction } = errorActions(ERR);
		expect(action?.label).toBe('Send report to support');
		expect(secondaryAction?.label).toBe('Fix with AI');
		secondaryAction!.onClick();
		expect(startFixChat).toHaveBeenCalledWith(ERR);
	});

	it('offers nothing to a login that cannot use either', () => {
		caps.send = false;
		expect(errorActions(ERR)).toEqual({ action: undefined });
	});

	it('drops sources the report API does not take', () => {
		expect(toSupportError({ message: 'x', source: 'mystery' })).toEqual({ message: 'x' });
	});
});

import { afterAll, afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render } from '@testing-library/svelte';
import ControlsDock from './ControlsDock.svelte';

vi.hoisted(() => {
	vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
		ok: false,
		status: 404,
		json: vi.fn().mockResolvedValue({})
	}));
});

vi.mock('$lib/api/client', async (importOriginal) => {
	const actual = await importOriginal<Record<string, unknown>>();
	return {
		...actual,
		getUsageLimits: vi.fn().mockResolvedValue({
			available: false,
			buckets: {},
			provider_buckets: {}
		})
	};
});

afterEach(() => cleanup());
afterAll(() => vi.unstubAllGlobals());

describe('ControlsDock mobile control order', () => {
	it('places chat settings before context usage', () => {
		const { container } = render(ControlsDock, {
			props: {
				isUploading: false,
				onAttachClick: vi.fn()
			}
		});
		const mobileControls = container.querySelector('.mobile-controls');
		const settings = mobileControls?.querySelector('.mobile-settings-trigger');
		const contextUsage = mobileControls?.querySelector('.usage-context-wrapper');

		expect(settings).toBeInstanceOf(Element);
		expect(contextUsage).toBeInstanceOf(Element);
		if (!(settings instanceof Element) || !(contextUsage instanceof Element)) return;
		expect(settings.compareDocumentPosition(contextUsage) & Node.DOCUMENT_POSITION_FOLLOWING)
			.toBeTruthy();
	});

	it('places attach on the left and usage on the right', () => {
		const { container } = render(ControlsDock, {
			props: {
				isUploading: false,
				onAttachClick: vi.fn()
			}
		});
		const left = container.querySelector('.dock-left');
		const mobile = container.querySelector('.mobile-controls');
		const attach = left?.querySelector('.attach-btn');
		const contextUsage = mobile?.querySelector('.usage-context-wrapper');

		expect(attach).toBeInstanceOf(Element);
		expect(contextUsage).toBeInstanceOf(Element);
		if (!(attach instanceof Element) || !(contextUsage instanceof Element)) return;
		expect(attach.compareDocumentPosition(contextUsage) & Node.DOCUMENT_POSITION_FOLLOWING)
			.toBeTruthy();
	});
});

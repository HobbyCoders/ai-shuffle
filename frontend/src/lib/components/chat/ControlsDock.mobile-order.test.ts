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
	it('places chat settings first on the left, then attach, with usage on the right', () => {
		const { container } = render(ControlsDock, {
			props: {
				isUploading: false,
				onAttachClick: vi.fn()
			}
		});
		const dock = container.querySelector('.controls-dock');
		expect(dock instanceof HTMLElement && !dock.inert).toBe(true);
		const left = container.querySelector('.dock-left');
		const mobile = container.querySelector('.mobile-controls');
		const settings = left?.querySelector('.mobile-settings-trigger');
		const attach = left?.querySelector('.attach-btn');
		const contextUsage = mobile?.querySelector('.usage-context-wrapper');

		expect(settings).toBeInstanceOf(Element);
		expect(attach).toBeInstanceOf(Element);
		expect(contextUsage).toBeInstanceOf(Element);
		if (
			!(settings instanceof Element) ||
			!(attach instanceof Element) ||
			!(contextUsage instanceof Element)
		) {
			return;
		}
		expect(settings.classList.contains('dock-ico')).toBe(true);
		expect(settings.classList.contains('composer-mobile-only')).toBe(true);
		expect(settings.classList.contains('mobile-only')).toBe(false);
		expect(settings.compareDocumentPosition(attach) & Node.DOCUMENT_POSITION_FOLLOWING)
			.toBeTruthy();
		expect(attach.compareDocumentPosition(contextUsage) & Node.DOCUMENT_POSITION_FOLLOWING)
			.toBeTruthy();
	});

	it('marks the dock inert when ChatInput is guarding the expand tap', () => {
		const { container } = render(ControlsDock, {
			props: {
				isUploading: false,
				onAttachClick: vi.fn(),
				inertToolbar: true
			}
		});
		const dock = container.querySelector('.controls-dock');
		expect(dock).toBeInstanceOf(HTMLElement);
		expect(dock instanceof HTMLElement && dock.inert).toBe(true);
	});
});

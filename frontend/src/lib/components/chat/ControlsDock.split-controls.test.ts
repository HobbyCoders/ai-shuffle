import { afterAll, afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
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

function renderDock() {
	return render(ControlsDock, {
		props: {
			isUploading: false,
			onAttachClick: vi.fn()
		}
	});
}

describe('ControlsDock split controls', () => {
	it('splits the old summary chip into permission, model and effort controls', () => {
		const { container } = renderDock();

		expect(container.querySelector('.controls-summary-chip')).toBeNull();
		expect(screen.getByRole('button', { name: /^permission mode:/i })).toBeTruthy();
		expect(screen.getByRole('button', { name: /^model:/i })).toBeTruthy();
		expect(screen.getByRole('button', { name: /^effort:/i })).toBeTruthy();
		expect(screen.getByRole('button', { name: /attach file/i })).toBeTruthy();
		expect(screen.getByRole('button', { name: /more input options/i })).toBeTruthy();
	});

	it('orders the row permission · attach · more on the left, model · effort · ring on the right', () => {
		const { container } = renderDock();

		const left = container.querySelector('.dock-left');
		const right = container.querySelector('.dock-right');
		expect(left?.contains(screen.getByRole('button', { name: /^permission mode:/i }))).toBe(true);
		expect(left?.contains(screen.getByRole('button', { name: /attach file/i }))).toBe(true);
		expect(left?.contains(screen.getByRole('button', { name: /more input options/i }))).toBe(true);
		expect(right?.contains(screen.getByRole('button', { name: /^model:/i }))).toBe(true);
		expect(right?.contains(screen.getByRole('button', { name: /^effort:/i }))).toBe(true);
		expect(right?.querySelector('.usage-context-wrapper')).toBeTruthy();
		expect(left && right && (left.compareDocumentPosition(right) & Node.DOCUMENT_POSITION_FOLLOWING))
			.toBeTruthy();
	});

	it('opens the compact effort popover from the effort tag', async () => {
		renderDock();

		await fireEvent.click(screen.getByRole('button', { name: /^effort:/i }));

		const dialog = screen.getByRole('dialog', { name: /reasoning effort/i });
		expect(dialog.querySelector('.effort-pop-card')).toBeTruthy();
		expect(screen.getByRole('slider', { name: /reasoning effort/i })).toBeTruthy();
		expect(screen.getByRole('button', { name: /^effort:/i }).getAttribute('aria-expanded')).toBe('true');
	});

	it('opens the tool-access menu from the permission text', async () => {
		renderDock();

		await fireEvent.click(screen.getByRole('button', { name: /^permission mode:/i }));

		const menu = screen.getByRole('menu', { name: /tool access/i });
		expect(menu.querySelectorAll('[role="menuitemradio"]')).toHaveLength(5);
		expect(screen.getByRole('menuitemradio', { name: /bypass/i })).toBeTruthy();
	});

	it('keeps only one dock popover open at a time and closes on Escape', async () => {
		renderDock();

		await fireEvent.click(screen.getByRole('button', { name: /^effort:/i }));
		expect(screen.getByRole('dialog', { name: /reasoning effort/i })).toBeTruthy();

		await fireEvent.click(screen.getByRole('button', { name: /^permission mode:/i }));
		expect(screen.queryByRole('dialog', { name: /reasoning effort/i })).toBeNull();
		expect(screen.getByRole('menu', { name: /tool access/i })).toBeTruthy();

		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(screen.queryByRole('menu', { name: /tool access/i })).toBeNull();
	});

	it('opens the full controls popover from the chevron without repeating chat setup', async () => {
		renderDock();

		await fireEvent.click(screen.getByRole('button', { name: /more input options/i }));

		const dialog = screen.getByRole('dialog', { name: /^chat controls$/i });
		expect(dialog.textContent).toMatch(/reasoning effort/i);
		expect(dialog.textContent).toMatch(/tool access/i);
		expect(dialog.textContent).not.toMatch(/chat setup/i);
		expect(dialog.querySelector('.context-pill-bar')).toBeNull();
	});

	it('opens the model picker from the model text', async () => {
		renderDock();

		await fireEvent.click(screen.getByRole('button', { name: /^model:/i }));

		const dialog = screen.getByRole('dialog', { name: /choose model/i });
		expect(dialog.querySelector('[data-model-picker-panel]')).toBeTruthy();
	});
});

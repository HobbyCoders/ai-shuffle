/**
 * #371 step 4: the attach sheet gives a running no-project session its
 * project. It must state the one-way warning before the confirm, refuse while
 * blocked, and mirror the converted row once the backend accepts.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';

const mocks = vi.hoisted(() => ({
	attachProject: vi.fn(),
	applyAttachedProject: vi.fn(),
	notify: vi.fn(),
}));

vi.mock('$lib/stores/app-data.svelte', async () => {
	const { writable: w } = await import('svelte/store');
	return {
		projects: w([
			{ id: 'proj-local', name: 'ai-shuffle', path: '/w/ai-shuffle' },
			{ id: 'proj-two', name: 'site', path: '/w/site' },
		]),
		clusterProjects: w([
			{ project_v2_id: 'v2-remote', origin: 'git@example.com:me/remote-only.git', name: 'remote-only', local_project_id: null, eligible: [{ id: 'peer', online: true }] },
		]),
	};
});
vi.mock('$lib/api/sessions', () => ({ attachProject: mocks.attachProject }));
vi.mock('$lib/stores/active-session', () => ({ activeSession: { applyAttachedProject: mocks.applyAttachedProject } }));
vi.mock('$lib/stores/notifications', () => ({ notify: mocks.notify }));

import { tick } from 'svelte';
import { platform } from '$lib/stores/platform.svelte';
import AttachProjectSheet from './AttachProjectSheet.svelte';

const props = (over: Partial<{ blockedReason: string | null; onClose: () => void }> = {}) => ({
	open: true,
	sessionId: 'sess-1',
	agentName: 'Juniper',
	blockedReason: null,
	onClose: vi.fn(),
	...over,
});

beforeEach(() => {
	mocks.attachProject.mockReset();
	mocks.applyAttachedProject.mockReset();
	mocks.notify.mockReset();
});
afterEach(() => cleanup());

describe('AttachProjectSheet', () => {
	it('shows the one-way warning before anything is picked, and names the project once one is', async () => {
		render(AttachProjectSheet, { props: props() });

		const note = screen.getByRole('note');
		expect(note.textContent).toContain('One-way. Projects can’t be detached.');
		expect(note.textContent).toContain('Juniper keeps this conversation');

		await fireEvent.click(screen.getByRole('radio', { name: /ai-shuffle/ }));
		expect(screen.getByRole('note').textContent).toContain('ai-shuffle’s checkout');
	});

	it('says plainly that access returns to Ask', () => {
		render(AttachProjectSheet, { props: props() });
		expect(screen.getByRole('note').textContent).toContain('Access returns to Ask');
	});

	it('is one Tab stop, and the arrow keys move the pick and the focus, skipping other workers’ projects', async () => {
		render(AttachProjectSheet, { props: props() });
		const radio = (name: RegExp) => screen.getByRole('radio', { name }) as HTMLButtonElement;
		const stops = () => screen.getAllByRole('radio').filter((el) => el.tabIndex === 0);

		// Nothing picked yet: the first attachable project is the only stop.
		expect(stops()).toEqual([radio(/ai-shuffle/)]);

		radio(/ai-shuffle/).focus();
		await fireEvent.keyDown(radio(/ai-shuffle/), { key: 'ArrowDown' });
		expect(radio(/site/).getAttribute('aria-checked')).toBe('true');
		expect(document.activeElement).toBe(radio(/site/));
		expect(stops()).toEqual([radio(/site/)]);

		// remote-only is on another worker: the arrow wraps past it.
		await fireEvent.keyDown(radio(/site/), { key: 'ArrowDown' });
		expect(document.activeElement).toBe(radio(/ai-shuffle/));
		expect(radio(/remote-only/).getAttribute('aria-checked')).toBe('false');

		await fireEvent.keyDown(radio(/ai-shuffle/), { key: 'End' });
		expect(document.activeElement).toBe(radio(/site/));
		await fireEvent.keyDown(radio(/site/), { key: 'Home' });
		expect(document.activeElement).toBe(radio(/ai-shuffle/));
		expect(radio(/ai-shuffle/).getAttribute('aria-checked')).toBe('true');
	});

	it('keeps the confirm disabled until a local project is picked', async () => {
		render(AttachProjectSheet, { props: props() });
		const confirm = () => screen.getByRole('button', { name: /^attach/i }) as HTMLButtonElement;

		expect(confirm().disabled).toBe(true);
		// A project only another worker has cannot be attached from here.
		const remote = screen.getByRole('radio', { name: /remote-only/ });
		expect(remote.getAttribute('aria-disabled')).toBe('true');
		await fireEvent.click(remote);
		expect(confirm().disabled).toBe(true);

		await fireEvent.click(screen.getByRole('radio', { name: /site/ }));
		expect(confirm().disabled).toBe(false);
	});

	it('refuses while blocked and says why', async () => {
		render(AttachProjectSheet, { props: props({ blockedReason: 'Wait for this turn to finish, then attach.' }) });
		await fireEvent.click(screen.getByRole('radio', { name: /ai-shuffle/ }));

		expect(screen.getByText('Wait for this turn to finish, then attach.')).toBeTruthy();
		expect((screen.getByRole('button', { name: /^attach/i }) as HTMLButtonElement).disabled).toBe(true);
	});

	it('attaches through the API, mirrors the converted session and closes', async () => {
		mocks.attachProject.mockResolvedValue({ session_id: 'sess-1', project_id: 'proj-local', project_v2_id: 'v2-local', mode: 'code', permission_mode: 'default' });
		const onClose = vi.fn();
		render(AttachProjectSheet, { props: props({ onClose }) });

		await fireEvent.click(screen.getByRole('radio', { name: /ai-shuffle/ }));
		await fireEvent.click(screen.getByRole('button', { name: /^attach/i }));

		await waitFor(() => expect(onClose).toHaveBeenCalled());
		expect(mocks.attachProject).toHaveBeenCalledWith('sess-1', 'proj-local');
		// The attach put the session back on Ask; the open session shows that.
		expect(mocks.applyAttachedProject).toHaveBeenCalledWith('sess-1', 'proj-local', 'v2-local', 'default');
		expect(mocks.notify).toHaveBeenCalled();
	});

	it('keeps the sheet open with the server’s reason when the attach is refused', async () => {
		mocks.attachProject.mockRejectedValue(new Error('This session already has a project.'));
		const onClose = vi.fn();
		render(AttachProjectSheet, { props: props({ onClose }) });

		await fireEvent.click(screen.getByRole('radio', { name: /ai-shuffle/ }));
		await fireEvent.click(screen.getByRole('button', { name: /^attach/i }));

		await waitFor(() => expect(screen.getByText(/already has a project/)).toBeTruthy());
		expect(onClose).not.toHaveBeenCalled();
		expect(mocks.applyAttachedProject).not.toHaveBeenCalled();
	});
});

describe('AttachProjectSheet on the phone: drag down to dismiss', () => {
	beforeEach(() => platform.set('mobile'));
	afterEach(() => {
		platform.set('desktop');
		vi.unstubAllGlobals();
	});

	/** One touch event as the WebView sends it; returns it to read defaultPrevented. */
	function touch(target: Element, type: 'touchstart' | 'touchmove' | 'touchend', y: number, time: number) {
		const event = new Event(type, { bubbles: true, cancelable: true });
		Object.defineProperty(event, 'touches', { value: type === 'touchend' ? [] : [{ clientX: 20, clientY: y }] });
		Object.defineProperty(event, 'timeStamp', { value: time });
		target.dispatchEvent(event);
		return event;
	}

	/** A slow pull from `from` down `by` pixels, released at the end. */
	function pull(target: Element, by: number, from = 200) {
		touch(target, 'touchstart', from, 0);
		let last: Event | null = null;
		for (let step = 1; step <= 10; step++) last = touch(target, 'touchmove', from + (by * step) / 10, step * 100);
		touch(target, 'touchend', from + by, 1100);
		return last!;
	}

	const sheet = () => screen.getByRole('dialog') as HTMLElement;
	const drag = () => sheet().style.getPropertyValue('--sheet-drag');

	it('follows the finger from the header and closes past the threshold', async () => {
		const onClose = vi.fn();
		render(AttachProjectSheet, { props: props({ onClose }) });
		const title = screen.getByRole('heading', { name: 'Attach a project' });

		touch(title, 'touchstart', 200, 0);
		const move = touch(title, 'touchmove', 260, 100);
		expect(move.defaultPrevented).toBe(true);
		await tick();
		expect(drag()).toBe('60px');
		expect(sheet().classList.contains('dragging')).toBe(true);

		touch(title, 'touchmove', 320, 200);
		touch(title, 'touchend', 320, 300);
		await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
	});

	it('springs back from a short slow pull', async () => {
		const onClose = vi.fn();
		render(AttachProjectSheet, { props: props({ onClose }) });

		pull(screen.getByRole('heading', { name: 'Attach a project' }), 40);
		await tick();
		expect(drag()).toBe('0px');
		expect(sheet().classList.contains('dragging')).toBe(false);
		await new Promise((r) => setTimeout(r, 300));
		expect(onClose).not.toHaveBeenCalled();
	});

	it('closes on a quick flick, at once when motion is reduced', async () => {
		vi.stubGlobal('matchMedia', (query: string) => ({ matches: query.includes('reduce'), media: query }));
		const onClose = vi.fn();
		render(AttachProjectSheet, { props: props({ onClose }) });
		const title = screen.getByRole('heading', { name: 'Attach a project' });

		touch(title, 'touchstart', 200, 0);
		touch(title, 'touchmove', 215, 10);
		touch(title, 'touchmove', 230, 20);
		touch(title, 'touchmove', 245, 30);
		touch(title, 'touchend', 245, 40);
		expect(onClose).toHaveBeenCalledTimes(1);
	});

	it('lets a scrolled list scroll, and drags it only once it is at its top', async () => {
		const onClose = vi.fn();
		render(AttachProjectSheet, { props: props({ onClose }) });
		const list = screen.getByRole('radiogroup');
		const row = screen.getByRole('radio', { name: /site/ });

		Object.defineProperty(list, 'scrollTop', { configurable: true, value: 80 });
		const scrolling = pull(row, 120);
		expect(scrolling.defaultPrevented).toBe(false);
		await tick();
		expect(drag()).toBe('0px');

		// At the top, scrolling the list up is still the list's.
		Object.defineProperty(list, 'scrollTop', { configurable: true, value: 0 });
		touch(row, 'touchstart', 300, 0);
		expect(touch(row, 'touchmove', 240, 100).defaultPrevented).toBe(false);
		touch(row, 'touchend', 240, 200);

		// …and pulling down from the top moves the sheet.
		expect(pull(row, 120).defaultPrevented).toBe(true);
		// The release must not also pick the row under the finger.
		row.click();
		await tick();
		expect(row.getAttribute('aria-checked')).toBe('false');
		await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
	});

	it('does not move while the attach is in flight', async () => {
		mocks.attachProject.mockReturnValue(new Promise(() => {}));
		const onClose = vi.fn();
		render(AttachProjectSheet, { props: props({ onClose }) });
		await fireEvent.click(screen.getByRole('radio', { name: /ai-shuffle/ }));
		await fireEvent.click(screen.getByRole('button', { name: /^attach/i }));

		const move = pull(screen.getByRole('heading', { name: 'Attach a project' }), 200);
		expect(move.defaultPrevented).toBe(false);
		await tick();
		expect(drag()).toBe('0px');
		await new Promise((r) => setTimeout(r, 300));
		expect(onClose).not.toHaveBeenCalled();
	});

	it('stays put on desktop, where the sheet is a centred dialog', async () => {
		platform.set('desktop');
		const onClose = vi.fn();
		render(AttachProjectSheet, { props: props({ onClose }) });

		const move = pull(screen.getByRole('heading', { name: 'Attach a project' }), 200);
		expect(move.defaultPrevented).toBe(false);
		await new Promise((r) => setTimeout(r, 300));
		expect(onClose).not.toHaveBeenCalled();
	});
});

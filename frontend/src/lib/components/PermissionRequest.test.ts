/**
 * Behavior tests for the simplified permission prompt: it shows WHAT is being
 * requested and offers exactly two actions (Approve / Deny) plus one
 * "always for this tool" checkbox that applies to whichever button is pressed.
 * Defer was removed from the UI entirely (the backend still supports it).
 */
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { render, screen, cleanup, fireEvent } from '@testing-library/svelte';
import PermissionRequest, { type PermissionRequestData } from './PermissionRequest.svelte';

function makeRequest(over: Partial<PermissionRequestData> = {}): PermissionRequestData {
	return {
		request_id: 'req-1',
		tool_name: 'Bash',
		tool_input: { command: 'git status' },
		...over
	};
}

afterEach(() => {
	cleanup();
});

describe('PermissionRequest — approve/deny + always-remember', () => {
	it('shows the tool name and the requested details', () => {
		render(PermissionRequest, { props: { request: makeRequest() } });

		expect(screen.getByText('Bash')).toBeTruthy();
		expect(screen.getByText(/git status/)).toBeTruthy();
	});

	it('renders exactly two action buttons — Approve and Deny — and no Defer', () => {
		render(PermissionRequest, { props: { request: makeRequest() } });

		expect(screen.getByRole('button', { name: 'Approve' })).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Deny' })).toBeTruthy();
		expect(screen.queryByRole('button', { name: /defer/i })).toBeNull();
		expect(screen.queryByText(/defer/i)).toBeNull();
	});

	it('approves without remembering when the checkbox is unchecked', async () => {
		const onRespond = vi.fn();
		render(PermissionRequest, { props: { request: makeRequest(), onRespond } });

		await fireEvent.click(screen.getByRole('button', { name: 'Approve' }));

		expect(onRespond).toHaveBeenCalledTimes(1);
		expect(onRespond.mock.calls[0][0]).toMatchObject({
			request_id: 'req-1',
			decision: 'allow',
			remember: 'none'
		});
		// No pattern is sent — the remember scope is tool-wide, never input-scoped.
		expect(onRespond.mock.calls[0][0].pattern).toBeUndefined();
	});

	it('denies without remembering when the checkbox is unchecked', async () => {
		const onRespond = vi.fn();
		render(PermissionRequest, { props: { request: makeRequest(), onRespond } });

		await fireEvent.click(screen.getByRole('button', { name: 'Deny' }));

		expect(onRespond.mock.calls[0][0]).toMatchObject({
			decision: 'deny',
			remember: 'none'
		});
	});

	it('checkbox + Approve remembers an always-allow for the tool', async () => {
		const onRespond = vi.fn();
		render(PermissionRequest, { props: { request: makeRequest(), onRespond } });

		const checkbox = screen.getByRole('checkbox') as HTMLInputElement;
		expect(checkbox.checked).toBe(false);
		await fireEvent.click(checkbox);
		await fireEvent.click(screen.getByRole('button', { name: 'Approve' }));

		expect(onRespond.mock.calls[0][0]).toMatchObject({
			decision: 'allow',
			remember: 'profile'
		});
		expect(onRespond.mock.calls[0][0].pattern).toBeUndefined();
	});

	it('checkbox + Deny remembers an always-deny for the tool', async () => {
		const onRespond = vi.fn();
		render(PermissionRequest, { props: { request: makeRequest(), onRespond } });

		await fireEvent.click(screen.getByRole('checkbox'));
		await fireEvent.click(screen.getByRole('button', { name: 'Deny' }));

		expect(onRespond.mock.calls[0][0]).toMatchObject({
			decision: 'deny',
			remember: 'profile'
		});
	});

	it('labels the checkbox with the tool it applies to', () => {
		render(PermissionRequest, { props: { request: makeRequest({ tool_name: 'WebFetch', tool_input: { url: 'https://example.com' } }) } });

		expect(screen.getByLabelText(/Always do this for WebFetch/i)).toBeTruthy();
	});


});

/** Make the prompt's shortcuts live: a keyup after it appeared, and the
 *  visibility window elapsed. */
async function arm() {
	await fireEvent.keyUp(document.body, { key: 'Shift' });
	vi.advanceTimersByTime(500);
}

describe('PermissionRequest — page-wide keyboard shortcuts', () => {
	beforeEach(() => {
		vi.useFakeTimers();
	});
	afterEach(() => {
		vi.useRealTimers();
	});

	it('once armed, Enter with nothing focused approves and Escape denies', async () => {
		const onRespond = vi.fn();
		render(PermissionRequest, { props: { request: makeRequest(), onRespond } });
		await arm();

		await fireEvent.keyDown(document.body, { key: 'Enter' });
		await fireEvent.keyUp(document.body, { key: 'Enter' });
		await fireEvent.keyDown(document.body, { key: 'Escape' });

		expect(onRespond.mock.calls.map(([r]) => r.decision)).toEqual(['allow', 'deny']);
	});

	it('Enter within the visibility window does nothing, even after a keyup', async () => {
		const onRespond = vi.fn();
		render(PermissionRequest, { props: { request: makeRequest(), onRespond } });

		await fireEvent.keyUp(document.body, { key: 'o' });
		vi.advanceTimersByTime(499);
		await fireEvent.keyDown(document.body, { key: 'Enter' });
		expect(onRespond).not.toHaveBeenCalled();

		vi.advanceTimersByTime(1);
		await fireEvent.keyUp(document.body, { key: 'Enter' });
		await fireEvent.keyDown(document.body, { key: 'Enter' });
		expect(onRespond).toHaveBeenCalledTimes(1);
		expect(onRespond).toHaveBeenCalledWith(expect.objectContaining({ decision: 'allow' }));
	});

	it('Enter after the window but with no keyup since the prompt appeared does nothing', async () => {
		const onRespond = vi.fn();
		render(PermissionRequest, { props: { request: makeRequest(), onRespond } });
		vi.advanceTimersByTime(2000);

		await fireEvent.keyDown(document.body, { key: 'Enter' });
		await fireEvent.keyDown(document.body, { key: 'Escape' });
		expect(onRespond).not.toHaveBeenCalled();
	});

	it('a held key (auto-repeat) never approves or denies', async () => {
		const onRespond = vi.fn();
		render(PermissionRequest, { props: { request: makeRequest(), onRespond } });
		await arm();

		for (let i = 0; i < 20; i++) {
			await fireEvent.keyDown(document.body, { key: 'Enter', repeat: true });
			await fireEvent.keyDown(document.body, { key: 'Escape', repeat: true });
			vi.advanceTimersByTime(50);
		}
		expect(onRespond).not.toHaveBeenCalled();
	});

	it('a deliberate Escape denies as soon as a keyup was seen, without waiting out the window', async () => {
		const onRespond = vi.fn();
		render(PermissionRequest, { props: { request: makeRequest(), onRespond } });

		await fireEvent.keyDown(document.body, { key: 'Escape' });
		expect(onRespond).not.toHaveBeenCalled();

		await fireEvent.keyUp(document.body, { key: 'Escape' });
		await fireEvent.keyDown(document.body, { key: 'Escape' });
		expect(onRespond).toHaveBeenCalledWith(expect.objectContaining({ decision: 'deny' }));
	});

	it('one press answers once: a second Enter needs its own keyup', async () => {
		const onRespond = vi.fn();
		render(PermissionRequest, { props: { request: makeRequest(), onRespond } });
		await arm();

		await fireEvent.keyDown(document.body, { key: 'Enter' });
		await fireEvent.keyDown(document.body, { key: 'Enter' });
		expect(onRespond).toHaveBeenCalledTimes(1);
	});

	it('Enter on the focused Deny button does not approve', async () => {
		const onRespond = vi.fn();
		render(PermissionRequest, { props: { request: makeRequest(), onRespond } });
		await arm();

		const deny = screen.getByRole('button', { name: 'Deny' });
		deny.focus();
		const notPrevented = await fireEvent.keyDown(deny, { key: 'Enter' });

		// The native button activation is left alone and nothing is approved.
		expect(notPrevented).toBe(true);
		expect(onRespond).not.toHaveBeenCalled();

		// Escape has no meaning on a button, so it still denies from there.
		await fireEvent.keyDown(deny, { key: 'Escape' });
		expect(onRespond).toHaveBeenCalledWith(expect.objectContaining({ decision: 'deny' }));
	});

	it("holds back a held Enter's native activation of the prompt's own buttons", async () => {
		const onRespond = vi.fn();
		render(PermissionRequest, { props: { request: makeRequest(), onRespond } });
		await arm();

		const approve = screen.getByRole('button', { name: 'Approve' });
		approve.focus();
		const notPrevented = await fireEvent.keyDown(approve, { key: 'Enter', repeat: true });
		expect(notPrevented).toBe(false);
		expect(onRespond).not.toHaveBeenCalled();
	});

	it('Enter and Escape typed into a text field elsewhere on the page are ignored', async () => {
		const onRespond = vi.fn();
		render(PermissionRequest, { props: { request: makeRequest(), onRespond } });
		const composer = document.createElement('textarea');
		document.body.appendChild(composer);
		await arm();

		await fireEvent.keyDown(composer, { key: 'Enter' });
		await fireEvent.keyDown(composer, { key: 'Escape' });

		expect(onRespond).not.toHaveBeenCalled();
		composer.remove();
	});
});

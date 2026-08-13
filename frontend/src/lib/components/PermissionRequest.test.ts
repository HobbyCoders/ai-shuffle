/**
 * Behavior tests for the simplified permission prompt: it shows WHAT is being
 * requested and offers exactly two actions (Approve / Deny) plus one
 * "always for this tool" checkbox that applies to whichever button is pressed.
 * Defer was removed from the UI entirely (the backend still supports it).
 */
import { describe, it, expect, afterEach, vi } from 'vitest';
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

	it('plan approvals keep their own two buttons and offer no remember checkbox', () => {
		render(PermissionRequest, {
			props: { request: makeRequest({ tool_name: 'ExitPlanMode', tool_input: {} }) }
		});

		expect(screen.getByRole('button', { name: 'Approve Plan' })).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Reject Plan' })).toBeTruthy();
		expect(screen.queryByRole('checkbox')).toBeNull();
	});
});

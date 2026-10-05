/**
 * The docked prompts above the phone's composer (#1816): permission and
 * one-shot access approvals answered by sliding (right approves, left denies)
 * or by the tappable Deny / Approve; "Always do this"; questions as large
 * option buttons or a typed answer; "1 of N" cycling.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import MobilePromptDock from './MobilePromptDock.svelte';
import { chatAccessSummary } from '$lib/stores/chat-access';

afterEach(() => {
	cleanup();
	chatAccessSummary.set(null);
});

const bash = { request_id: 'p1', tool_name: 'Bash', tool_input: { command: 'cd desktop && npm run dist:linux' } };
const edit = { request_id: 'p2', tool_name: 'Edit', tool_input: { file_path: 'docs/install/linux.md' } };
const question = {
	request_id: 'q1', tool_use_id: 'tu1',
	questions: [{ question: 'Which install format should the guide lead with?', header: 'Format', multiSelect: false,
		options: [{ label: 'AppImage', description: 'Recommended' }, { label: '.deb package', description: '' }] }]
};

function renderDock(overrides: Record<string, unknown> = {}) {
	const onPermission = vi.fn();
	const onQuestion = vi.fn();
	const view = render(MobilePromptDock, { props: {
		sessionId: 's1', permissions: [bash], questions: [], working: true, connected: true, onPermission, onQuestion, ...overrides
	} });
	return { ...view, onPermission, onQuestion };
}

/** Wait out the arming delay that keeps a stray tap from answering. */
const armed = () => new Promise((resolve) => setTimeout(resolve, 350));

async function slide(dx: number) {
	const knob = screen.getByRole('slider');
	await fireEvent.pointerDown(knob, { pointerId: 3, button: 0, clientX: 200, clientY: 10 });
	await fireEvent.pointerMove(knob, { pointerId: 3, clientX: 200 + dx / 2, clientY: 10 });
	await fireEvent.pointerMove(knob, { pointerId: 3, clientX: 200 + dx, clientY: 10 });
	await fireEvent.pointerUp(knob, { pointerId: 3, clientX: 200 + dx, clientY: 10 });
}

describe('permission prompt', () => {
	it('shows the tool and its command above the composer', () => {
		renderDock();
		const dock = screen.getByRole('region', { name: 'Waiting on you' });
		expect(within(dock).getByText('Bash')).toBeTruthy();
		expect(dock.querySelector('.cmd')?.textContent).toContain('cd desktop && npm run dist:linux');
		expect(within(dock).getByRole('checkbox', { name: 'Always do this' })).toBeTruthy();
	});

	it('approves when the knob is slid right', async () => {
		const { onPermission } = renderDock();
		await slide(160);
		expect(onPermission).toHaveBeenCalledExactlyOnceWith({ request_id: 'p1', decision: 'allow', remember: 'none' });
		expect(await screen.findByRole('status')).toBeTruthy();
	});

	it('denies when the knob is slid left', async () => {
		const { onPermission } = renderDock();
		await slide(-160);
		expect(onPermission).toHaveBeenCalledExactlyOnceWith({ request_id: 'p1', decision: 'deny', remember: 'none' });
	});

	it('answers once: a second slide on the same request does nothing', async () => {
		const { onPermission } = renderDock();
		await slide(160);
		await waitFor(() => expect(screen.queryByRole('slider')).toBeNull());
		expect(onPermission).toHaveBeenCalledOnce();
	});

	it('takes the arrow keys on the knob', async () => {
		const { onPermission } = renderDock();
		const knob = screen.getByRole('slider');
		await fireEvent.keyDown(knob, { key: 'ArrowLeft' });
		expect(onPermission).not.toHaveBeenCalled();
		await fireEvent.keyDown(knob, { key: 'ArrowLeft' });
		expect(onPermission).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ decision: 'deny' }));
	});

	it('holds back a tap that lands as the prompt appears, then approves with Always', async () => {
		const { onPermission } = renderDock();
		await fireEvent.click(screen.getByRole('button', { name: /Approve/ }));
		expect(onPermission).not.toHaveBeenCalled();
		await armed();
		await fireEvent.click(screen.getByRole('checkbox', { name: 'Always do this' }));
		await fireEvent.click(screen.getByRole('button', { name: /Approve/ }));
		expect(onPermission).toHaveBeenCalledExactlyOnceWith({ request_id: 'p1', decision: 'allow', remember: 'profile' });
	});

	it('denies with the Deny button', async () => {
		const { onPermission } = renderDock();
		await armed();
		await fireEvent.click(screen.getByRole('button', { name: /Deny/ }));
		expect(onPermission).toHaveBeenCalledExactlyOnceWith({ request_id: 'p1', decision: 'deny', remember: 'none' });
	});

	it('offers no "Always" for a prompt relayed from another worker', () => {
		renderDock({ permissions: [{ ...bash, provider_agent: { remote_approval_id: 'r1', worker_name: 'Unraid' } }] });
		expect(screen.queryByRole('checkbox', { name: 'Always do this' })).toBeNull();
		expect(screen.getByText('From Unraid. Answer each time.')).toBeTruthy();
	});

	it('cycles several waiting requests with "1 of N"', async () => {
		renderDock({ permissions: [bash, edit], questions: [question] });
		const next = screen.getByRole('button', { name: '1 of 3. Show the next request' });
		expect(next.textContent).toContain('1 of 3');
		await fireEvent.click(next);
		expect(screen.getByText('Edit')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: '2 of 3. Show the next request' }));
		expect(screen.getByText('Which install format should the guide lead with?')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: '3 of 3. Show the next request' }));
		expect(screen.getByText('Bash')).toBeTruthy();
	});

	it('renders nothing when nothing waits', () => {
		renderDock({ permissions: [] });
		expect(screen.queryByRole('region', { name: 'Waiting on you' })).toBeNull();
	});
});

describe('agent question', () => {
	it('answers with a large option button', async () => {
		const { onQuestion } = renderDock({ permissions: [], questions: [question] });
		await armed();
		await fireEvent.click(screen.getByRole('button', { name: /AppImage/ }));
		expect(onQuestion).toHaveBeenCalledExactlyOnceWith({
			request_id: 'q1', tool_use_id: 'tu1', answers: { 'Which install format should the guide lead with?': 'AppImage' }, skipped: []
		});
	});

	it('takes a typed answer', async () => {
		const { onQuestion } = renderDock({ permissions: [], questions: [question] });
		await fireEvent.click(screen.getByRole('button', { name: 'Type an answer' }));
		const input = screen.getByRole('textbox', { name: 'Your answer' });
		await fireEvent.input(input, { target: { value: 'Both, AppImage first' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Send answer' }));
		expect(onQuestion).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({
			answers: { 'Which install format should the guide lead with?': 'Both, AppImage first' }
		}));
	});

	it('uses the shared question form for a multi-part request', () => {
		renderDock({ permissions: [], questions: [{ ...question, questions: [question.questions[0], { ...question.questions[0], question: 'Second?' }] }] });
		expect(screen.getByRole('form', { name: 'Answer questions' })).toBeTruthy();
	});

	it('says to reconnect and disables answers while disconnected', async () => {
		renderDock({ permissions: [], questions: [question], connected: false });
		expect(screen.getByText('Reconnect to answer')).toBeTruthy();
		await armed();
		expect((screen.getByRole('button', { name: /AppImage/ }) as HTMLButtonElement).disabled).toBe(true);
	});
});

describe('private access', () => {
	function access(items: { id: string; kind: 'action' | 'folder'; detail: string }[]) {
		const decide = vi.fn().mockResolvedValue(true);
		const open = vi.fn();
		chatAccessSummary.set({ sessionId: 's1', pending: items.length, items, active: 0, saved: 0, error: false, open, decide });
		return { decide, open };
	}

	it('answers a one-shot approval by sliding', async () => {
		const { decide } = access([{ id: 'a1', kind: 'action', detail: '$ ls /mnt/user/appdata' }]);
		renderDock({ permissions: [] });
		expect(screen.getByText('$ ls /mnt/user/appdata')).toBeTruthy();
		await slide(160);
		await waitFor(() => expect(decide).toHaveBeenCalledExactlyOnceWith('a1', true));
	});

	it('sends a request that needs private inputs to the access dialog', async () => {
		const { open, decide } = access([{ id: 'a2', kind: 'folder', detail: 'Compare the Linux paths' }]);
		renderDock({ permissions: [] });
		expect(screen.queryByRole('slider')).toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: 'Review' }));
		expect(open).toHaveBeenCalledOnce();
		expect(decide).not.toHaveBeenCalled();
	});

	it('ignores another chat\'s requests', () => {
		access([{ id: 'a1', kind: 'action', detail: '$ ls' }]);
		renderDock({ permissions: [], sessionId: 'other' });
		expect(screen.queryByRole('region', { name: 'Waiting on you' })).toBeNull();
	});
});

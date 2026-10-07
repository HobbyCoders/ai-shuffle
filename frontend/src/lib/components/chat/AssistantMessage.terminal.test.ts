import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';

const api = vi.hoisted(() => ({
	list: vi.fn(), targets: vi.fn(), create: vi.fn(), get: vi.fn(), close: vi.fn(), restart: vi.fn(), paste: vi.fn(),
}));
vi.mock('$lib/api/terminals', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/api/terminals')>();
	return { ...actual, terminalsApi: api };
});

import AssistantMessage from './AssistantMessage.svelte';
import { TerminalApiError, type TerminalTargets } from '$lib/api/terminals';
import { terminals } from '$lib/stores/terminals.svelte';

const targets: TerminalTargets = {
	worker: { id: 'w', name: 'Unraid', platform: 'linux' }, supported: true, unsupported_reason: null, targets: [],
	shells: [{ name: 'bash', default: true }], default_shell: 'bash',
	default_folder: { path: '/workspace/app', source: 'project' }, limit: 8, open: 0,
};
const reply = 'Run this:\n\n```bash\nnpm test\n```\n';

beforeEach(() => {
	terminals.reset();
	Object.values(api).forEach((fn) => fn.mockReset());
});
afterEach(() => {
	cleanup();
	vi.restoreAllMocks();
});

function head(view: { container: HTMLElement }) {
	return view.container.querySelector('.codeblock .codehead') as HTMLElement;
}

describe('command block terminal buttons', () => {
	it('puts Open terminal and Open and paste before Copy for an owner', async () => {
		api.targets.mockResolvedValue(targets);
		const view = render(AssistantMessage, { content: reply, streaming: false, sessionId: 's1' });
		await waitFor(() => expect(head(view).querySelectorAll('.code-run-btn')).toHaveLength(2));
		const order = Array.from(head(view).querySelectorAll('button')).map((b) => b.getAttribute('aria-label') ?? b.className);
		const copyAt = order.findIndex((label) => /copy/i.test(label));
		expect(order.indexOf('Open terminal')).toBeLessThan(copyAt);
		expect(order.indexOf('Open and paste')).toBeLessThan(copyAt);
		expect(head(view).querySelector('.cb-target')?.textContent).toBe('Unraid · app · bash');
	});

	it('queues the registry command for the panel when clicked, and runs nothing itself', async () => {
		api.targets.mockResolvedValue(targets);
		const request = vi.spyOn(terminals, 'request');
		const view = render(AssistantMessage, { content: reply, streaming: false, sessionId: 's1' });
		await waitFor(() => expect(head(view).querySelector('[data-act="paste"]')).toBeTruthy());
		await fireEvent.click(head(view).querySelector('[data-act="paste"]')!);
		expect(request).toHaveBeenCalledWith('s1', 'paste', expect.objectContaining({ command: 'npm test' }), head(view).querySelector('[data-act="paste"]'));
		expect(api.paste).not.toHaveBeenCalled();
		expect(api.create).not.toHaveBeenCalled();
		request.mockRestore();
	});

	it('leaves Copy only for a viewer the worker refuses', async () => {
		api.targets.mockRejectedValue(new TerminalApiError('no', 403, 'http_403'));
		const view = render(AssistantMessage, { content: reply, streaming: false, sessionId: 's1' });
		await waitFor(() => expect(terminals.probeFor('s1').status).toBe('denied'));
		expect(head(view).querySelector('.code-run-btn')).toBeNull();
		expect(head(view).querySelector('.code-copy-btn')).toBeTruthy();
	});

	it('shows disabled buttons with the reason for a chat on another worker', async () => {
		api.targets.mockRejectedValue(new TerminalApiError('That chat runs on DesktopWorker.', 409, 'chat_on_other_worker'));
		const view = render(AssistantMessage, { content: reply, streaming: false, sessionId: 's1' });
		await waitFor(() => expect(head(view).querySelectorAll('.code-run-btn[aria-disabled="true"]')).toHaveLength(2));
		const request = vi.spyOn(terminals, 'request');
		await fireEvent.click(head(view).querySelector('[data-act="open"]')!);
		expect(request).not.toHaveBeenCalled();
		expect(head(view).querySelector('[data-act="open"]')?.getAttribute('title')).toBe('That chat runs on DesktopWorker.');
		request.mockRestore();
	});

	it('adds nothing while the reply streams', async () => {
		api.targets.mockResolvedValue(targets);
		const view = render(AssistantMessage, { content: reply, streaming: true, sessionId: 's1' });
		await new Promise((r) => setTimeout(r, 20));
		expect(view.container.querySelector('.code-run-btn')).toBeNull();
		expect(api.targets).not.toHaveBeenCalled();
	});

	it('shows a fence folder outside the chat folder in full, flagged, and sends exactly that', async () => {
		api.targets.mockResolvedValue(targets);
		const request = vi.spyOn(terminals, 'request');
		const content = 'Run this:\n\n```bash cwd="/mnt/user/appdata/ai-shuffle"\nrm -rf build\n```\n';
		const view = render(AssistantMessage, { content, streaming: false, sessionId: 's1' });
		await waitFor(() => expect(view.container.querySelector('.cb-where')).toBeTruthy());
		const row = view.container.querySelector('.cb-where') as HTMLElement;
		expect(row.classList.contains('outside')).toBe(true);
		expect(row.querySelector('.cb-where-path')?.textContent).toBe('/mnt/user/appdata/ai-shuffle');
		expect(row.querySelector('.cb-where-flag')?.textContent).toBe("Outside this chat's folder");
		expect(row.querySelector('.cb-where-meta')?.textContent).toBe('Unraid · bash');
		await fireEvent.click(head(view).querySelector('[data-act="paste"]')!);
		expect(request).toHaveBeenCalledWith('s1', 'paste', expect.objectContaining({ command: 'rm -rf build', cwd: '/mnt/user/appdata/ai-shuffle' }), expect.anything());
		request.mockRestore();
	});

	it('marks a fence folder inside the chat folder without the flag', async () => {
		api.targets.mockResolvedValue(targets);
		const content = '```bash cwd="/workspace/app/web"\nnpm test\n```\n';
		const view = render(AssistantMessage, { content, streaming: false, sessionId: 's1' });
		await waitFor(() => expect(view.container.querySelector('.cb-where')).toBeTruthy());
		const row = view.container.querySelector('.cb-where') as HTMLElement;
		expect(row.classList.contains('outside')).toBe(false);
		expect(row.querySelector('.cb-where-path')?.textContent).toBe('/workspace/app/web');
		expect(row.querySelector('.cb-where-flag')).toBeNull();
	});

	it('shows a long fence path whole, never shortened in the middle', async () => {
		api.targets.mockResolvedValue(targets);
		const alpha = '/workspace/app/projects/ALPHA/services/api/scripts/migrations/build';
		const content = '```bash cwd="' + alpha + '"\nls\n```\n\n```bash cwd="' + alpha.replace('ALPHA', 'BETA') + '"\nls\n```\n';
		const view = render(AssistantMessage, { content, streaming: false, sessionId: 's1' });
		await waitFor(() => expect(view.container.querySelectorAll('.cb-where')).toHaveLength(2));
		const paths = Array.from(view.container.querySelectorAll('.cb-where-path')).map((el) => el.textContent);
		expect(paths).toEqual([alpha, alpha.replace('ALPHA', 'BETA')]);
	});

	it('flags a folder with unusual characters and shows them as code points', async () => {
		api.targets.mockResolvedValue({ ...targets, default_folder: { path: '/workspace', source: 'project' } });
		const request = vi.spyOn(terminals, 'request');
		const cwd = '/workspace/\u0441onfig\u00a0x'; // a Cyrillic "с" and a no-break space
		const content = '```bash cwd="' + cwd + '"\nls\n```\n';
		const view = render(AssistantMessage, { content, streaming: false, sessionId: 's1' });
		await waitFor(() => expect(view.container.querySelector('.cb-where')).toBeTruthy());
		const row = view.container.querySelector('.cb-where') as HTMLElement;
		expect(row.classList.contains('unusual')).toBe(true);
		expect(row.classList.contains('outside')).toBe(false);
		expect(row.querySelector('.cb-where-path')?.textContent).toBe('/workspace/U+0441onfigU+00A0x');
		expect(Array.from(row.querySelectorAll('.cb-where-cp')).map((el) => el.textContent)).toEqual(['U+0441', 'U+00A0']);
		expect(row.textContent).not.toContain('\u0441');
		expect(Array.from(row.querySelectorAll('.cb-where-flag')).map((el) => el.textContent)).toEqual(['Unusual characters in this folder name']);
		await fireEvent.click(head(view).querySelector('[data-act="open"]')!);
		expect(request).toHaveBeenCalledWith('s1', 'open', expect.objectContaining({ cwd }), expect.anything());
		request.mockRestore();
	});

	for (const [name, cwd] of [['a .. part', '/etc/..'], ['a text-direction character', '/tmp/\u202eppa'],
		['a zero-width space', '/workspace/a\u200bb'], ['a byte-order mark', '/workspace/\ufeffapp'], ['a soft hyphen', '/workspace/ap\u00adp'],
		['an Arabic letter mark', '/workspace/\u061capp'], ['a line separator', '/workspace/a\u2028b']] as const) {
		it(`disables both buttons for a folder with ${name}, shows the reason and not the path`, async () => {
			api.targets.mockResolvedValue(targets);
			const request = vi.spyOn(terminals, 'request');
			const content = '```bash cwd="' + cwd + '"\nls\n```\n';
			const view = render(AssistantMessage, { content, streaming: false, sessionId: 's1' });
			await waitFor(() => expect(head(view).querySelectorAll('.code-run-btn[aria-disabled="true"]')).toHaveLength(2));
			const row = view.container.querySelector('.cb-where.refused') as HTMLElement;
			expect(row).toBeTruthy();
			expect(row.textContent).not.toContain(cwd);
			expect(row.querySelector('.cb-where-reason')?.textContent).toMatch(/\. or \.\.|hidden or control/);
			await fireEvent.click(head(view).querySelector('[data-act="paste"]')!);
			expect(request).not.toHaveBeenCalled();
			request.mockRestore();
		});
	}
});

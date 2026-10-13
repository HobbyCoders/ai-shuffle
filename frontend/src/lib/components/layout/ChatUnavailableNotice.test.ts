import { describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';
import ChatUnavailableNotice from './ChatUnavailableNotice.svelte';

describe('ChatUnavailableNotice', () => {
	it('names the problem, shows the link id and offers a way back', async () => {
		const onDismiss = vi.fn();
		render(ChatUnavailableNotice, {
			props: { chat: { sessionId: 'c671567c', status: 404 }, onDismiss }
		});
		expect(screen.getByRole('alert')).toBeTruthy();
		expect(screen.getByRole('heading', { name: 'Chat not found' })).toBeTruthy();
		expect(screen.getByText('c671567c')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Start a new chat' }));
		expect(onDismiss).toHaveBeenCalledTimes(1);
	});

	it('explains a chat this viewer may not open', () => {
		render(ChatUnavailableNotice, {
			props: { chat: { sessionId: 'abc', status: 403 }, onDismiss: () => {} }
		});
		expect(screen.getByRole('heading', { name: /can’t open this chat/ })).toBeTruthy();
	});
});

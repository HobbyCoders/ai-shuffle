import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
import { get } from 'svelte/store';
import ChatInput from '$lib/components/chat/ChatInput.svelte';
import { activeSession } from '$lib/stores/active-session';
import { auth } from '$lib/stores/auth.svelte';
import { platform } from '$lib/stores/platform.svelte';
import { cancelVoiceInput, globalSttState } from '$lib/services/globalStt';
import '$lib/test-stubs/composer-dom';
import { installMobileShell } from './register';

vi.hoisted(() => {
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
		const url = String(input);
		const data = url.includes('/auth/status')
			? { authenticated: true, has_any_credential: true, onboarding_complete: true }
			: url.includes('/commands') ? { commands: [] } : [];
		return new Response(JSON.stringify(data), { status: 200 });
	}));
});
const stt = vi.hoisted(() => ({ capability: vi.fn() }));
vi.mock('$lib/services/sttStream', () => ({
	// Hold real globalStt in preparation, as while asking for microphone access.
	fetchSttCapability: stt.capability,
	startSttStream: vi.fn(),
	SttUnsupportedError: class extends Error {}
}));
vi.mock('$lib/utils/earcon', () => ({ playEarcon: vi.fn(), primeEarcons: vi.fn(), setEarconsEnabled: vi.fn() }));

let finishCapability: () => void;
beforeEach(async () => {
	stt.capability.mockImplementation(() => new Promise(resolve => {
		finishCapability = () => resolve({ supported: false });
	}));
	installMobileShell();
	platform.set('mobile');
	sessionStorage.clear();
	activeSession.newChat();
	Object.assign(activeSession.current, { sessionId: 'composer-toolbar-test', mode: 'chat', profile: 'test-profile', draft: '' });
	await auth.checkAuth();
});
afterEach(async () => {
	cancelVoiceInput();
	finishCapability?.();
	await settle();
	cleanup();
	vi.restoreAllMocks();
});

async function settle() {
	await tick();
	await new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
	await tick();
}
async function setup() {
	const { container } = render(ChatInput, { props: { compact: true } });
	const editor = container.querySelector<HTMLElement>('.tiptap')!;
	await fireEvent.pointerDown(editor);
	editor.focus();
	await fireEvent.pointerUp(editor);
	await settle();
	const wrapper = container.querySelector('.chat-input-wrapper')!;
	expect(wrapper.classList.contains('mobile-collapsed')).toBe(false);
	return { editor, wrapper };
}

describe('mobile toolbar interaction ownership', () => {
	it.each(['touch', 'keyboard'])('keeps voice preparation visible after %s activation loses focus', async method => {
		const { wrapper } = await setup();
		const mic = screen.getByRole('button', { name: 'Start voice input' }) as HTMLButtonElement;
		if (method === 'touch') await fireEvent.pointerDown(mic);
		mic.focus();
		await fireEvent.click(mic);
		expect(get(globalSttState).isPreparing).toBe(true);
		expect(mic.hasAttribute('disabled')).toBe(true);
		// jsdom neither blurs on disable nor allows blur() on a disabled button.
		// Reproduce the browser's focus loss without changing the recorder state.
		mic.disabled = false;
		mic.blur();
		mic.disabled = true;
		await settle();
		expect(document.activeElement).toBe(document.body);
		expect(wrapper.classList.contains('mobile-collapsed')).toBe(false);
		cancelVoiceInput();
		await settle();
		expect(wrapper.classList.contains('mobile-collapsed')).toBe(false);
		await fireEvent.pointerDown(document.body);
		await settle();
		expect(wrapper.classList.contains('mobile-collapsed')).toBe(true);
	});

	it('keeps a non-focusing toolbar tap open until a keyboard move outside', async () => {
		const { editor, wrapper } = await setup();
		const attach = screen.getByRole('button', { name: 'Attach file' });
		await fireEvent.pointerDown(attach);
		editor.blur();
		await fireEvent.click(attach);
		await settle();
		expect(document.activeElement).toBe(document.body);
		expect(wrapper.classList.contains('mobile-collapsed')).toBe(false);
		const outside = document.createElement('button');
		document.body.append(outside);
		outside.focus();
		await settle();
		expect(wrapper.classList.contains('mobile-collapsed')).toBe(true);
		outside.remove();
	});

	it('still collapses after editor-only blur', async () => {
		const { editor, wrapper } = await setup();
		editor.blur();
		await settle();
		expect(wrapper.classList.contains('mobile-collapsed')).toBe(true);
	});

	it('blurs a focused toolbar button on a non-focusable outside tap', async () => {
		const { wrapper } = await setup();
		const mic = screen.getByRole('button', { name: 'Start voice input' }) as HTMLButtonElement;
		await fireEvent.pointerDown(mic);
		mic.focus();
		await fireEvent.pointerDown(document.body);
		await settle();
		expect(document.activeElement).not.toBe(mic);
		expect(wrapper.classList.contains('mobile-collapsed')).toBe(true);
	});
});

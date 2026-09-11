import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { createRawSnippet, tick } from 'svelte';
import { get } from 'svelte/store';
import { activeSession } from '$lib/stores/active-session';
import { appMode, type SessionMode } from '$lib/stores/sessionMode.svelte';
import ControlsDock from './ControlsDock.svelte';
import { loadVoiceSettings } from '$lib/api/voice';
import '../../test-stubs/composer-dom';
import { api, getUsageLimits } from '$lib/api/client';
import { getModels, hydrateModelRegistry } from '$lib/constants/models';

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

// The dock reads its lane off `$activeSession.mode`, and `newChat()` is what
// stamps the sidepanel's lane onto a fresh compose surface — so it is also how
// a test puts the dock in one. Every test starts (and leaves) the code lane.
function setLane(mode: SessionMode) {
	appMode.set(mode);
	activeSession.newChat();
}

beforeEach(async () => {
 setLane('code');
 const registryGet = vi.spyOn(api, 'get').mockResolvedValue({ models: getModels(), providers: [], claude: { auth_kind: 'subscription', plan_type: null } });
 await hydrateModelRegistry();
 registryGet.mockRestore();
});
afterEach(() => {
	cleanup();
	setLane('code');
});
afterAll(() => vi.unstubAllGlobals());

function renderDock(props: Record<string, unknown> = {}) {
	return render(ControlsDock, {
		props: {
			isUploading: false,
			onAttachClick: vi.fn(),
			...props
		}
	});
}

const voiceActions = createRawSnippet(() => ({
	render: () => `<span data-testid="voice-snippet">voice</span>`
}));

describe('ControlsDock split controls', () => {
 it.each(['gpt-realtime-2.1', 'fal-ai/personaplex/realtime'])('locks the model block for %s', async model => {
  const request = vi.spyOn(api, 'get').mockResolvedValue({ voice_model: model });
  await loadVoiceSettings(true);
  setLane('voice');
  renderDock();
  const label = model.startsWith('fal-') ? 'PersonaPlex' : 'GPT Realtime 2.1';
  await waitFor(() => expect((screen.getByRole('button', { name: `Model: ${label}` }) as HTMLButtonElement).disabled).toBe(true));
  await fireEvent.click(screen.getByRole('button', { name: `Model: ${label}` }));
  expect(screen.queryByRole('listbox')).toBeNull();
  request.mockRestore();
 });
 it('locks Responses delegation to its saved backend', async () => {
  const request = vi.spyOn(api, 'get').mockResolvedValue({voice_model:'gpt-live-1',voice_options:{delegation_type:'responses'}});
  await loadVoiceSettings(true); setLane('voice'); renderDock();
  await waitFor(() => expect((screen.getByRole('button',{name:'Model: GPT Live 1'}) as HTMLButtonElement).disabled).toBe(true));
  request.mockRestore();
 });
 it('does not replace the composer with a retired voice delegation default', async () => {
  const model=getModels()[0].value;
  const request=vi.spyOn(api,'get').mockResolvedValue({voice_model:'gpt-live-1',voice_options:{delegation_type:'client',delegation_model:model}});
  await loadVoiceSettings(true);setLane('voice');activeSession.setModelOverride('composer-selection');renderDock();
  await waitFor(()=>expect((screen.getByRole('button', {name:/^Model:/}) as HTMLButtonElement).disabled).toBe(false));
  expect(get(activeSession).modelOverride).toBe('composer-selection');
  request.mockRestore();
 });
 it('keeps the selected delegation model editable for GPT Live 1', async () => {
  const request = vi.spyOn(api, 'get').mockResolvedValue({ voice_model: 'gpt-live-1' });
  await loadVoiceSettings(true);
  setLane('voice');
  renderDock();
  await waitFor(() => expect((screen.getByRole('button', { name: /^Model:/ }) as HTMLButtonElement).disabled).toBe(false));
  expect(screen.queryByRole('button', { name: 'Model: GPT Live 1' })).toBeNull();
  request.mockRestore();
 });
 it('does not label a fresh provider-only composer with the bundled Sonnet default', async () => {
  const registryGet = vi.spyOn(api, 'get').mockResolvedValue({ models: getModels(), providers: [], claude: { auth_kind: null, plan_type: null } });
  await hydrateModelRegistry();
  registryGet.mockRestore();
  activeSession.setModelOverride(null);
  renderDock();
  expect(screen.getByRole('button', { name: 'Model: Select model' })).toBeTruthy();
  await fireEvent.click(screen.getByRole('button', { name: 'Model: Select model' }));
  expect(screen.queryByRole('tab', { name: /Claude/ })).toBeNull();
  expect(screen.queryByText('Sonnet')).toBeNull();
  expect(screen.queryByText('Extended')).toBeNull();
 });

	it('hides the browser control without an eligible-session toggle', () => {
		renderDock({ browserOpen: true });
		expect(screen.queryByRole('button', { name: 'Toggle live browser view' })).toBeNull();
	});

	it('forwards toggle and preload events and reflects the owned open state', async () => {
		const onBrowserToggle = vi.fn();
		const onBrowserPreload = vi.fn();
		const { rerender } = renderDock({ onBrowserToggle, onBrowserPreload });
		const browser = screen.getByRole('button', { name: 'Toggle live browser view' });
		expect(browser.getAttribute('aria-pressed')).toBe('false');
		expect(browser.classList.contains('active')).toBe(false);
		expect(browser.getAttribute('title')).toBe('Toggle live browser view');
		expect(browser.getAttribute('type')).toBe('button');
		await fireEvent.pointerEnter(browser);
		await fireEvent.focus(browser);
		expect(onBrowserPreload).toHaveBeenCalledTimes(2);
		expect(onBrowserToggle).not.toHaveBeenCalled();
		await fireEvent.click(browser);
		expect(onBrowserToggle).toHaveBeenCalledTimes(1);
		expect(browser.getAttribute('aria-pressed')).toBe('false');
		await rerender({ isUploading: false, onAttachClick: vi.fn(), onBrowserToggle, onBrowserPreload, browserOpen: true });
		expect(browser.getAttribute('aria-pressed')).toBe('true');
		expect(browser.classList.contains('active')).toBe(true);
	});

	it('splits the old summary chip into permission, model and effort controls', () => {
		const { container } = renderDock();

		expect(container.querySelector('.controls-summary-chip')).toBeNull();
		expect(screen.getByRole('button', { name: /^permission mode:/i })).toBeTruthy();
		expect(screen.getByRole('button', { name: /^model:/i })).toBeTruthy();
		expect(screen.getByRole('button', { name: /^effort:/i })).toBeTruthy();
		expect(screen.getByRole('button', { name: /attach file/i })).toBeTruthy();
		expect(screen.queryByRole('button', { name: /more input options/i })).toBeNull();
	});

	it('orders the row permission · attach on the left, model · effort · ring on the right', () => {
		const { container } = renderDock();

		const left = container.querySelector('.dock-left');
		const right = container.querySelector('.dock-right');
		const permission = screen.getByRole('button', { name: /^permission mode:/i });
		const attach = screen.getByRole('button', { name: /attach file/i });
		expect(left?.contains(permission)).toBe(true);
		expect(left?.contains(attach)).toBe(true);
		expect(permission.compareDocumentPosition(attach) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
		expect(right?.contains(screen.getByRole('button', { name: /^model:/i }))).toBe(true);
		expect(right?.contains(screen.getByRole('button', { name: /^effort:/i }))).toBe(true);
		expect(right?.querySelector('.usage-context-wrapper')).toBeTruthy();
		expect(left && right && (left.compareDocumentPosition(right) & Node.DOCUMENT_POSITION_FOLLOWING))
			.toBeTruthy();
	});

	it('places the Globe between voice actions and attach in the left cluster', () => {
		const { container } = renderDock({ voiceActions, onBrowserToggle: vi.fn() });

		const left = container.querySelector('.dock-left');
		const voice = left?.querySelector('[data-testid="voice-snippet"]');
		const attach = screen.getByRole('button', { name: /attach file/i });
		expect(voice).toBeInstanceOf(Element);
		expect(left?.contains(attach)).toBe(true);
		if (!(voice instanceof Element)) return;
		const browser = screen.getByRole('button', { name: 'Toggle live browser view' });
		expect(voice.nextElementSibling).toBe(browser);
		expect(browser.nextElementSibling).toBe(attach);
		expect(browser.querySelector('svg.lucide-globe')).toBeTruthy();
		expect(browser.querySelector('svg')?.getAttribute('width')).toBe('14');
		expect(browser.querySelector('svg')?.getAttribute('stroke-width')).toBe('1.8');
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
		// Ask, Accept Edits, Bypass. Auto needs a model that supports it, and the
		// dedicated planning mode was removed.
		expect(menu.querySelectorAll('[role="menuitemradio"]')).toHaveLength(3);
		expect(screen.queryByRole('menuitemradio', { name: /^auto\b/i })).toBeNull();
		expect(screen.queryByRole('menuitemradio', { name: /plan/i })).toBeNull();
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

	it('opens the model picker from the model text', async () => {
		renderDock();

		await fireEvent.click(screen.getByRole('button', { name: /^model:/i }));

		const dialog = screen.getByRole('dialog', { name: /choose model/i });
		expect(dialog.querySelector('[data-model-picker-panel]')).toBeTruthy();
	});

	it('pins context size selection in the desktop model picker', async () => {
		renderDock();

		await fireEvent.click(screen.getByRole('button', { name: /^model:/i }));

		const dialog = screen.getByRole('dialog', { name: /choose model/i });
		expect(dialog.querySelector('[data-model-context-control]')).toBeTruthy();
		expect(screen.getByRole('radiogroup', { name: /context window/i })).toBeTruthy();
		expect(screen.getByRole('radio', { name: /1m.*extended/i }).getAttribute('aria-checked')).toBe(
			'true'
		);

		await fireEvent.click(screen.getByRole('radio', { name: /200k.*standard/i }));
		expect(screen.getByRole('dialog', { name: /choose model/i })).toBeTruthy();
		expect(screen.getByRole('radio', { name: /200k.*standard/i }).getAttribute('aria-checked')).toBe(
			'true'
		);
	});

	it('keeps the model picker open after selecting a model so context stays reachable', async () => {
		const registryGet = vi.spyOn(api, 'get').mockResolvedValue({ models: getModels(), providers: [], claude: { auth_kind: 'subscription', plan_type: null } });
		await hydrateModelRegistry();
		registryGet.mockRestore();
		renderDock();

		await fireEvent.click(screen.getByRole('button', { name: /^model:/i }));
		expect(screen.getByRole('dialog', { name: /choose model/i })).toBeTruthy();

		await fireEvent.click(screen.getByRole('option', { name: /opus/i }));
		expect(get(activeSession).modelOverride).toBe('opus');
		expect(screen.getByRole('dialog', { name: /choose model/i })).toBeTruthy();
		expect(screen.getByRole('radiogroup', { name: /context window/i })).toBeTruthy();

		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(screen.queryByRole('dialog', { name: /choose model/i })).toBeNull();
	});

	it('drops the tool-access control in the chat lane but keeps attach', () => {
		setLane('chat');
		const { container } = renderDock({ voiceActions });

		// A chat session's tool surface is fixed server-side, so there is nothing
		// for a permission menu to grant.
		expect(screen.queryByRole('button', { name: /^permission mode:/i })).toBeNull();
		// Attach survives: chat uploads land in the workspace's shared
		// chat-uploads dir, and the chat ceiling carries a Read scoped to it.
		const attach = screen.getByRole('button', { name: /attach file/i });
		expect(attach).toBeTruthy();
		// Not captioned with the project advice — a projectless chat is the
		// working state here, not a misconfiguration. (Whether the button is
		// enabled depends on credentials, which this harness has none of.)
		expect(attach.getAttribute('title')).toBe('Attach file');
		// Everything else the chat composer keeps.
		expect(screen.getByRole('button', { name: /^model:/i })).toBeTruthy();
		expect(screen.getByRole('button', { name: /^effort:/i })).toBeTruthy();
		expect(container.querySelector('[data-testid="voice-snippet"]')).toBeTruthy();
		expect(container.querySelector('.usage-context-wrapper')).toBeTruthy();
		expect(container.querySelector('.mobile-settings-trigger')).toBeTruthy();
	});

	it('keeps attach and tool access in the voice lane', () => {
		setLane('voice');
		renderDock({ voiceActions });

		expect(screen.getByRole('button', { name: /attach file/i })).toBeTruthy();
		expect(screen.getByRole('button', { name: /^permission mode:/i })).toBeTruthy();
	});


	describe('provider refresh polling', () => {
		beforeEach(() => { vi.useFakeTimers(); vi.mocked(getUsageLimits).mockReset(); });
		afterEach(() => { vi.useRealTimers(); });
		const snapshot = (refreshing: boolean) => ({ windows: [], available: false, provider_refreshing: refreshing });
		async function openUsage() {
			const view = renderDock();
			await tick();
			await fireEvent.click(view.container.querySelector('.usage-indicator')!);
			await vi.advanceTimersByTimeAsync(0);
			return view;
		}
		it('forces once after mount, reads progressive results and stops at terminal', async () => {
			vi.mocked(getUsageLimits).mockResolvedValueOnce(snapshot(false)).mockResolvedValueOnce(snapshot(true)).mockResolvedValueOnce(snapshot(true)).mockResolvedValue(snapshot(false));
			await openUsage();
			await vi.advanceTimersByTimeAsync(5000);
			expect(vi.mocked(getUsageLimits).mock.calls.map(([force]) => force)).toEqual([false, true, false, false]);
		});
		it('continues polling a still-pending refresh after 70 seconds', async () => {
			vi.mocked(getUsageLimits).mockResolvedValue(snapshot(true));
			await openUsage();
			await vi.advanceTimersByTimeAsync(70_000);
			const count = vi.mocked(getUsageLimits).mock.calls.length;
			await vi.advanceTimersByTimeAsync(1_000);
			expect(getUsageLimits).toHaveBeenCalledTimes(count + 1);
			expect(getUsageLimits).toHaveBeenLastCalledWith(false);
		});
		it.each(['close', 'destroy', 'deadline'])('stops polling on %s', async (stop) => {
			vi.mocked(getUsageLimits).mockResolvedValue(snapshot(true));
			const view = await openUsage();
			if (stop === 'close') await fireEvent.click(view.container.querySelector('.usage-indicator')!);
			if (stop === 'destroy') view.unmount();
			if (stop === 'deadline') await vi.advanceTimersByTimeAsync(120_000);
			const count = vi.mocked(getUsageLimits).mock.calls.length;
			await vi.advanceTimersByTimeAsync(5000);
			expect(getUsageLimits).toHaveBeenCalledTimes(count);
		});
		it('does not swallow a forced open behind a pending mount request', async () => {
			let resolve!: (value: ReturnType<typeof snapshot>) => void;
			vi.mocked(getUsageLimits).mockReturnValueOnce(new Promise((r) => { resolve = r; })).mockResolvedValue(snapshot(false));
			await openUsage();
			expect(getUsageLimits).toHaveBeenCalledTimes(1);
			resolve(snapshot(false));
			await vi.advanceTimersByTimeAsync(0);
			expect(vi.mocked(getUsageLimits).mock.calls.map(([force]) => force)).toEqual([false, true]);
		});
	});

	it('does not wedge the dock when the lane flips with the access menu open', async () => {
		renderDock();
		await fireEvent.click(screen.getByRole('button', { name: /^permission mode:/i }));
		expect(screen.getByRole('menu', { name: /tool access/i })).toBeTruthy();

		setLane('chat');
		await tick();

		expect(screen.queryByRole('menu', { name: /tool access/i })).toBeNull();
		// The stranded popover would otherwise keep `openPopover` pinned to a
		// trigger that no longer exists, and no outside click could clear it.
		await fireEvent.click(screen.getByRole('button', { name: /^effort:/i }));
		expect(screen.getByRole('dialog', { name: /reasoning effort/i })).toBeTruthy();
	});
});

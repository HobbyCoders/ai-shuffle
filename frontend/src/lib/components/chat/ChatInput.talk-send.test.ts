/**
 * The composer's circles: attach left of the text; dictate, then one slot
 * that is Talk while there is nothing to send and Send once there is, then
 * Stop while work runs. Attach, dictate and the call are not in the controls
 * row any more.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import { activeSession } from '$lib/stores/active-session';
import { appMode } from '$lib/stores/sessionMode.svelte';
import { ATTACHMENTS_NEED_LOCAL_CHECKOUT } from './attach-disable';
import { auth } from '$lib/stores/auth.svelte';
import { initialState, reduce } from '$lib/stores/session/v2/reducer';
import { bumpInitGeneration } from '$lib/stores/session/session-init';
import '$lib/test-stubs/composer-dom';
import { get } from 'svelte/store';
import { voiceCallState } from '$lib/services/voiceCall';

const voice = vi.hoisted(() => ({ enabled: true }));
vi.hoisted(() => {
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
		const url = String(input);
		const data = url.includes('/auth/status')
			? { authenticated: true, has_any_credential: true, onboarding_complete: true }
			: url.includes('/commands') ? { commands: [] } : [];
		return new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } });
	}));
});
vi.mock('$lib/api/voice', async (importOriginal) => {
	const actual = await importOriginal<Record<string, unknown>>();
	return { ...actual, isVoiceEnabled: vi.fn(async () => voice.enabled) };
});

import ChatInput from './ChatInput.svelte';
import VoiceCallButton from './VoiceCallButton.svelte';

Object.defineProperties(Range.prototype, {
	getClientRects: { configurable: true, value: () => [] },
	getBoundingClientRect: { configurable: true, value: () => new DOMRect() }
});

beforeEach(async () => {
	voice.enabled = true;
	sessionStorage.clear();
	activeSession.newChat();
	activeSession.setDraft('');
	await auth.checkAuth();
});
afterEach(() => {
	cleanup();
	vi.restoreAllMocks();
});

/** The visible circles after dictate, left to right, by accessible name. */
function circles(container: HTMLElement): string[] {
	const cluster = container.querySelector('.send-cluster')!;
	return [...cluster.querySelectorAll<HTMLButtonElement>('button')]
		.filter((b) => !b.closest('[hidden]') && !b.closest('.stash'))
		.map((b) => b.getAttribute('aria-label') ?? '');
}

async function type(container: HTMLElement, text: string) {
	const editor = container.querySelector<HTMLElement>('.tiptap')!;
	editor.focus();
	editor.innerHTML = `<p>${text}</p>`;
	await fireEvent.input(editor, { inputType: 'insertText', data: text.slice(-1) });
	await tick();
}

function startTurn(sessionId = 'local-working') {
	Object.assign(activeSession.current, { sessionId, mode: 'code', profile: 'coding', draft: '' });
	const live = reduce(initialState(), { type: 'turn.started', id: 'started', seq: 1,
		session_id: sessionId, turn_id: 'live-turn' });
	Object.assign(activeSession.current, { isStreaming: live.isStreaming, liveTurnId: live.liveTurnId });
	activeSession.setDraft('');
}

describe('Talk turns into Send', () => {
	it('shows Talk, not Send, in an empty composer, and Send once there is text', async () => {
		const { container } = render(ChatInput);
		await waitFor(() => expect(circles(container)).toEqual(['Start voice input', 'Start a call in a new conversation']));
		expect(screen.queryByRole('button', { name: 'Send' })).toBeNull();

		await type(container, 'Fix the flaky test');
		await waitFor(() => expect(circles(container)).toEqual(['Start voice input', 'Send']));
		expect((screen.getByRole('button', { name: 'Send' }) as HTMLButtonElement).disabled).toBe(false);
		// Talk stays mounted (hidden), so a keystroke never tears down a starting call.
		expect(container.querySelector('.talk-slot[hidden] .call-circle')).toBeTruthy();

		await type(container, '');
		await waitFor(() => expect(circles(container)).toEqual(['Start voice input', 'Start a call in a new conversation']));
	});

	it('falls back to Send where no call can start', async () => {
		voice.enabled = false;
		const { container } = render(ChatInput);
		await tick();
		await waitFor(() => expect(circles(container)).toEqual(['Start voice input', 'Send']));
		expect((screen.getByRole('button', { name: 'Send' }) as HTMLButtonElement).disabled).toBe(true);
	});

	it('keeps Stop as the last circle while work runs: Talk with no draft, Send with one', async () => {
		startTurn();
		const { container } = render(ChatInput);
		await waitFor(() => expect(circles(container)).toEqual([
			'Start voice input', 'Start a call in this conversation', 'Stop generating'
		]));

		await type(container, 'Also update the docs');
		await waitFor(() => expect(circles(container)).toEqual(['Start voice input', 'Send', 'Stop generating']));
	});

	it('holds no empty seat for Send or Stop when it is not shown', async () => {
		// Owner decision (2026-11-14): a circle that is not shown takes no room;
		// the others move over. Working with nothing to send and no call: mic · Stop.
		voice.enabled = false;
		startTurn();
		const { container } = render(ChatInput);
		await waitFor(() => expect(circles(container)).toEqual(['Start voice input', 'Stop generating']));
		const cluster = container.querySelector('.send-cluster')!;
		const empty = [...cluster.children]
			.filter((el) => !el.matches('[hidden], .stash') && !el.matches('button') && !el.querySelector('button'))
			.map((el) => el.outerHTML.slice(0, 80));
		expect(empty).toEqual([]);
	});

	it('keeps Send in its seat through a double click after the composer clears', async () => {
		// Send clears the composer at the click; Talk taking the seat at once
		// would put a call under the second click of a double click.
		Object.assign(activeSession.current, { sessionId: 'local-idle', mode: 'code', profile: 'coding',
			isStreaming: false, liveTurnId: null, error: null, worktreeError: null });
		activeSession.setDraft('');
		const sends = vi.spyOn(activeSession, 'sendMessage').mockResolvedValue(true);
		const { container } = render(ChatInput);
		await waitFor(() => expect(circles(container)).toEqual(['Start voice input', 'Start a call in this conversation']));
		await type(container, 'Ship it');
		await waitFor(() => expect(circles(container)).toEqual(['Start voice input', 'Send']));

		await fireEvent.click(screen.getByRole('button', { name: 'Send' }));
		await waitFor(() => expect(container.querySelector('.tiptap')!.textContent).toBe(''));
		expect(circles(container)).toEqual(['Start voice input', 'Send']);
		const held = screen.getByRole('button', { name: 'Send' }) as HTMLButtonElement;
		expect(held.disabled).toBe(true);
		await fireEvent.click(held);

		await waitFor(() => expect(sends).toHaveBeenCalledOnce());
		// Past the double-click window the empty composer offers the call again.
		await waitFor(() => expect(circles(container)).toEqual(['Start voice input', 'Start a call in this conversation']), { timeout: 2000 });
		expect(sends).toHaveBeenCalledOnce();
	});

	describe('Stop under a double tap on Send', () => {
		// With no seat held, Stop arrives in the place Send just left: the second
		// press of a double tap on Send must not cancel the turn it started.
		// fireEvent.click's detail is 0, a keyboard click; a tap has detail 1.
		const tap = (el: HTMLElement) => fireEvent.click(el, { detail: 1 });
		let stops: ReturnType<typeof vi.spyOn>;
		let sends: ReturnType<typeof vi.spyOn>;

		function idle(extra: Record<string, unknown> = {}) {
			voice.enabled = false;
			Object.assign(activeSession.current, { sessionId: 'local-idle', mode: 'code', profile: 'coding',
				isStreaming: false, liveTurnId: null, error: null, worktreeError: null, activeTasks: [], ...extra });
			activeSession.setDraft('');
			sends = vi.spyOn(activeSession, 'sendMessage').mockImplementation(async () => { startTurn('local-idle'); return true; });
			stops = vi.spyOn(activeSession, 'stopGeneration').mockImplementation(() => {});
		}

		async function sendDraft(container: HTMLElement) {
			await type(container, 'Ship it');
			await waitFor(() => expect(circles(container)).toContain('Send'));
			await tap(screen.getByRole('button', { name: 'Send' }));
			return screen.findByRole('button', { name: 'Stop generating' });
		}

		it('ignores the second tap, but not Stop from the keyboard or a later tap', async () => {
			idle();
			const { container } = render(ChatInput, { props: { compact: true } });
			const stop = await sendDraft(container);
			await tap(stop);
			expect(stops).not.toHaveBeenCalled();
			await fireEvent.click(stop);
			expect(stops).toHaveBeenCalledOnce();

			vi.spyOn(performance, 'now').mockReturnValue(performance.now() + 1000);
			await tap(screen.getByRole('button', { name: 'Stop generating' }));
			expect(stops).toHaveBeenCalledTimes(2);
		});

		it('judges the second tap by when it was made, not when a stalled page handles it', async () => {
			idle();
			const { container } = render(ChatInput, { props: { compact: true } });
			await type(container, 'Ship it');
			await waitFor(() => expect(circles(container)).toContain('Send'));
			const before = performance.now();
			await tap(screen.getByRole('button', { name: 'Send' }));
			const stop = await screen.findByRole('button', { name: 'Stop generating' });
			const after = performance.now();
			// The page was busy: both taps are handled a second after the send.
			vi.spyOn(performance, 'now').mockReturnValue(after + 1000);
			const tapAt = (timeStamp: number) => {
				const event = new MouseEvent('click', { bubbles: true, cancelable: true, detail: 1 });
				Object.defineProperty(event, 'timeStamp', { value: timeStamp });
				return fireEvent(stop, event);
			};
			// Made inside the window (the send was handled at or after `before`).
			await tapAt(before + 400);
			expect(stops).not.toHaveBeenCalled();
			// Made after it (the send was handled at or before `after`).
			await tapAt(after + 550);
			expect(stops).toHaveBeenCalledOnce();
		});

		it('judges a held second touch by when it began, not when it lifted', async () => {
			idle();
			const SENT = 1000;
			const clock = vi.spyOn(performance, 'now').mockReturnValue(SENT);
			const { container } = render(ChatInput, { props: { compact: true } });
			const stop = await sendDraft(container);
			// The row's own guard has long expired; only Stop's Send window holds.
			clock.mockReturnValue(SENT + 5000);
			const touch = async (type: string, timeStamp: number) => {
				const Ctor = typeof PointerEvent === 'function' ? PointerEvent : MouseEvent;
				const event = new Ctor(type, { bubbles: true, cancelable: true, detail: 1, pointerId: 7 } as PointerEventInit);
				Object.defineProperty(event, 'timeStamp', { value: timeStamp });
				if ((event as PointerEvent).pointerId !== 7) Object.defineProperty(event, 'pointerId', { value: 7 });
				await fireEvent(stop, event);
			};
			// Pressed 460ms after Send (inside its 500ms window), lifted at 800ms.
			await touch('pointerdown', SENT + 460);
			await touch('pointerup', SENT + 800);
			await touch('click', SENT + 800);
			expect(stops).not.toHaveBeenCalled();
			// A press that begins after the window stops the turn.
			await touch('pointerdown', SENT + 900);
			await touch('pointerup', SENT + 960);
			await touch('click', SENT + 960);
			expect(stops).toHaveBeenCalledOnce();
		});

		it('never ignores Stop after a send from the start screen (the composer moves away)', async () => {
			idle();
			const { container } = render(ChatInput);
			const stop = await sendDraft(container);
			await tap(stop);
			expect(stops).toHaveBeenCalledOnce();
		});

		it('ignores the second click of a double click on Send that reaches Stop from the start screen (#1632)', async () => {
			idle();
			const { container } = render(ChatInput);
			const stop = await sendDraft(container);
			await fireEvent.click(stop, { detail: 2 });
			expect(stops).not.toHaveBeenCalled();
		});

		it('ignores the stray tap before the background-agents confirmation, never after it', async () => {
			// Running agents keep Stop shown, so they start with the turn here.
			idle();
			sends.mockImplementation(async () => {
				startTurn('local-idle');
				Object.assign(activeSession.current, { activeTasks: [{ id: 'agent-1', status: 'running' }] });
				activeSession.setDraft('');
				return true;
			});
			const { container } = render(ChatInput, { props: { compact: true } });
			const stop = await sendDraft(container);
			await tap(stop);
			expect(screen.queryByRole('button', { name: 'Stop everything' })).toBeNull();
			// Inside the same window: the keyboard opens it, and confirming stops.
			await fireEvent.click(stop);
			await fireEvent.click(await screen.findByRole('button', { name: 'Stop everything' }));
			expect(stops).toHaveBeenCalledOnce();
		});

		it("never ignores another chat's Stop after a switch inside the window", async () => {
			idle();
			const { container } = render(ChatInput, { props: { compact: true } });
			await sendDraft(container);
			// Switch to chat B, already running (as switchSession does: a new
			// navigation, then B's state).
			bumpInitGeneration();
			startTurn('chat-b');
			await tap(await screen.findByRole('button', { name: 'Stop generating' }));
			expect(stops).toHaveBeenCalledOnce();
		});

		it('never ignores Stop after a queued send, with Stop already shown at the press', async () => {
			idle();
			startTurn();
			const { container } = render(ChatInput, { props: { compact: true } });
			const stop = await sendDraft(container);
			await waitFor(() => expect(sends).toHaveBeenCalledOnce());
			await tap(stop);
			expect(stops).toHaveBeenCalledOnce();
		});

		it('never ignores Stop after a press that sent nothing', async () => {
			// No agent to send with: the submit is refused before it sends.
			idle({ profile: '' });
			const { container } = render(ChatInput, { props: { compact: true } });
			await type(container, 'Ship it');
			await waitFor(() => expect(circles(container)).toContain('Send'));
			const pressed = performance.now();
			const clock = vi.spyOn(performance, 'now').mockReturnValue(pressed);
			await tap(screen.getByRole('button', { name: 'Send' }));
			await tick();
			expect(sends).not.toHaveBeenCalled();
			startTurn();
			const stop = await screen.findByRole('button', { name: 'Stop generating' });
			// Work starting elsewhere slid Stop into Send's seat: a tap is judged
			// after the row's own 400ms, still inside a Send window had one armed.
			clock.mockReturnValue(pressed + 450);
			const event = new MouseEvent('click', { bubbles: true, cancelable: true, detail: 1 });
			Object.defineProperty(event, 'timeStamp', { value: pressed + 450 });
			await fireEvent(stop, event);
			expect(stops).toHaveBeenCalledOnce();
		});
	});

	it('names the agent on a new session', async () => {
		render(VoiceCallButton, { props: { variant: 'circle', agentName: 'Overseer' } });
		const talk = await screen.findByRole('button', { name: 'Talk to Overseer' });
		expect(talk.classList.contains('call-circle')).toBe(true);
	});
});

describe('the attach circle', () => {
	afterEach(() => appMode.set('code'));

	it.each(['code', 'voice'] as const)('enables attach for an existing %s worker session without a local checkout', async (mode) => {
		appMode.set('code');
		activeSession.newChat();
		// A fresh compose for a project with no local checkout has nowhere to upload to.
		activeSession.setProject('', 'pv2-worker');
		render(ChatInput);
		const attach = await screen.findByRole('button', { name: 'Attach files' }) as HTMLButtonElement;
		await waitFor(() => expect(attach.disabled).toBe(true));
		// The circle says why on hover.
		expect(attach.getAttribute('title')).toBe(ATTACHMENTS_NEED_LOCAL_CHECKOUT);

		// An existing worker session stages files in its own directory instead.
		await activeSession.switchSession('attach-worker');
		activeSession.setProject('', 'pv2-worker');
		activeSession.reconcileSessionMode([{ id: 'attach-worker', mode }]);
		await tick();
		await waitFor(() => expect(attach.disabled).toBe(false));
		expect(attach.getAttribute('title')).toBe('Attach files');
		const pick = vi.spyOn(HTMLInputElement.prototype, 'click').mockImplementation(() => {});
		await fireEvent.click(attach);
		expect(pick).toHaveBeenCalledOnce();
	});
});

describe('the composer circles', () => {
	it('puts attach left of the text and keeps attach, dictate and the call out of the controls row', async () => {
		const { container } = render(ChatInput);
		const attach = screen.getByRole('button', { name: 'Attach files' });
		const editor = container.querySelector('.textarea-wrapper')!;
		expect(attach.compareDocumentPosition(editor) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
		expect(attach.closest('.input-area')).toBeTruthy();
		const dock = container.querySelector('.controls-dock')!;
		expect(dock.querySelector('.composer-attach, .attach-btn, .voice-btn, .voice-call-btn, .call-circle')).toBeNull();
	});
});

describe('a button sliding into a freed seat (#1632)', () => {
	// The row's clock is frozen at ARRIVED while it changes, then moved on so
	// the events' own times (the press, the release) are what count.
	const ARRIVED = 1000;
	let clock: ReturnType<typeof vi.spyOn>;
	beforeEach(() => { clock = vi.spyOn(performance, 'now').mockReturnValue(ARRIVED); });

	function fire(type: string, el: Element, timeStamp: number, init: { pointerId?: number; detail?: number } = {}) {
		const Ctor = typeof PointerEvent === 'function' ? PointerEvent : MouseEvent;
		const event = new Ctor(type, { bubbles: true, cancelable: true, detail: init.detail ?? 1, ...init });
		Object.defineProperty(event, 'timeStamp', { value: timeStamp });
		if (init.pointerId !== undefined && (event as PointerEvent).pointerId !== init.pointerId) {
			Object.defineProperty(event, 'pointerId', { value: init.pointerId });
		}
		return fireEvent(el, event);
	}
	/** Clicks that got through to `el`, kept from its own handler. */
	function reached(el: Element) {
		const hits: number[] = [];
		el.addEventListener('click', (event) => { hits.push(event.timeStamp); event.stopPropagation(); });
		return hits;
	}
	/** A tap: pressed at `down`, released (the click) at `up`. */
	async function tap(el: Element, down: number, up: number, pointerId = 1) {
		await fire('pointerdown', el, down, { pointerId });
		await fire('pointerup', el, up, { pointerId });
		await fire('click', el, up, { pointerId });
	}

	/** Talk takes Stop's seat as the turn ends on its own, at ARRIVED. */
	async function talkReplacesStop() {
		startTurn();
		const { container } = render(ChatInput, { props: { compact: true } });
		await waitFor(() => expect(circles(container)).toEqual([
			'Start voice input', 'Start a call in this conversation', 'Stop generating'
		]));
		const stop = container.querySelector('.stop-btn')!;
		Object.assign(activeSession.current, { isStreaming: false, liveTurnId: null });
		activeSession.setDraft('');
		await waitFor(() => expect(circles(container)).toEqual(['Start voice input', 'Start a call in this conversation']));
		clock.mockReturnValue(ARRIVED + 5000);
		return { container, stop, talk: container.querySelector('.call-circle')! };
	}

	it('judges a tap by when the press began, not when it was released', async () => {
		const { talk } = await talkReplacesStop();
		const hits = reached(talk);
		// Pressed 350ms after Talk arrived, released 100ms later: aimed at Stop.
		await tap(talk, ARRIVED + 350, ARRIVED + 450);
		expect(hits).toEqual([]);
		// Pressed after the window: a real press on Talk.
		await tap(talk, ARRIVED + 450, ARRIVED + 520);
		expect(hits).toEqual([ARRIVED + 520]);
		// However long it is held, a press that began inside the window is held back.
		cleanup();
		clock.mockReturnValue(ARRIVED);
		const late = await talkReplacesStop();
		const lateHits = reached(late.talk);
		clock.mockReturnValue(ARRIVED + 60_000);
		await tap(late.talk, ARRIVED + 100, ARRIVED + 100 + 30_000);
		expect(lateHits).toEqual([]);
	});

	it('never lets a press that began on Stop act on the Talk that replaced it', async () => {
		startTurn();
		const mounted = render(ChatInput, { props: { compact: true } });
		await waitFor(() => expect(circles(mounted.container)).toContain('Stop generating'));
		await fire('pointerdown', mounted.container.querySelector('.stop-btn')!, ARRIVED - 50, { pointerId: 4 });
		Object.assign(activeSession.current, { isStreaming: false, liveTurnId: null });
		activeSession.setDraft('');
		await waitFor(() => expect(circles(mounted.container)).not.toContain('Stop generating'));
		clock.mockReturnValue(ARRIVED + 5000);
		const talk = mounted.container.querySelector('.call-circle')!;
		const hits = reached(talk);
		// Released on Talk well after the window: still Stop's press.
		await fire('click', talk, ARRIVED + 900, { pointerId: 4 });
		expect(hits).toEqual([]);
	});

	it('leaves no stale press behind a cancelled touch, and no press to a click without one', async () => {
		const { talk } = await talkReplacesStop();
		const hits = reached(talk);
		// A touch that turned into a scroll inside the window...
		await fire('pointerdown', talk, ARRIVED + 100, { pointerId: 9 });
		await fire('pointercancel', talk, ARRIVED + 150, { pointerId: 9 });
		// ...does not decide a later click: that one is judged by its own time.
		await fire('click', talk, ARRIVED + 600, { pointerId: 9 });
		// A scripted or assistive click (no pointer) after the window works,
		await fire('click', talk, ARRIVED + 650, { pointerId: -1 });
		// and a keyboard click always does.
		await fire('click', talk, ARRIVED + 10, { detail: 0 });
		expect(hits).toEqual([ARRIVED + 600, ARRIVED + 650, ARRIVED + 10]);
		// Inside the window, a click with no press of its own is still held back.
		const early = await talkReplacesStop();
		const earlyHits = reached(early.talk);
		await fire('click', early.talk, ARRIVED + 100, { pointerId: -1 });
		expect(earlyHits).toEqual([]);
	});

	it('never holds back the mic when work starting elsewhere grows the row', async () => {
		// Same chat, nothing typed: another tab starts a turn, mic · Talk → mic · Talk · Stop.
		Object.assign(activeSession.current, { sessionId: 'local-working', mode: 'code', profile: 'coding',
			isStreaming: false, liveTurnId: null, error: null, worktreeError: null, activeTasks: [] });
		activeSession.setDraft('');
		const { container } = render(ChatInput, { props: { compact: true } });
		await waitFor(() => expect(circles(container)).toEqual(['Start voice input', 'Start a call in this conversation']));
		startTurn('local-working');
		await waitFor(() => expect(circles(container)).toContain('Stop generating'));
		clock.mockReturnValue(ARRIVED + 5000);
		const mic = container.querySelector('.voice-btn')!;
		const hits = reached(mic);
		await tap(mic, ARRIVED + 100, ARRIVED + 160);
		expect(hits).toEqual([ARRIVED + 160]);
	});

	it('stops the queued turn when Stop is pressed just after it came back at a queue handoff', async () => {
		const stops = vi.spyOn(activeSession, 'stopGeneration').mockImplementation(() => {});
		const { container, talk } = await talkReplacesStop();
		// Talk shows for a moment between the two turns and is still held back...
		const talkHits = reached(talk);
		await tap(talk, ARRIVED + 50, ARRIVED + 90);
		expect(talkHits).toEqual([]);
		// ...and 150 ms after the turn ended the queued one starts in the same
		// chat: Stop is back in the seat it held.
		clock.mockReturnValue(ARRIVED + 150);
		startTurn();
		await waitFor(() => expect(circles(container)).toContain('Stop generating'));
		clock.mockReturnValue(ARRIVED + 5000);
		const back = container.querySelector('.stop-btn')!;
		await tap(back, ARRIVED + 170, ARRIVED + 230);
		expect(stops).toHaveBeenCalledTimes(1);
		await tap(back, ARRIVED + 450, ARRIVED + 510, 2);
		expect(stops).toHaveBeenCalledTimes(2);
	});

	it('still holds Stop back when Talk had the seat for longer than the wait', async () => {
		const stops = vi.spyOn(activeSession, 'stopGeneration').mockImplementation(() => {});
		const { container } = await talkReplacesStop();
		clock.mockReturnValue(ARRIVED + 600);
		startTurn();
		await waitFor(() => expect(circles(container)).toContain('Stop generating'));
		clock.mockReturnValue(ARRIVED + 5000);
		const back = container.querySelector('.stop-btn')!;
		await tap(back, ARRIVED + 620, ARRIVED + 680);
		expect(stops).not.toHaveBeenCalled();
		await tap(back, ARRIVED + 1010, ARRIVED + 1070, 2);
		expect(stops).toHaveBeenCalledTimes(1);
	});

	describe('a call starting or ending in a working chat', () => {
		afterEach(() => voiceCallState.set(callIdle));
		const callIdle = { ...get(voiceCallState) };

		async function workingWithTalk() {
			startTurn('local-working');
			const { container } = render(ChatInput, { props: { compact: true } });
			await waitFor(() => expect(circles(container)).toEqual([
				'Start voice input', 'Start a call in this conversation', 'Stop generating'
			]));
			return container;
		}

		it('never lets the second touch of a double tap on Talk start dictation on the mic that took its seat', async () => {
			const container = await workingWithTalk();
			// The tap on Talk starts the call: Talk leaves, mic · Stop, the mic in Talk's seat.
			voiceCallState.set({ ...callIdle, sessionId: 'local-working', status: 'connecting' });
			await waitFor(() => expect(circles(container)).toEqual(['Start voice input', 'Stop generating']));
			clock.mockReturnValue(ARRIVED + 5000);
			const mic = container.querySelector('.voice-btn')!;
			const hits = reached(mic);
			await tap(mic, ARRIVED + 100, ARRIVED + 160);
			expect(hits).toEqual([]);
			await tap(mic, ARRIVED + 450, ARRIVED + 520);
			expect(hits).toEqual([ARRIVED + 520]);
		});

		it('holds back a tap aimed at the mic that lands on Talk returning as the call ends', async () => {
			voiceCallState.set({ ...callIdle, sessionId: 'local-working', status: 'live' });
			startTurn('local-working');
			const { container } = render(ChatInput, { props: { compact: true } });
			await waitFor(() => expect(circles(container)).toEqual(['Start voice input', 'Stop generating']));
			// The call ends: Talk comes back into the mic's seat.
			voiceCallState.set(callIdle);
			await waitFor(() => expect(circles(container)).toEqual([
				'Start voice input', 'Start a call in this conversation', 'Stop generating'
			]));
			clock.mockReturnValue(ARRIVED + 5000);
			const talk = container.querySelector('.call-circle')!;
			const hits = reached(talk);
			await tap(talk, ARRIVED + 100, ARRIVED + 160);
			expect(hits).toEqual([]);
			await tap(talk, ARRIVED + 450, ARRIVED + 520);
			expect(hits).toEqual([ARRIVED + 520]);
		});
	});
});

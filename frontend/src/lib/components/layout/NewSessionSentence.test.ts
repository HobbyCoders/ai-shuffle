/**
 * The new-session sentence: "Runs on <worker> in <project> on <branch> in
 * <a worktree>." / "Runs on <worker> with <no project>." Each part is a real
 * button that opens its menu; branch and worktree only for a git project; the
 * worker's online dot never shows in the sentence. Cmd/Ctrl+P (AppPage's
 * shortcut, through `openProjectPicker`) opens its project picker.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { tick } from 'svelte';

const m = vi.hoisted(() => ({
	setWorktreeIntent: vi.fn(),
	clearProject: vi.fn(),
	setProject: vi.fn(),
	listInstalls: vi.fn()
}));

const stores = vi.hoisted(() => ({}) as Record<string, import('svelte/store').Writable<unknown>>);

vi.mock('$lib/stores/active-session', async () => {
	const { writable } = await import('svelte/store');
	stores.session = writable({});
	return {
		activeSession: {
			subscribe: stores.session.subscribe,
			setWorktreeIntent: (intent: string) => {
				m.setWorktreeIntent(intent);
				stores.session.update((s) => ({ ...(s as object), worktreeIntent: intent }));
			},
			clearProject: m.clearProject,
			setProject: m.setProject,
			setDeviceIntent: vi.fn(),
			setWorktreeBranch: vi.fn(),
			setWorktreeMode: vi.fn(),
			clearWorktreeSelection: vi.fn(),
			setWorktreeBaseBranch: vi.fn(),
			setWorktreeCreateNewBranch: vi.fn(),
			selectWorktreeBranch: vi.fn(),
			clearWorktreeError: vi.fn(),
			setWorkspaceProjectIds: vi.fn()
		}
	};
});
vi.mock('$lib/stores/app-data.svelte', async () => {
	const { writable } = await import('svelte/store');
	stores.projects = writable([]);
	return {
		projects: stores.projects,
		clusterProjects: writable([]),
		profiles: writable([]),
		profilesLoaded: writable(true),
		sessions: writable([]),
		appData: { loadClusterProjects: vi.fn(), current: { projects: [], clusterProjects: [] } }
	};
});
vi.mock('$lib/stores/auth.svelte', async () => {
	const { writable } = await import('svelte/store');
	stores.github = writable(true);
	// The shared view kit pulls in Settings access, which reads isAdmin.
	return { githubAuthenticated: stores.github, isAdmin: writable(true) };
});
vi.mock('$lib/stores/git.svelte', async () => {
	const { writable } = await import('svelte/store');
	stores.git = writable({});
	const git = {
		subscribe: stores.git.subscribe,
		loadWorktrees: vi.fn(async () => {}),
		loadBranches: vi.fn(async () => {}),
		refreshStatus: vi.fn(),
		refreshDiffStats: vi.fn(),
		forgetWorktree: vi.fn()
	};
	return {
		git,
		worktrees: writable([]),
		branches: writable([]),
		currentBranch: writable(null),
		gitStatus: writable({ current_branch: 'main' }),
		gitMode: writable('local')
	};
});
vi.mock('$lib/api/installs', () => ({
	listInstalls: m.listInstalls,
	getClusterGitState: vi.fn().mockResolvedValue(null)
}));

vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });

import NewSessionSentence from './NewSessionSentence.svelte';
import { openProjectPicker } from '$lib/services/project-picker';
import { resetWorkerPresence } from '$lib/stores/worker-presence';
import TiptapEditor from '$lib/components/chat/TiptapEditor.svelte';
import type { Editor } from '$lib/components/chat/tiptap/editorConfig';

const install = (id: string, name: string, extra: Record<string, unknown> = {}) => ({
	id, device_name: name, base_url: `http://${id}`, platform: 'linux', version: '1', capabilities: {},
	last_seen: null, online: true, approved: true, ...extra
});

function session(over: Record<string, unknown> = {}) {
	stores.session.set({
		sessionId: null, project: 'p1', projectV2Id: null, worktreeIntent: 'worktree', worktreeId: null,
		worktreeBranch: null, selectedWorktreeBranch: null, worktreeBaseBranch: null, deviceIntent: null,
		workspaceProjectIds: [], worktreeError: null, isStreaming: false, ...over
	});
}

/**
 * The sentence's words. Whitespace inside each word's wrapper is markup only
 * (the wrappers are inline-flex, so it never renders), hence the " ." fold.
 */
const text = () => (screen.getByRole('group', { name: 'Where this session runs' }).textContent ?? '')
	.replace(/\s+/g, ' ').replace(/ \./g, '.').trim();

beforeEach(() => {
	resetWorkerPresence();
	m.listInstalls.mockResolvedValue({ installs: [install('self', 'office-pc', { is_self: true }), install('pc', 'laptop', { online: false })], self_instance_id: 'self' });
	stores.projects.set([{ id: 'p1', name: 'ai-shuffle', path: '/workspace/ai-shuffle' }]);
	stores.github.set(true);
	stores.git.set({ hydrated: false });
	session();
});
afterEach(() => {
	cleanup();
	vi.clearAllMocks();
	resetWorkerPresence();
});

describe('NewSessionSentence', () => {
	it('names every control for a git project in a worktree, each one a button', async () => {
		render(NewSessionSentence);
		await waitFor(() => expect(text()).toBe('office-pc ai-shuffle New branch from main Worktree'));
		for (const name of [/^Runs on office-pc$/, /^Workspace folders: ai-shuffle$/, /^Worktree branch: a new branch from main$/, /Runs in a worktree/]) {
			expect(screen.getByRole('button', { name })).toBeTruthy();
		}
		// The status dot replaces the worker's icon: one mark, never both.
		const runsOn = screen.getByRole('button', { name: /^Runs on office-pc$/ });
		expect(runsOn.querySelector('.worker-status-dot')).toBeTruthy();
		expect(runsOn.querySelector('svg')).toBeNull();
	});

	it('names a picked branch instead of "a new branch"', async () => {
		session({ selectedWorktreeBranch: 'feat/redesign' });
		render(NewSessionSentence);
		await waitFor(() => expect(text()).toContain('feat/redesign Worktree'));
	});

	it('toggles the worktree control to the main checkout and shows the checkout branch', async () => {
		render(NewSessionSentence);
		const wt = await screen.findByRole('button', { name: /Runs in a worktree/ });
		await fireEvent.click(wt);
		expect(m.setWorktreeIntent).toHaveBeenCalledWith('local');
		await waitFor(() => expect(text()).toBe('office-pc ai-shuffle main Worktree'));
	});

	it('shows extra workspace folders as +N on the project control', async () => {
		session({ workspaceProjectIds: ['p2'] });
		render(NewSessionSentence);
		const project = await screen.findByRole('button', { name: /Workspace folders: ai-shuffle and 1 more/ });
		expect(project.textContent).toBe('ai-shuffle+1');
	});

	it('drops branch and worktree for a folder that is not a git repository', async () => {
		session({ worktreeIntent: 'local' });
		stores.git.set({ hydrated: true, projectId: 'p1', sessionId: null, worktreeId: null, status: { is_git_repo: false } });
		render(NewSessionSentence);
		await waitFor(() => expect(text()).toBe('office-pc ai-shuffle'));
	});

	it('reads "No project" without one, and that control opens the project picker', async () => {
		session({ project: '', projectV2Id: null });
		render(NewSessionSentence);
		await waitFor(() => expect(text()).toBe('office-pc No project'));
		const none = screen.getByRole('button', { name: /^No project\. Add a project/ });
		await fireEvent.click(none);
		await tick();
		expect(none.getAttribute('aria-expanded')).toBe('true');
	});

	it('closes the branch menu on Escape and puts focus back on the branch word', async () => {
		render(NewSessionSentence);
		const word = await screen.findByRole('button', { name: /^Worktree branch: a new branch from main$/ });
		expect(word.getAttribute('aria-haspopup')).toBe('dialog');
		await fireEvent.click(word);
		const menu = await screen.findByRole('dialog', { name: 'Worktree branch' });
		expect(word.getAttribute('aria-expanded')).toBe('true');
		// It hangs from <body>, centred under the word.
		expect(menu.parentElement).toBe(document.body);
		(menu.querySelector('button') as HTMLButtonElement).focus();

		await fireEvent.keyDown(window, { key: 'Escape' });
		await tick();
		expect(screen.queryByRole('dialog', { name: 'Worktree branch' })).toBeNull();
		expect(word.getAttribute('aria-expanded')).toBe('false');
		expect(document.activeElement).toBe(word);
	});

	it('opens the worker list from the worker word, offline workers disabled', async () => {
		render(NewSessionSentence);
		await fireEvent.click(await screen.findByRole('button', { name: /^Runs on office-pc$/ }));
		await waitFor(() => expect(screen.getAllByRole('menuitemradio')).toHaveLength(2));
		const rows = screen.getAllByRole('menuitemradio') as HTMLButtonElement[];
		expect(rows[0].textContent).toContain('office-pc');
		expect(rows[0].getAttribute('aria-checked')).toBe('true');
		expect(rows[1].textContent).toContain('laptop');
		expect(rows[1].disabled || rows[1].getAttribute('aria-disabled') === 'true').toBe(true);
		// The menu hangs from <body>, centred under the word.
		expect(screen.getByRole('menu').parentElement).toBe(document.body);
	});

	describe('Cmd/Ctrl+P', () => {
		// The real message box: a TipTap editor, as ChatInput mounts it.
		let composer: Editor;
		let editor: HTMLElement;
		let unmountComposer: () => void;
		async function mountComposer(content?: object) {
			const view = render(TiptapEditor, { props: {} });
			await tick();
			composer = view.component.getEditor()!;
			if (content) composer.commands.setContent(content);
			editor = composer.view.dom as HTMLElement;
			unmountComposer = view.unmount;
		}
		const p = (...content: object[]) => ({ type: 'paragraph', ...(content.length ? { content } : {}) });
		const t = (text: string) => ({ type: 'text', text });
		const doc = (...content: object[]) => ({ type: 'doc', content });
		/** Focus the message box with the caret at a document position. */
		function caretAt(pos: number) {
			composer.commands.setTextSelection(pos);
			composer.view.focus();
		}
		const caret = () => ({ from: composer.state.selection.from, to: composer.state.selection.to });
		const searchBox = () => screen.getByRole('textbox', { name: 'Search projects' });
		/** Typing goes where the caret is: "X" after it. */
		function type(text: string) {
			composer.commands.insertContent(text);
			return composer.getJSON();
		}
		async function noProjectScreen() {
			session({ project: '', projectV2Id: null });
			render(NewSessionSentence);
			await tick();
		}
		async function openThenEscape() {
			expect(openProjectPicker()).toBe(true);
			await tick();
			expect(document.activeElement).toBe(searchBox());
			await fireEvent.keyDown(searchBox(), { key: 'Escape' });
			expect(screen.queryByRole('textbox', { name: 'Search projects' })).toBeNull();
		}
		beforeEach(async () => { await mountComposer(); });

		it('declines with no new-session row on screen, so the browser keeps the key', () => {
			expect(openProjectPicker()).toBe(false);
		});

		it('declines once the chat is a saved session', async () => {
			session({ sessionId: 'chat-1' });
			render(NewSessionSentence);
			await tick();
			expect(openProjectPicker()).toBe(false);
			expect(screen.queryByRole('dialog')).toBeNull();
		});

		it('opens the picker on "No project" from the editor, and Escape returns there', async () => {
			await noProjectScreen();
			composer.view.focus();
			expect(openProjectPicker()).toBe(true);
			await tick();
			expect(document.activeElement).toBe(searchBox());
			expect(screen.getByRole('button', { name: /^No project\. Add a project/ }).getAttribute('aria-expanded')).toBe('true');
			await fireEvent.keyDown(searchBox(), { key: 'Escape' });
			expect(screen.queryByRole('textbox', { name: 'Search projects' })).toBeNull();
			expect(document.activeElement).toBe(editor);
		});

		it('picks with Enter and keeps the cursor in the editor', async () => {
			await noProjectScreen();
			composer.view.focus();
			openProjectPicker();
			await tick();
			await fireEvent.keyDown(searchBox(), { key: 'Enter' });
			await tick();
			expect(m.setProject).toHaveBeenCalledWith('p1', null, { user: true });
			expect(document.activeElement).toBe(editor);
		});

		describe('puts the caret back where it was', () => {
			it('at the start of the second paragraph, after Escape and after a pick', async () => {
				composer.commands.setContent(doc(p(t('ab')), p(t('cd'))));
				await noProjectScreen();
				// <p>ab</p><p>|cd</p>: position 5.
				caretAt(5);
				await openThenEscape();
				expect(document.activeElement).toBe(editor);
				expect(caret()).toEqual({ from: 5, to: 5 });
				expect(type('X')).toEqual(doc(p(t('ab')), p(t('Xcd'))));

				caretAt(6);
				openProjectPicker();
				await tick();
				await fireEvent.keyDown(searchBox(), { key: 'Enter' });
				await tick();
				expect(m.setProject).toHaveBeenCalledWith('p1', null, { user: true });
				expect(document.activeElement).toBe(editor);
				expect(type('Y')).toEqual(doc(p(t('ab')), p(t('XYcd'))));
			});

			it('in an empty last paragraph', async () => {
				composer.commands.setContent(doc(p(t('ab')), p()));
				await noProjectScreen();
				caretAt(5);
				await openThenEscape();
				expect(type('X')).toEqual(doc(p(t('ab')), p(t('X'))));
			});

			it('after a hard break', async () => {
				composer.commands.setContent(doc(p(t('ab'), { type: 'hardBreak' }, t('cd'))));
				await noProjectScreen();
				// <p>ab<br>|cd</p>: 1 + 2 + 1.
				caretAt(4);
				await openThenEscape();
				expect(type('X')).toEqual(doc(p(t('ab'), { type: 'hardBreak' }, t('Xcd'))));
			});

			it('keeps a selection, not just a caret', async () => {
				composer.commands.setContent(doc(p(t('abcd'))));
				await noProjectScreen();
				composer.commands.setTextSelection({ from: 2, to: 4 });
				composer.view.focus();
				await openThenEscape();
				expect(caret()).toEqual({ from: 2, to: 4 });
			});

			it('in the message box rebuilt while the picker was open (the project changed)', async () => {
				composer.commands.setContent(doc(p(t('ab')), p(t('cd'))));
				await noProjectScreen();
				caretAt(5);
				expect(openProjectPicker()).toBe(true);
				await tick();
				// The composer remounts with the same draft.
				unmountComposer();
				await mountComposer(doc(p(t('ab')), p(t('cd'))));
				await fireEvent.keyDown(searchBox(), { key: 'Escape' });
				expect(document.activeElement).toBe(editor);
				expect(type('X')).toEqual(doc(p(t('ab')), p(t('Xcd'))));
			});

			it('at the end when the rebuilt draft is shorter than the saved place', async () => {
				composer.commands.setContent(doc(p(t('abcdef'))));
				await noProjectScreen();
				caretAt(6);
				openProjectPicker();
				await tick();
				unmountComposer();
				await mountComposer(doc(p(t('ab'))));
				await fireEvent.keyDown(searchBox(), { key: 'Escape' });
				expect(type('X')).toEqual(doc(p(t('abX'))));
			});

			it('at the end of the message box when nothing had focus', async () => {
				composer.commands.setContent(doc(p(t('ab')), p(t('cd'))));
				await noProjectScreen();
				(document.activeElement as HTMLElement | null)?.blur();
				await openThenEscape();
				expect(document.activeElement).toBe(editor);
				expect(type('X')).toEqual(doc(p(t('ab')), p(t('cdX'))));
			});
		});

		it('switches a picked project from the project word, "No project" clearing it', async () => {
			render(NewSessionSentence);
			const word = await screen.findByRole('button', { name: /^Workspace folders: ai-shuffle$/ });
			expect(word.getAttribute('aria-keyshortcuts')).toMatch(/^(Control|Meta)\+P$/);
			composer.view.focus();
			expect(openProjectPicker()).toBe(true);
			await tick();
			const picker = screen.getByRole('dialog', { name: 'Switch project' });
			await fireEvent.click(within(picker).getByRole('button', { name: 'No project' }));
			expect(m.clearProject).toHaveBeenCalledWith({ user: true });
			await tick();
			expect(document.activeElement).toBe(editor);
		});

		it('offers the same picker to the mouse from the folders menu', async () => {
			vi.useFakeTimers();
			try {
				render(NewSessionSentence);
				await fireEvent.click(await screen.findByRole('button', { name: /^Workspace folders: ai-shuffle$/ }));
				await fireEvent.click(screen.getByRole('button', { name: /Switch project/ }));
				await vi.runAllTimersAsync();
				expect(screen.getByRole('dialog', { name: 'Switch project' })).toBeTruthy();
				expect(screen.queryByRole('dialog', { name: 'Workspace folders' })).toBeNull();
			} finally {
				vi.useRealTimers();
			}
		});
	});
});

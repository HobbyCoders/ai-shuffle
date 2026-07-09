/**
 * Behavior tests for the SessionsBoard action rail — specifically the inline
 * Resume / Fork icon buttons that replaced the old right-click context menu.
 *
 * These mount the real board with its store/api dependencies mocked, then drive
 * the rail buttons that appear on every session row. Covered:
 *  - both new buttons render on each row, alongside Favorite/Clear
 *  - the Resume tooltip is conditional: "Open" for a local row, "Resume here …"
 *    (with the owning device) for a cross-install/remote row
 *  - clicking Fork forks that row's session and opens the NEW one, and does NOT
 *    open the clicked row (stopPropagation) — the row click would open its own id
 *  - clicking Resume on a remote row adopts-then-opens; on a local row it opens
 *    directly without adopting
 *  - the pre-existing Favorite rail action still fires
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/svelte';

// jsdom lacks ResizeObserver; some lucide/layout paths probe for it.
if (!('ResizeObserver' in globalThis)) {
	(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = class {
		observe() {}
		unobserve() {}
		disconnect() {}
	};
}

// A tiny Svelte-store-compatible container so `$store` auto-subscription works
// inside the component under test without pulling in the real store graph.
const mocks = vi.hoisted(() => {
	function store<T>(initial: T) {
		let val = initial;
		const subs = new Set<(v: T) => void>();
		return {
			subscribe(fn: (v: T) => void) {
				fn(val);
				subs.add(fn);
				return () => subs.delete(fn);
			},
			set(v: T) {
				val = v;
				subs.forEach((f) => f(val));
			}
		};
	}
	return {
		sessions: store<unknown[]>([]),
		projects: store<unknown[]>([]),
		activeSession: store<{ sessionId: string | null; isStreaming: boolean }>({
			sessionId: null,
			isStreaming: false
		}),
		streamingSessions: store<Set<string>>(new Set()),
		backgroundPermissions: store<Set<string>>(new Set()),
		isAdmin: store<boolean>(false),
		forkSession: vi.fn(),
		adoptSession: vi.fn(),
		toggleSessionFavorite: vi.fn(),
		loadSessions: vi.fn(),
		loadProjects: vi.fn(),
		deleteSession: vi.fn(),
		toastPush: vi.fn(),
		apiPost: vi.fn(),
		apiDelete: vi.fn()
	};
});

vi.mock('$lib/stores/app-data.svelte', () => ({
	appData: {
		loadSessions: mocks.loadSessions,
		loadProjects: mocks.loadProjects,
		deleteSession: mocks.deleteSession
	},
	sessions: mocks.sessions,
	projects: mocks.projects
}));
vi.mock('$lib/stores/active-session', () => ({
	activeSession: mocks.activeSession,
	streamingSessions: mocks.streamingSessions,
	backgroundPermissions: mocks.backgroundPermissions
}));
vi.mock('$lib/api/client', () => ({
	toggleSessionFavorite: mocks.toggleSessionFavorite,
	forkSession: mocks.forkSession,
	adoptSession: mocks.adoptSession,
	api: { post: mocks.apiPost, delete: mocks.apiDelete }
}));
vi.mock('$lib/stores/auth.svelte', () => ({ isAdmin: mocks.isAdmin }));
vi.mock('$lib/stores/toast', () => ({ toast: { push: mocks.toastPush } }));

// Imported AFTER the mocks are registered.
import SessionsBoard from './SessionsBoard.svelte';

type AnySession = Record<string, unknown>;
function makeSession(over: AnySession = {}): AnySession {
	return {
		id: 'a',
		title: 'A session',
		is_favorite: false,
		is_remote: false,
		instance_id: null,
		device_name: null,
		device_online: true,
		controllable: true,
		project_id: null,
		project_name: null,
		link_group_id: null,
		automation_id: null,
		worktree_branch: null,
		total_cost_usd: 0,
		turn_count: 0,
		created_at: '2026-07-01T00:00:00Z',
		updated_at: '2026-07-08T12:00:00Z',
		...over
	};
}

// Row A: local (tooltip "Open"). Row B: remote on "Mac Studio" (tooltip "Resume here …").
const localSession = makeSession({ id: 'a', title: 'Local one', updated_at: '2026-07-08T12:00:00Z' });
const remoteSession = makeSession({
	id: 'b',
	title: 'Remote one',
	is_remote: true,
	instance_id: 'inst-2',
	device_name: 'Mac Studio',
	device_online: true,
	updated_at: '2026-07-08T11:00:00Z'
});

function renderBoard() {
	const onOpenSession = vi.fn();
	const onNewSession = vi.fn();
	render(SessionsBoard, { props: { onOpenSession, onNewSession } });
	return { onOpenSession, onNewSession };
}

/** The `.row` whose title matches (scoped to the row list — the same title
 * also appears in a project card's "latest" line, so a global text query is
 * ambiguous). */
function rowFor(title: string): HTMLElement {
	const rows = Array.from(document.querySelectorAll('.row')) as HTMLElement[];
	const el = rows.find((r) => r.querySelector('.row-title')?.textContent?.trim() === title);
	if (!el) throw new Error(`no row for ${title}`);
	return el;
}

beforeEach(() => {
	vi.clearAllMocks();
	mocks.forkSession.mockResolvedValue({ session_id: 'forked-a' });
	mocks.adoptSession.mockResolvedValue({ session_id: 'b', adopted: true });
	mocks.toggleSessionFavorite.mockResolvedValue({});
	mocks.loadSessions.mockResolvedValue(undefined);
	mocks.sessions.set([localSession, remoteSession]);
});

afterEach(() => cleanup());

describe('SessionsBoard action rail', () => {
	it('renders Resume + Fork buttons on every row, next to Favorite/Clear', () => {
		renderBoard();
		// One Fork + one Clear + one Favorite per row (two rows).
		expect(screen.getAllByTitle('Fork session')).toHaveLength(2);
		expect(screen.getAllByTitle('Clear session')).toHaveLength(2);
		expect(screen.getAllByTitle('Favorite')).toHaveLength(2);
	});

	it('gives the Resume button a conditional tooltip (Open vs Resume here + device)', () => {
		renderBoard();
		// Local row → plain open.
		expect(rowFor('Local one').querySelector('[title="Open"]')).toBeTruthy();
		// Remote row → adopt-and-resume, tagged with the owning device.
		expect(rowFor('Remote one').querySelector('[title="Resume here (Mac Studio)"]')).toBeTruthy();
	});

	it('forks the row and opens the NEW session, without opening the clicked row', async () => {
		const { onOpenSession } = renderBoard();
		const fork = rowFor('Local one').querySelector('[title="Fork session"]') as HTMLElement;

		await fireEvent.click(fork);

		expect(mocks.forkSession).toHaveBeenCalledWith('a');
		await vi.waitFor(() => expect(onOpenSession).toHaveBeenCalledWith('forked-a'));
		// stopPropagation: the row's own click (which would open 'a') never fired.
		expect(onOpenSession).not.toHaveBeenCalledWith('a');
	});

	it('resumes a remote session by adopting it, then opening it', async () => {
		const { onOpenSession } = renderBoard();
		const resume = rowFor('Remote one').querySelector('[title="Resume here (Mac Studio)"]') as HTMLElement;

		await fireEvent.click(resume);

		expect(mocks.adoptSession).toHaveBeenCalledWith('b');
		await vi.waitFor(() => expect(onOpenSession).toHaveBeenCalledWith('b'));
	});

	it('resumes a local session by opening it directly, without adopting', async () => {
		const { onOpenSession } = renderBoard();
		const resume = rowFor('Local one').querySelector('[title="Open"]') as HTMLElement;

		await fireEvent.click(resume);

		await vi.waitFor(() => expect(onOpenSession).toHaveBeenCalledWith('a'));
		expect(mocks.adoptSession).not.toHaveBeenCalled();
	});

	it('still fires the pre-existing Favorite rail action', async () => {
		renderBoard();
		const fav = rowFor('Local one').querySelector('[title="Favorite"]') as HTMLElement;

		await fireEvent.click(fav);

		expect(mocks.toggleSessionFavorite).toHaveBeenCalledWith('a');
	});
});

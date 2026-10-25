import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import ReviewDiffPanel from './ReviewDiffPanel.svelte';
import DiffViewer from './DiffViewer.svelte';
import type { FileChangeInfo } from '$lib/api/git';

const api = vi.hoisted(() => ({ getFileDiff: vi.fn(), getBaseChangedFiles: vi.fn(), getBaseFileDiff: vi.fn(), refreshChangedFiles: vi.fn(), target: { worktreeId: 'branch', sessionId: 'session', workerTarget: null } as { worktreeId: string | null; sessionId: string | null; workerTarget: {workerId: string; branch: string} | null } }));
vi.mock('$lib/api/git', () => ({ getFileDiff: api.getFileDiff, getBaseChangedFiles: api.getBaseChangedFiles, getBaseFileDiff: api.getBaseFileDiff }));
vi.mock('$lib/stores/git.svelte', () => ({ git: { get current() { return api.target; }, refreshChangedFiles: api.refreshChangedFiles } }));
vi.mock('$lib/stores/notifications', () => ({ notify: vi.fn() }));
vi.mock('$lib/utils/shiki', () => ({
	resolveShikiLang: () => 'typescript',
	onHighlighterReady: () => () => {},
	highlightToInnerHtml: (code: string) => code.split('\n').map(line => `<span>${line.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')}</span>`).join('\n')
}));
const patch = 'diff --git a/src/a.ts b/src/a.ts\n--- a/src/a.ts\n+++ b/src/a.ts\n@@ -1,2 +1,2 @@\n-old value\n+new value\n context\n';
const files: FileChangeInfo[] = [
	{ file: 'src/a.ts', category: 'modified', status: 'M', additions: 1, deletions: 1, is_binary: false },
	{ file: 'src/a.ts', category: 'staged', status: 'M', additions: 3, deletions: 2, is_binary: false },
	{ file: 'new.txt', category: 'untracked', status: '?', additions: 1, deletions: 0, is_binary: false }
];
function setup() {
	return render(ReviewDiffPanel, { projectId: 'project', files, loading: false, selected: files[0], onSelect: vi.fn(), onClose: vi.fn(), embedded: true });
}
beforeEach(() => {
	vi.clearAllMocks();
	api.target = { worktreeId: 'branch', sessionId: 'session', workerTarget: null };
	api.getFileDiff.mockResolvedValue({ file: 'src/a.ts', diff: patch, is_binary: false, truncated: false });
	api.refreshChangedFiles.mockResolvedValue(undefined);
	vi.stubGlobal('IntersectionObserver', class { constructor(private cb: IntersectionObserverCallback) {} observe(target: Element) { this.cb([{ isIntersecting: true, target } as IntersectionObserverEntry], this as unknown as IntersectionObserver); } disconnect() {} });
	Element.prototype.scrollIntoView = vi.fn();
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('t3code-style review panel', () => {
	it('loads diffs from the selected worker and branch without a session', async () => {
		const workerTarget = { workerId: 'worker-b', branch: 'main' };
		api.target = { worktreeId: null, sessionId: null, workerTarget };
		setup();
		await waitFor(() => expect(api.getFileDiff).toHaveBeenCalledWith('project', 'src/a.ts', false, undefined, workerTarget));
	});
	it('selects the first current entry when opened without a selection', async () => {
		const onSelect = vi.fn();
		render(ReviewDiffPanel, { projectId: 'project', files, loading: false, selected: null, onSelect, onClose: vi.fn(), embedded: true });
		await waitFor(() => expect(onSelect).toHaveBeenCalledWith(files[0]));
	});
	it('renders continuous diffs, totals and distinct staged/unstaged entries', async () => {
		const { container } = setup();
		await waitFor(() => expect(container.querySelectorAll('.diff-content')).toHaveLength(3));
		expect(screen.getByLabelText('5 additions, 3 deletions')).toBeTruthy();
		expect(api.getFileDiff).toHaveBeenCalledWith('project', 'src/a.ts', true, 'branch', 'session');
		expect(api.getFileDiff).toHaveBeenCalledWith('project', 'src/a.ts', false, 'branch', 'session');
	});
	it('switches stacked/split layout and wrapping without refetching', async () => {
		const { container } = setup();
		await waitFor(() => expect(container.querySelectorAll('.diff-content')).toHaveLength(3));
		await fireEvent.click(screen.getByLabelText('Split diff view'));
		expect(container.querySelectorAll('.sbs-table')).toHaveLength(3);
		expect(container.querySelector('.sbs-table .syntax')?.textContent).toBe('old value');
		await fireEvent.click(screen.getByLabelText('Wrap diff lines'));
		expect(container.querySelectorAll('.diff-viewer.wrap')).toHaveLength(3);
		await fireEvent.click(screen.getByLabelText('Stacked diff view'));
		expect(container.querySelectorAll('.diff-content')).toHaveLength(3);
		expect(api.getFileDiff).toHaveBeenCalledTimes(3);
		expect(container.querySelector('.diff-content .syntax')?.textContent).toBe('old value');
	});
	it('collapses all files and reveals a file from the optional tree', async () => {
		const { container } = setup();
		await fireEvent.click(screen.getByLabelText('Collapse all files'));
		expect(container.querySelectorAll('.file-body')).toHaveLength(0);
		await fireEvent.click(screen.getByLabelText('Toggle file tree'));
		await fireEvent.click(screen.getByTitle('src/a.ts (staged)'));
		expect(container.querySelectorAll('.file-body')).toHaveLength(1);
		expect(Element.prototype.scrollIntoView).toHaveBeenCalled();
	});
	it('filters scope and reloads unchanged paths on refresh', async () => {
		setup();
		await waitFor(() => expect(api.getFileDiff).toHaveBeenCalledTimes(3));
		await fireEvent.change(screen.getByLabelText('Diff scope'), { target: { value: 'staged' } });
		expect(screen.getByLabelText('3 additions, 2 deletions')).toBeTruthy();
		expect(screen.queryByLabelText('new.txt (untracked)')).toBeNull();
		await fireEvent.click(screen.getByLabelText('Refresh diff'));
		await waitFor(() => expect(api.getFileDiff).toHaveBeenCalledTimes(4));
		expect(api.refreshChangedFiles).toHaveBeenCalledOnce();
	});
	it('shows an empty state rather than an empty viewport', () => {
		render(ReviewDiffPanel, { projectId: 'project', files: [], loading: false, selected: null, onSelect: vi.fn(), onClose: vi.fn(), embedded: true });
		expect(screen.getByText('No changes in your working tree.')).toBeTruthy();
	});
});

describe('review diff loading', () => {
	it('retains binary and truncated warnings', async () => {
		api.getFileDiff.mockResolvedValueOnce({ is_binary: true, diff: '' });
		const view = render(DiffViewer, { projectId: 'project', file: files[0], onClose: vi.fn(), embedded: true, continuous: true });
		await screen.findByText('Binary file — cannot display diff');
		api.getFileDiff.mockResolvedValueOnce({ diff: patch, is_binary: false, truncated: true });
		await view.rerender({ refreshKey: 1 });
		await screen.findByText('Diff output was truncated — only showing partial changes');
	});
	it('ignores a stale request after changing files', async () => {
		let resolveOld!: (value: unknown) => void;
		api.getFileDiff.mockReturnValueOnce(new Promise(resolve => { resolveOld = resolve; }));
		const view = render(DiffViewer, { projectId: 'project', file: files[0], onClose: vi.fn() });
		await view.rerender({ file: files[2] });
		await screen.findByText('new value', { exact: false });
		resolveOld({ is_binary: true, diff: '' });
		await Promise.resolve();
		expect(screen.queryByText('Binary file — cannot display diff')).toBeNull();
	});
});

describe('changes vs base scope', () => {
	const baseRows: FileChangeInfo[] = [
		{ file: 'src/a.ts', category: 'base', status: 'M', additions: 4, deletions: 1, is_binary: false, change_origin: 'committed' },
		{ file: 'src/b.ts', category: 'base', status: 'R', additions: 1, deletions: 0, is_binary: false, change_origin: 'both', original_file: 'src/old-b.ts' },
	];
	const baseResponse = { files: baseRows, total_additions: 5, total_deletions: 1, base_ref: 'origin/main', merge_base: '0123456789abcdef0123456789abcdef01234567' };
	beforeEach(() => {
		api.getBaseChangedFiles.mockResolvedValue(baseResponse);
		api.getBaseFileDiff.mockResolvedValue({ file: 'src/a.ts', diff: patch, is_binary: false, truncated: false, base_ref: 'origin/main', merge_base: baseResponse.merge_base });
	});

	it('switches to the session base comparison and back', async () => {
		const onScopeChange = vi.fn();
		const { container } = render(ReviewDiffPanel, { projectId: 'project', files, loading: false, selected: files[0], onSelect: vi.fn(), onClose: vi.fn(), embedded: true, onScopeChange });
		await fireEvent.click(screen.getByRole('button', { name: 'Changes vs base' }));
		expect(onScopeChange).toHaveBeenCalledWith('base');
		expect(api.getBaseChangedFiles).toHaveBeenCalledWith('project', 'session');
		await screen.findByText('origin/main');
		expect(screen.getByRole('button', { name: /Copy merge-base SHA/ }).textContent).toBe('0123456');
		expect(screen.getByText('Committed')).toBeTruthy();
		expect(screen.getByText('Committed + local')).toBeTruthy();
		expect(screen.getByLabelText('5 additions, 1 deletions')).toBeTruthy();
		// The uncommitted-only sub-filter does not apply to the base comparison.
		expect(screen.queryByLabelText('Diff scope')).toBeNull();
		await waitFor(() => expect(api.getBaseFileDiff).toHaveBeenCalledWith('project', 'src/a.ts', 'session'));
		expect(container.querySelectorAll('.diff-file')).toHaveLength(2);

		await fireEvent.click(screen.getByRole('button', { name: 'Uncommitted' }));
		expect(onScopeChange).toHaveBeenLastCalledWith('working');
		expect(screen.getByLabelText('Diff scope')).toBeTruthy();
		expect(container.querySelectorAll('.diff-file')).toHaveLength(3);
	});

	it('is unavailable without a chat session', async () => {
		api.target = { worktreeId: null, sessionId: null, workerTarget: { workerId: 'worker-b', branch: 'main' } };
		setup();
		const base = screen.getByRole('button', { name: 'Changes vs base' }) as HTMLButtonElement;
		expect(base.disabled).toBe(true);
		await fireEvent.click(base);
		expect(api.getBaseChangedFiles).not.toHaveBeenCalled();
	});

	it('shows an empty state naming the base', async () => {
		api.getBaseChangedFiles.mockResolvedValueOnce({ ...baseResponse, files: [] });
		setup();
		await fireEvent.click(screen.getByRole('button', { name: 'Changes vs base' }));
		await screen.findByText('No changes since origin/main.');
	});

	it('explains a failed comparison and can retry', async () => {
		api.getBaseChangedFiles.mockRejectedValueOnce({ detail: 'Branch has no merge base with origin/main' });
		const { container } = setup();
		await fireEvent.click(screen.getByRole('button', { name: 'Changes vs base' }));
		await screen.findByText('Branch has no merge base with origin/main');
		expect(container.querySelectorAll('.diff-file')).toHaveLength(0);
		expect(screen.getByRole('button', { name: 'Show uncommitted' })).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
		await screen.findByText('origin/main');
		expect(api.getBaseChangedFiles).toHaveBeenCalledTimes(2);
	});
});

describe('diff line feedback', () => {
	const multi = 'diff --git a/src/a.ts b/src/a.ts\n--- a/src/a.ts\n+++ b/src/a.ts\n@@ -1,4 +1,4 @@\n first\n-old value\n+new value\n middle\n last\n';
	function viewer(props: Record<string, unknown> = {}) {
		const onAskAgent = vi.fn().mockResolvedValue(true);
		const view = render(DiffViewer, { projectId: 'project', file: files[0], onClose: vi.fn(), embedded: true, continuous: true, onAskAgent, ...props });
		return { ...view, onAskAgent: (props.onAskAgent ?? onAskAgent) as typeof onAskAgent };
	}
	const gutter = (container: HTMLElement, row: number) => container.querySelector(`[data-row="${row}"] .gutter-col`)!;

	beforeEach(() => { api.getFileDiff.mockResolvedValue({ file: 'src/a.ts', diff: multi, is_binary: false, truncated: false }); });

	it('selects a range from line numbers and sends one composed message', async () => {
		const { container, onAskAgent } = viewer();
		await screen.findByText('new value');
		// Rows: 0 header, 1 first, 2 -old, 3 +new, 4 middle, 5 last.
		await fireEvent.pointerDown(gutter(container, 2), { button: 0, pointerType: 'mouse' });
		// The card waits for the drag to end, so it cannot push rows under the pointer.
		expect(container.querySelector('.ask-card')).toBeNull();
		await fireEvent.pointerMove(gutter(container, 3), { pointerType: 'mouse' });
		expect(container.querySelectorAll('.row-selected')).toHaveLength(2);
		await fireEvent.pointerUp(window);
		expect(container.querySelector('.ask-card')).not.toBeNull();
		await fireEvent.pointerDown(gutter(container, 4), { button: 0, pointerType: 'mouse', shiftKey: true });
		await fireEvent.pointerUp(window);
		expect(container.querySelectorAll('.row-selected')).toHaveLength(3);
		expect(screen.getByText('L2–3')).toBeTruthy();
		const input = screen.getByLabelText('Comment for the agent');
		await fireEvent.input(input, { target: { value: 'Rename this' } });
		await fireEvent.keyDown(input, { key: 'Enter' });
		await waitFor(() => expect(onAskAgent).toHaveBeenCalledOnce());
		expect(onAskAgent.mock.calls[0][0]).toBe([
			'Review comment on `src/a.ts`, lines new 2-3, old 2-3, in the uncommitted changes (unstaged):',
			'', '```diff', '-old value', '+new value', ' middle', '```', '', 'Rename this',
		].join('\n'));
		await waitFor(() => expect(container.querySelector('.ask-card')).toBeNull());
	});

	it('lets the owner edit the full message before sending', async () => {
		const { container, onAskAgent } = viewer();
		await screen.findByText('new value');
		await fireEvent.pointerDown(gutter(container, 3), { button: 0, pointerType: 'mouse' });
		await fireEvent.pointerUp(window);
		await fireEvent.input(screen.getByLabelText('Comment for the agent'), { target: { value: 'Check this' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Edit message' }));
		const draft = screen.getByLabelText('Message to send') as HTMLTextAreaElement;
		expect(draft.value).toContain('+new value');
		expect(draft.value.endsWith('Check this')).toBe(true);
		await fireEvent.input(draft, { target: { value: 'My own wording' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Send' }));
		await waitFor(() => expect(onAskAgent).toHaveBeenCalledWith('My own wording'));
	});

	it('keeps the text and reports when sending fails', async () => {
		const { container } = viewer({ onAskAgent: vi.fn().mockResolvedValue(false) });
		await screen.findByText('new value');
		await fireEvent.pointerDown(gutter(container, 3), { button: 0, pointerType: 'mouse' });
		await fireEvent.pointerUp(window);
		const input = screen.getByLabelText('Comment for the agent') as HTMLTextAreaElement;
		await fireEvent.input(input, { target: { value: 'Keep me' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Send' }));
		await screen.findByRole('alert');
		expect(input.value).toBe('Keep me');
	});

	it('extends with a second tap on touch', async () => {
		const { container } = viewer();
		await screen.findByText('new value');
		const row = (i: number) => container.querySelector(`[data-row="${i}"]`)!;
		await fireEvent.pointerDown(row(1), { pointerType: 'touch' });
		await fireEvent.click(row(1));
		expect(container.querySelectorAll('.row-selected')).toHaveLength(1);
		expect(screen.getByText('Tap another line to select a range')).toBeTruthy();
		await fireEvent.pointerDown(row(5), { pointerType: 'touch' });
		await fireEvent.click(row(5));
		expect(container.querySelectorAll('.row-selected')).toHaveLength(5);
	});

	it('selects with the keyboard and clears with Escape', async () => {
		const { container } = viewer();
		await screen.findByText('new value');
		const lines = screen.getByRole('group', { name: 'Diff lines of src/a.ts' });
		await fireEvent.keyDown(lines, { key: 'ArrowDown' });
		await fireEvent.keyDown(lines, { key: 'ArrowDown', shiftKey: true });
		expect(container.querySelectorAll('.row-selected')).toHaveLength(2);
		const escape = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true });
		lines.dispatchEvent(escape);
		expect(escape.defaultPrevented).toBe(true);
		await waitFor(() => expect(container.querySelectorAll('.row-selected')).toHaveLength(0));
	});

	it('offers no selection on truncated diffs', async () => {
		api.getFileDiff.mockResolvedValueOnce({ file: 'src/a.ts', diff: multi, is_binary: false, truncated: true });
		const { container } = viewer();
		await screen.findByText('· line comments are off');
		await fireEvent.pointerDown(gutter(container, 3), { button: 0, pointerType: 'mouse' });
		expect(container.querySelector('.row-selected')).toBeNull();
		expect(screen.queryByRole('group', { name: 'Diff lines of src/a.ts' })).toBeNull();
	});

	it('stays read-only without an ask handler', async () => {
		const { container } = viewer({ onAskAgent: undefined });
		await screen.findByText('new value');
		await fireEvent.pointerDown(gutter(container, 3), { button: 0, pointerType: 'mouse' });
		expect(container.querySelector('.row-selected')).toBeNull();
	});
});

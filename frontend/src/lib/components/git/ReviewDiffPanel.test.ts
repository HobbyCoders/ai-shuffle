import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import ReviewDiffPanel from './ReviewDiffPanel.svelte';
import DiffViewer from './DiffViewer.svelte';
import type { FileChangeInfo } from '$lib/api/git';

const api = vi.hoisted(() => ({ getFileDiff: vi.fn(), refreshChangedFiles: vi.fn(), target: { worktreeId: 'branch', sessionId: 'session', workerTarget: null } as { worktreeId: string | null; sessionId: string | null; workerTarget: {workerId: string; branch: string} | null } }));
vi.mock('$lib/api/git', () => ({ getFileDiff: api.getFileDiff }));
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

/**
 * Behavior tests for FilesTreeNode — the recursive folder tree used by the
 * Files page sidebar.
 *
 * What matters here is the refresh contract:
 *  - a node lazily fetches its child directories once and caches them
 *  - bumping the `treeVersion` prop drops the cache and refetches, so expanded
 *    nodes never keep showing folders that were renamed or deleted elsewhere
 *  - a collapsed node with cached children refetches on next expand
 *
 * fetch is mocked at the global level; the component calls
 * `/api/v1/workspace/files?path=...` directly.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/svelte';
import FilesTreeNode from './FilesTreeNode.svelte';

const fetchMock = vi.fn();

function jsonResponse(files: Array<{ name: string; type: string; path: string }>) {
	return {
		ok: true,
		json: async () => ({ files })
	};
}

beforeEach(() => {
	vi.stubGlobal('fetch', fetchMock);
	fetchMock.mockReset();
});

afterEach(() => {
	cleanup();
	vi.unstubAllGlobals();
});

function dirs(paths: string[]) {
	return jsonResponse(
		paths.map((p) => ({
			name: p.split('/').pop() ?? p,
			type: 'directory',
			path: p
		}))
	);
}

describe('FilesTreeNode', () => {
	it('renders its name collapsed and does not fetch until expanded', () => {
		render(FilesTreeNode, {
			props: { name: 'alpha', path: 'alpha', depth: 0, currentPath: '', showHidden: false, onNavigate: vi.fn() }
		});
		expect(screen.getByText('alpha')).toBeTruthy();
		expect(fetchMock).not.toHaveBeenCalled();
	});

	it('fetches child directories when expanded', async () => {
		fetchMock.mockResolvedValue(dirs(['alpha/one', 'alpha/two']));
		render(FilesTreeNode, {
			props: { name: 'alpha', path: 'alpha', depth: 0, currentPath: '', showHidden: false, onNavigate: vi.fn() }
		});
		await fireEvent.click(screen.getByRole('button', { name: 'Expand' }));
		await waitFor(() => expect(screen.getByText('one')).toBeTruthy());
		expect(screen.getByText('two')).toBeTruthy();
		expect(fetchMock).toHaveBeenCalledTimes(1);
	});

	it('caches children: collapsing and re-expanding does not refetch', async () => {
		fetchMock.mockResolvedValue(dirs(['alpha/one']));
		render(FilesTreeNode, {
			props: { name: 'alpha', path: 'alpha', depth: 0, currentPath: '', showHidden: false, onNavigate: vi.fn() }
		});
		await fireEvent.click(screen.getByRole('button', { name: 'Expand' }));
		await waitFor(() => expect(screen.getByText('one')).toBeTruthy());
		await fireEvent.click(screen.getByRole('button', { name: 'Collapse' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Expand' }));
		await waitFor(() => expect(screen.getByText('one')).toBeTruthy());
		expect(fetchMock).toHaveBeenCalledTimes(1);
	});

	it('refetches when treeVersion bumps, dropping stale children', async () => {
		fetchMock.mockResolvedValueOnce(dirs(['alpha/one', 'alpha/old']));
		const { rerender } = render(FilesTreeNode, {
			props: { name: 'alpha', path: 'alpha', depth: 0, currentPath: '', showHidden: false, onNavigate: vi.fn(), treeVersion: 0 }
		});
		await fireEvent.click(screen.getByRole('button', { name: 'Expand' }));
		await waitFor(() => expect(screen.getByText('old')).toBeTruthy());

		// Simulate a rename/delete elsewhere: the parent bumps treeVersion and
		// the API now reports a different set of child directories.
		fetchMock.mockResolvedValueOnce(dirs(['alpha/one', 'alpha/new']));
		await rerender({
			name: 'alpha',
			path: 'alpha',
			depth: 0,
			currentPath: '',
			showHidden: false,
			onNavigate: vi.fn(),
			treeVersion: 1
		});
		await waitFor(() => expect(screen.getByText('new')).toBeTruthy());
		expect(screen.queryByText('old')).toBeNull();
		expect(fetchMock).toHaveBeenCalledTimes(2);
	});

	it('passes treeVersion down to child nodes so they refresh too', async () => {
		fetchMock.mockImplementation(async (url: string) => {
			// URLSearchParams encodes '/' as %2F — parse, don't substring-match.
			const p = new URL(url, 'http://localhost').searchParams.get('path');
			if (p === 'alpha') return dirs(['alpha/beta']);
			return dirs(['alpha/beta/gamma']);
		});
		const { rerender } = render(FilesTreeNode, {
			props: { name: 'alpha', path: 'alpha', depth: 0, currentPath: '', showHidden: false, onNavigate: vi.fn(), treeVersion: 0 }
		});
		// Expand alpha, then expand beta (its caret is the second one rendered).
		await fireEvent.click(screen.getByRole('button', { name: 'Expand' }));
		await waitFor(() => expect(screen.getByText('beta')).toBeTruthy());
		const carets = document.querySelectorAll('.ftn-caret');
		await fireEvent.click(carets[1]);
		await waitFor(() => expect(screen.getByText('gamma')).toBeTruthy());

		// Bump the version: alpha refetches, and beta — which receives the same
		// version prop — refetches too. Neither node may keep a stale subtree.
		await rerender({
			name: 'alpha',
			path: 'alpha',
			depth: 0,
			currentPath: '',
			showHidden: false,
			onNavigate: vi.fn(),
			treeVersion: 1
		});
		await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(4));
		expect(screen.getByText('gamma')).toBeTruthy();
	});
});

import { afterEach, describe, expect, it, vi } from 'vitest';
import { generateCommitMessage, generatePRDescription, startShipOperation } from './git';

const selection = 'prov:strong:vendor/astra';
afterEach(() => vi.unstubAllGlobals());

function mockFetch() {
	const fetch = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }));
	vi.stubGlobal('fetch', fetch);
	return fetch;
}

describe('Git generation request bindings', () => {
	it('serializes the commit model alongside the captured checkout scope', async () => {
		const fetch = mockFetch();
		await generateCommitMessage('p1', { model: selection, include_unstaged: false }, 'wt1', 's1');
		const [url, request] = fetch.mock.calls[0];
		expect(url).toBe('/api/v1/projects/p1/git/generate-commit-message?worktree_id=wt1&session_id=s1');
		expect(JSON.parse(request.body)).toEqual({ model: selection, include_unstaged: false });
	});

	it('serializes the PR model without changing the base branch', async () => {
		const fetch = mockFetch();
		await generatePRDescription('p1', { model: selection, base_branch: 'release' });
		const [url, request] = fetch.mock.calls[0];
		expect(url).toBe('/api/v1/projects/p1/git/generate-pr-description');
		expect(JSON.parse(request.body)).toEqual({ model: selection, base_branch: 'release' });
	});

	it('serializes the Ship It binding for both generators', async () => {
		const fetch = mockFetch();
		await startShipOperation('p1', { generation_model: selection, merge_method: 'squash', cleanup: false, include_unstaged: true });
		const [url, request] = fetch.mock.calls[0];
		expect(url).toBe('/api/v1/projects/p1/git/ship');
		expect(JSON.parse(request.body)).toMatchObject({ generation_model: selection });
	});

	it('omits the binding when the Settings default is selected', async () => {
		const fetch = mockFetch();
		await generateCommitMessage('p1', { model: undefined });
		expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({});
	});
});

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import type { InboxPullRequest, PullRequestInbox } from '$lib/api/github';

const getPullRequestInbox = vi.fn<(state?: string, limit?: number) => Promise<PullRequestInbox>>();
vi.mock('$lib/api/github', () => ({ getPullRequestInbox: (...args: [string?, number?]) => getPullRequestInbox(...args) }));

import PrInboxCard from './PrInboxCard.svelte';
import { activityAge, filterInbox, inboxProjects, DEFAULT_FILTERS } from './pr-inbox';

const pr = (over: Partial<InboxPullRequest>): InboxPullRequest => ({
	repo: 'o/alpha', number: 1, title: 'PR', url: 'https://github.com/o/alpha/pull/1', author: 'me',
	head_branch: 'feat', base_branch: 'main', state: 'open', review: 'none', checks: 'passing',
	updated_at: '2026-10-02T09:00:00Z', projects: [{ id: 'p-a', name: 'Alpha' }], ...over
});

const items = [
	pr({ number: 1, title: 'Add inbox', review: 'approved', checks: 'passing' }),
	pr({ number: 2, title: 'Fix flake', review: 'changes_requested', checks: 'failing' }),
	pr({ repo: 'o/beta', number: 3, title: 'Draft idea', state: 'draft', checks: 'pending', projects: [{ id: 'p-b', name: 'Beta' }] })
];

describe('pr-inbox helpers', () => {
	it('filters by review, checks, project and drafts', () => {
		expect(filterInbox(items, { ...DEFAULT_FILTERS, review: 'approved' }).map((p) => p.number)).toEqual([1]);
		expect(filterInbox(items, { ...DEFAULT_FILTERS, checks: 'failing' }).map((p) => p.number)).toEqual([2]);
		expect(filterInbox(items, { ...DEFAULT_FILTERS, project: 'p-b' }).map((p) => p.number)).toEqual([3]);
		expect(filterInbox(items, { ...DEFAULT_FILTERS, hideDrafts: true }).map((p) => p.number)).toEqual([1, 2]);
	});

	it('lists each project once, by name', () => {
		expect(inboxProjects(items)).toEqual([{ id: 'p-a', name: 'Alpha' }, { id: 'p-b', name: 'Beta' }]);
	});

	it('shortens the activity age', () => {
		const now = Date.parse('2026-10-02T12:00:00Z');
		expect(activityAge('2026-10-02T11:55:00Z', now)).toBe('5m');
		expect(activityAge('2026-10-02T09:00:00Z', now)).toBe('3h');
		expect(activityAge('2026-09-30T12:00:00Z', now)).toBe('2d');
		expect(activityAge('nope', now)).toBe('');
	});
});

describe('PrInboxCard', () => {
	beforeEach(() => {
		getPullRequestInbox.mockReset();
		getPullRequestInbox.mockResolvedValue({
			items,
			skipped: [{ project_id: 'p-x', project_name: 'Scratch', reason: 'No GitHub remote' }],
			repos: ['o/alpha', 'o/beta']
		});
	});
	afterEach(cleanup);

	it('lists pull requests from every project and filters them', async () => {
		render(PrInboxCard);
		const list = await screen.findByRole('list', { name: 'Pull requests' });
		expect(within(list).getAllByRole('listitem')).toHaveLength(3);
		expect(screen.getByRole('link', { name: /Add inbox/ }).getAttribute('href')).toBe('https://github.com/o/alpha/pull/1');
		expect(screen.getByText('o/beta#3')).toBeTruthy();
		expect(screen.getByText(/1 project or repository not included/)).toBeTruthy();

		await fireEvent.change(screen.getByRole('combobox', { name: 'Checks' }), { target: { value: 'failing' } });
		await waitFor(() => expect(within(list).getAllByRole('listitem')).toHaveLength(1));
		expect(screen.getByRole('link', { name: /Fix flake/ })).toBeTruthy();

		await fireEvent.change(screen.getByRole('combobox', { name: 'Review' }), { target: { value: 'approved' } });
		await screen.findByText('No pull requests match these filters.');
		await fireEvent.click(screen.getAllByRole('button', { name: 'Clear filters' })[0]);
		await waitFor(() => expect(screen.getAllByRole('listitem').length).toBeGreaterThanOrEqual(3));

		await fireEvent.change(screen.getByRole('combobox', { name: 'Project' }), { target: { value: 'p-b' } });
		await waitFor(() => expect(screen.queryByRole('link', { name: /Add inbox/ })).toBeNull());
		expect(screen.getByRole('link', { name: /Draft idea/ })).toBeTruthy();
	});

	it('asks the server again when the state changes', async () => {
		render(PrInboxCard);
		await screen.findByRole('list', { name: 'Pull requests' });
		expect(getPullRequestInbox).toHaveBeenLastCalledWith('open');
		await fireEvent.click(screen.getByRole('button', { name: 'Merged' }));
		await waitFor(() => expect(getPullRequestInbox).toHaveBeenLastCalledWith('merged'));
	});

	it('explains a GitHub CLI that is not signed in', async () => {
		getPullRequestInbox.mockRejectedValueOnce({ status: 401, detail: 'GitHub CLI is not authenticated.' });
		render(PrInboxCard);
		expect((await screen.findByRole('alert')).textContent).toMatch(/gh auth login/);
	});

	it('says when no project has a GitHub remote', async () => {
		getPullRequestInbox.mockResolvedValueOnce({ items: [], skipped: [], repos: [] });
		render(PrInboxCard);
		expect(await screen.findByText('No project has a GitHub remote yet.')).toBeTruthy();
	});
});

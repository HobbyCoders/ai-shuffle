import { describe, expect, it } from 'vitest';
import { accountSummary, githubSummary, historySummary, inviteJoinState, type AccountSummaryInput, type GithubSummaryInput } from './extras-summary';

const account = (over: Partial<AccountSummaryInput> = {}): AccountSummaryInput => ({
	choice: 'account', syncing: false, joinedOther: false, networkName: null, linked: false, waitingOnAccount: false, joining: false, ...over,
});
const github = (over: Partial<GithubSummaryInput> = {}): GithubSummaryInput => ({
	loading: false, authenticated: false, user: null, pending: false, error: '', ...over,
});

describe('accountSummary', () => {
	it('says what an account is for while nothing is signed in', () => {
		expect(accountSummary(account())).toEqual({ tone: 'idle', text: expect.stringContaining('Not signed in') });
		expect(accountSummary(account({ choice: 'invite' })).text).toBe('Join another worker with its invite');
	});

	it('follows a sign-in and a join through to syncing', () => {
		expect(accountSummary(account({ linked: true })).tone).toBe('busy');
		expect(accountSummary(account({ linked: true, waitingOnAccount: true }))).toEqual({ tone: 'done', text: "Signed in · joining your account's network" });
		expect(accountSummary(account({ choice: 'invite', joining: true })).text).toBe('Joining with a worker invite…');
		expect(accountSummary(account({ joinedOther: true, networkName: 'Home' }))).toEqual({ tone: 'done', text: 'Syncing settings with Home' });
		expect(accountSummary(account({ joinedOther: true })).text).toBe('Syncing settings with your other workers');
		expect(accountSummary(account({ syncing: true, joinedOther: true })).tone).toBe('busy');
	});

	it('says an invite that did not go through is an error to open', () => {
		expect(accountSummary(account({ choice: 'invite', joinFailed: true }))).toEqual({ tone: 'error', text: "Invite didn't go through · open to retry" });
	});
});

describe('inviteJoinState', () => {
	it('reads the enrollment the way the join flow does', () => {
		expect(inviteJoinState(false, 'pending')).toBe('joining');
		expect(inviteJoinState(false, 'syncing')).toBe('joining');
		for (const state of ['failed', 'rejected', 'expired']) expect(inviteJoinState(false, state)).toBe('failed');
		expect(inviteJoinState(false, 'complete')).toBeNull();
		expect(inviteJoinState(false, null)).toBeNull();
		expect(inviteJoinState(true, 'failed')).toBeNull();
	});
});

describe('githubSummary', () => {
	it('names the connected user before anything else', () => {
		expect(githubSummary(github({ authenticated: true, user: 'octocat', error: 'old' }))).toEqual({ tone: 'done', text: 'Connected as @octocat' });
		expect(githubSummary(github({ authenticated: true })).text).toBe('Connected');
	});

	it('shows a pending sign-in, an error, the first check and the idle hint', () => {
		expect(githubSummary(github({ pending: true })).tone).toBe('busy');
		expect(githubSummary(github({ error: 'gh failed' })).tone).toBe('error');
		expect(githubSummary(github({ loading: true })).text).toBe('Checking GitHub…');
		expect(githubSummary(github())).toEqual({ tone: 'idle', text: expect.stringContaining('Not connected') });
	});
});

describe('historySummary', () => {
	it('counts what was found and what was imported', () => {
		expect(historySummary({ remaining: 1, imported: 0 })).toEqual({ tone: 'idle', text: '1 conversation found from the Claude Code CLI' });
		expect(historySummary({ remaining: 0, imported: 3 })).toEqual({ tone: 'done', text: 'Imported 3 conversations' });
		expect(historySummary({ remaining: 2, imported: 3 }).text).toBe('Imported 3 · 2 more found');
	});
});

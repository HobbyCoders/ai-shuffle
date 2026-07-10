/**
 * Tests for the multiple-Claude-account API client functions in auth.ts.
 *
 * These exercise the request shape (method, path, body) and the error surface
 * the UI depends on — most importantly that a 409 (encryption locked /
 * can't-delete-active) throws `{ detail, status }` so the component can show the
 * server's message verbatim.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
	listClaudeAccounts,
	startAddClaudeAccount,
	completeAddClaudeAccount,
	activateClaudeAccount,
	renameClaudeAccount,
	deleteClaudeAccount,
	setClaudeAutoSwitchOnLimit,
	type ClaudeAccount
} from './auth';

const mockAccount: ClaudeAccount = {
	id: 'acc-1',
	name: 'Work',
	email: 'work@example.com',
	subscription_type: 'max',
	is_active: true,
	expires_at: 9999999999999,
	has_refresh_token: true,
	sync_pending: false,
	last_used_at: 1700000000000,
	created_at: 1699000000000
};

function okJson(body: unknown) {
	return { ok: true, status: 200, json: () => Promise.resolve(body) };
}

function errJson(status: number, body: unknown) {
	return { ok: false, status, json: () => Promise.resolve(body), text: () => Promise.resolve('') };
}

describe('Claude accounts API', () => {
	let fetchMock: ReturnType<typeof vi.fn>;

	beforeEach(() => {
		fetchMock = vi.fn();
		global.fetch = fetchMock as unknown as typeof fetch;
	});

	afterEach(() => {
		vi.restoreAllMocks();
	});

	describe('listClaudeAccounts', () => {
		it('GETs the accounts endpoint and returns the payload', async () => {
			const payload = {
				accounts: [mockAccount],
				active_account_id: 'acc-1',
				encryption_ready: true,
				auto_switch_on_limit: false
			};
			fetchMock.mockResolvedValueOnce(okJson(payload));

			const res = await listClaudeAccounts();

			expect(fetchMock).toHaveBeenCalledWith('/api/v1/auth/claude/accounts', {
				method: 'GET',
				headers: { 'Content-Type': 'application/json' },
				credentials: 'include',
				body: undefined,
				signal: undefined,
				keepalive: undefined
			});
			expect(res.accounts).toHaveLength(1);
			expect(res.active_account_id).toBe('acc-1');
			expect(res.encryption_ready).toBe(true);
			expect(res.auto_switch_on_limit).toBe(false);
		});

		it('surfaces encryption_ready:false from the server', async () => {
			fetchMock.mockResolvedValueOnce(
				okJson({ accounts: [], active_account_id: null, encryption_ready: false })
			);
			const res = await listClaudeAccounts();
			expect(res.encryption_ready).toBe(false);
			expect(res.accounts).toEqual([]);
		});
	});

	describe('startAddClaudeAccount', () => {
		it('POSTs the start endpoint with no body', async () => {
			fetchMock.mockResolvedValueOnce(
				okJson({ success: true, oauth_url: 'https://x/y', state: 'st-1' })
			);

			const res = await startAddClaudeAccount();

			expect(fetchMock).toHaveBeenCalledWith('/api/v1/auth/claude/accounts/start', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				credentials: 'include',
				body: undefined,
				signal: undefined,
				keepalive: undefined
			});
			expect(res.oauth_url).toBe('https://x/y');
			expect(res.state).toBe('st-1');
		});
	});

	describe('completeAddClaudeAccount', () => {
		it('POSTs only code+state when name/makeActive omitted', async () => {
			fetchMock.mockResolvedValueOnce(
				okJson({ success: true, account: mockAccount, active_account_id: 'acc-1' })
			);

			await completeAddClaudeAccount('the-code', 'st-1');

			expect(fetchMock).toHaveBeenCalledWith(
				'/api/v1/auth/claude/accounts/complete',
				expect.objectContaining({
					method: 'POST',
					body: JSON.stringify({ code: 'the-code', state: 'st-1' })
				})
			);
		});

		it('includes name and make_active when provided', async () => {
			fetchMock.mockResolvedValueOnce(
				okJson({ success: true, account: mockAccount, active_account_id: 'acc-1' })
			);

			await completeAddClaudeAccount('the-code', 'st-1', 'Personal', true);

			expect(fetchMock).toHaveBeenCalledWith(
				'/api/v1/auth/claude/accounts/complete',
				expect.objectContaining({
					body: JSON.stringify({
						code: 'the-code',
						state: 'st-1',
						name: 'Personal',
						make_active: true
					})
				})
			);
		});

		it('throws { detail, status:409 } when encryption is locked', async () => {
			fetchMock.mockResolvedValueOnce(
				errJson(409, { detail: 'Encryption is locked — log in again' })
			);

			await expect(completeAddClaudeAccount('c', 's')).rejects.toMatchObject({
				status: 409,
				detail: 'Encryption is locked — log in again'
			});
		});
	});

	describe('activateClaudeAccount', () => {
		it('POSTs the activate endpoint for the id', async () => {
			fetchMock.mockResolvedValueOnce(okJson({ success: true, active_account_id: 'acc-2' }));

			const res = await activateClaudeAccount('acc-2');

			expect(fetchMock).toHaveBeenCalledWith(
				'/api/v1/auth/claude/accounts/acc-2/activate',
				expect.objectContaining({ method: 'POST' })
			);
			expect(res.active_account_id).toBe('acc-2');
		});

		it('propagates a 404 for an unknown id', async () => {
			fetchMock.mockResolvedValueOnce(errJson(404, { detail: 'Unknown account' }));
			await expect(activateClaudeAccount('nope')).rejects.toMatchObject({ status: 404 });
		});

		it('propagates a 409 when encryption is locked', async () => {
			fetchMock.mockResolvedValueOnce(errJson(409, { detail: 'locked' }));
			await expect(activateClaudeAccount('acc-2')).rejects.toMatchObject({ status: 409 });
		});
	});

	describe('renameClaudeAccount', () => {
		it('PATCHes the account with the new name', async () => {
			fetchMock.mockResolvedValueOnce(
				okJson({ success: true, account: { ...mockAccount, name: 'Renamed' } })
			);

			const res = await renameClaudeAccount('acc-1', 'Renamed');

			expect(fetchMock).toHaveBeenCalledWith(
				'/api/v1/auth/claude/accounts/acc-1',
				expect.objectContaining({
					method: 'PATCH',
					body: JSON.stringify({ name: 'Renamed' })
				})
			);
			expect(res.account.name).toBe('Renamed');
		});
	});

	describe('deleteClaudeAccount', () => {
		it('DELETEs the account', async () => {
			fetchMock.mockResolvedValueOnce(okJson({ success: true }));

			await deleteClaudeAccount('acc-1');

			expect(fetchMock).toHaveBeenCalledWith(
				'/api/v1/auth/claude/accounts/acc-1',
				expect.objectContaining({ method: 'DELETE' })
			);
		});

		it('throws { detail, status:409 } with the "switch first" message', async () => {
			fetchMock.mockResolvedValueOnce(
				errJson(409, { detail: 'Switch to another account before deleting the active one.' })
			);

			await expect(deleteClaudeAccount('acc-1')).rejects.toMatchObject({
				status: 409,
				detail: 'Switch to another account before deleting the active one.'
			});
		});
	});

	describe('setClaudeAutoSwitchOnLimit', () => {
		it('PUTs the auto-switch endpoint with the enabled flag', async () => {
			fetchMock.mockResolvedValueOnce(
				okJson({ success: true, auto_switch_on_limit: true })
			);

			const res = await setClaudeAutoSwitchOnLimit(true);

			expect(fetchMock).toHaveBeenCalledWith(
				'/api/v1/auth/claude/accounts/auto-switch',
				expect.objectContaining({
					method: 'PUT',
					body: JSON.stringify({ enabled: true })
				})
			);
			expect(res.success).toBe(true);
			expect(res.auto_switch_on_limit).toBe(true);
		});

		it('sends enabled:false to turn the setting off', async () => {
			fetchMock.mockResolvedValueOnce(
				okJson({ success: true, auto_switch_on_limit: false })
			);

			const res = await setClaudeAutoSwitchOnLimit(false);

			expect(fetchMock).toHaveBeenCalledWith(
				'/api/v1/auth/claude/accounts/auto-switch',
				expect.objectContaining({
					method: 'PUT',
					body: JSON.stringify({ enabled: false })
				})
			);
			expect(res.auto_switch_on_limit).toBe(false);
		});

		it('propagates a 403 when the caller is not admin', async () => {
			fetchMock.mockResolvedValueOnce(errJson(403, { detail: 'Admin only' }));
			await expect(setClaudeAutoSwitchOnLimit(true)).rejects.toMatchObject({ status: 403 });
		});
	});
});

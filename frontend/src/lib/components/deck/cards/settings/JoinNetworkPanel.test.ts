import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import type { NetworkStatus } from '$lib/api/network';
const api = vi.hoisted(() => ({ post: vi.fn(), delete: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api, getErrorMessage: (e: Error) => e.message }));
import JoinNetworkPanel from './JoinNetworkPanel.svelte';
const fresh = (): NetworkStatus => ({ device: { device_id: 'self', fingerprint: 'abcd-'.repeat(7) + 'abcd' }, enrolled: false, platform: 'linux', version: '1.0', instance_id: 'self', enrollment: null });
const link = 'shuffle://network/join?endpoint=http%3A%2F%2F10.0.0.5%3A8000&code=ABCD-EFGH-JKLM&fp=abcd-abcd-abcd-abcd-abcd-abcd-abcd-abcd';
beforeEach(() => { vi.resetAllMocks(); api.post.mockResolvedValue({ join_id: 'j', status: 'approved', inviter: {}, your_fingerprint: 'x' }); });
afterEach(cleanup);

it('joins from a pasted invite link without any fingerprint confirmation', async () => {
	const onStarted = vi.fn();
	render(JoinNetworkPanel, { props: { status: fresh(), onStarted } });
	expect(screen.getByText(/Invite codes are for a worker on your home network or a VPN/).textContent).toContain('always talk over the encrypted tunnel');
	const join = screen.getByRole('button', { name: 'Join' }) as HTMLButtonElement;
	expect(join.disabled).toBe(true);
	await fireEvent.input(screen.getByLabelText('Invite link or code'), { target: { value: link } });
	expect(screen.getByText('http://10.0.0.5:8000')).toBeTruthy();
	expect(screen.queryByRole('checkbox')).toBeNull();
	expect(join.disabled).toBe(false);
	await fireEvent.click(join);
	expect(api.post).toHaveBeenCalledExactlyOnceWith('/network/enroll', { link });
	expect(onStarted).toHaveBeenCalledOnce();
});

it('accepts an address and a hand-typed code', async () => {
	render(JoinNetworkPanel, { props: { status: fresh(), deviceName: 'Laptop' } });
	await fireEvent.input(screen.getByLabelText('Worker address'), { target: { value: 'http://10.0.0.5:8000' } });
	await fireEvent.input(screen.getByLabelText('Code'), { target: { value: 'ABCD-EFGH-JKLM' } });
	await fireEvent.click(screen.getByRole('button', { name: 'Join' }));
	expect(api.post).toHaveBeenCalledWith('/network/enroll', { endpoint_url: 'http://10.0.0.5:8000', code: 'ABCD-EFGH-JKLM', device_name: 'Laptop' });
});

it('refuses a phone pairing link and explains the difference', async () => {
	render(JoinNetworkPanel, { props: { status: fresh() } });
	await fireEvent.input(screen.getByLabelText('Invite link or code'), { target: { value: 'https://worker.example.com/pair#token=ABCDEFGHJKLM' } });
	expect(screen.getByText(/That is a phone or browser pairing link/)).toBeTruthy();
	expect((screen.getByRole('button', { name: 'Join' }) as HTMLButtonElement).disabled).toBe(true);
});

it('shows joining progress and lets it be cancelled, then a failure with dismiss', async () => {
	const pending = { ...fresh(), enrollment: { id: 'j', status: 'pending', endpoint_url: 'http://10.0.0.5:8000', inviter_fingerprint: null, fingerprint: null, error: null } };
	const onChanged = vi.fn();
	const view = render(JoinNetworkPanel, { props: { status: pending, onChanged } });
	expect(screen.getByRole('status').textContent).toContain('Joining http://10.0.0.5:8000');
	expect(screen.queryByText(/approv/i)).toBeNull();
	await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
	expect(api.delete).toHaveBeenCalledWith('/network/enroll');
	expect(onChanged).toHaveBeenCalledOnce();
	await view.rerender({ status: { ...pending, enrollment: { ...pending.enrollment, status: 'expired', error: 'Approval timed out' } }, onChanged });
	expect(screen.getByRole('alert').textContent).toContain('The last join expired: Approval timed out');
	await fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
	expect(api.delete).toHaveBeenCalledTimes(2);
});

it('asks for confirmation before an enrolled worker leaves its network', async () => {
	render(JoinNetworkPanel, { props: { status: { ...fresh(), enrolled: true, enrollment: undefined }, replace: true } });
	await fireEvent.input(screen.getByLabelText('Invite link or code'), { target: { value: link } });
	await fireEvent.click(screen.getByRole('button', { name: 'Join another network' }));
	expect(api.post).not.toHaveBeenCalled();
	expect(screen.getByRole('alert').textContent).toContain('leaves its current network');
	await fireEvent.click(screen.getByRole('button', { name: 'Leave and join' }));
	expect(api.post).toHaveBeenCalledExactlyOnceWith('/network/enroll', { link, replace: true });
});

import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import ProviderBalance from './ProviderBalance.svelte';

const mocks = vi.hoisted(() => ({ get: vi.fn(), save: vi.fn(), remove: vi.fn(), start: vi.fn() }));
vi.mock('$lib/api/provider-balances', () => ({
 balancePath: (id: string) => `/model-providers/${encodeURIComponent(id)}/balance-access`,
 getProviderBalance: mocks.get, saveProviderBalance: mocks.save,
 removeProviderBalance: mocks.remove, startPlatformSignIn: mocks.start
}));
const status = { configured: true, source: 'OpenAI Platform balance (unofficial)', fetched_at: Date.now(),
 status: 'fresh', needs_reauth: false, credits: { balance_remaining: 12.34 } };
afterEach(() => { cleanup(); vi.resetAllMocks(); vi.unstubAllGlobals(); });

it('shows real remaining credit, source and fetched time; writes but never reads session keys', async () => {
 mocks.get.mockResolvedValue(status); mocks.save.mockResolvedValue(status);
 render(ProviderBalance, { providerId: 'p1', openAI: true });
 await screen.findByText('$12.34');
 expect(screen.getByText(/Last fetched:/)).toBeTruthy();
 expect(screen.getAllByText('OpenAI Platform balance (unofficial)')).toHaveLength(2);
 const input = screen.getByLabelText('Platform session key') as HTMLInputElement;
 expect(input.type).toBe('password');
 await fireEvent.input(input, { target: { value: 'sess-test-only' } });
 await fireEvent.click(screen.getByRole('button', { name: 'Save access' }));
 await waitFor(() => expect(mocks.save).toHaveBeenCalledWith('p1', 'sess-test-only'));
 expect(input.value).toBe('');
});
it('expired OpenAI session requests a new sign-in without a stale amount', async () => {
 mocks.get.mockResolvedValue({ ...status, credits: null, needs_reauth: true, status: 'unavailable' });
 render(ProviderBalance, { providerId: 'p1', openAI: true });
 await screen.findByText(/Session expired or refused/);
 expect(screen.queryByText('$12.34')).toBeNull();
 expect(screen.getByRole('button', { name: 'Sign in to OpenAI Platform' })).toBeTruthy();
});
it('xAI provides management-key controls, cycle spend and limit, not OpenAI sign-in', async () => {
 mocks.get.mockResolvedValue({ ...status, source: 'xAI Management API', credits: { balance_remaining: 0, spend_month: 3, spending_limit: 200 } });
 mocks.remove.mockResolvedValue({ ...status, configured: false, credits: null });
 render(ProviderBalance, { providerId: 'x1', openAI: false });
 await screen.findByText('$0.00');
 expect(screen.getByText('$3.00')).toBeTruthy();
 expect(screen.getByText('$200.00')).toBeTruthy();
 expect(screen.getByLabelText('xAI management key')).toBeTruthy();
 expect(screen.queryByRole('button', { name: 'Sign in to OpenAI Platform' })).toBeNull();
 await fireEvent.click(screen.getByRole('button', { name: 'Disconnect balance' }));
 await waitFor(() => expect(mocks.remove).toHaveBeenCalledWith('x1'));
});
it('starts owner sign-in with a one-shot ticket, never a URL credential', async () => {
 mocks.get.mockResolvedValue({ ...status, configured: false });
 mocks.start.mockResolvedValue({ ticket: 'one-shot-ticket' });
 class Socket {
  static OPEN = 1; readyState = 1; onopen?: () => void; onmessage?: (e: { data: string }) => void;
  onclose?: () => void; onerror?: () => void;
  send = vi.fn(); close = vi.fn();
  constructor(public url: string) { sockets.push(this); }
 }
 const sockets: Socket[] = [];
 vi.stubGlobal('WebSocket', Socket);
 render(ProviderBalance, { providerId: 'p1', openAI: true });
 await screen.findByText('$12.34');
 await fireEvent.click(screen.getByRole('button', { name: 'Sign in to OpenAI Platform' }));
 await waitFor(() => expect(sockets).toHaveLength(1));
 expect(sockets[0].url).toContain('/balance-access/browser');
 expect(sockets[0].url).not.toContain('one-shot-ticket');
 sockets[0].onopen?.();
 expect(sockets[0].send).toHaveBeenCalledWith(JSON.stringify({ ticket: 'one-shot-ticket' }));
 await fireEvent.click(screen.getByRole('button', { name: 'Cancel sign-in' }));
 expect(sockets[0].close).toHaveBeenCalled();
});

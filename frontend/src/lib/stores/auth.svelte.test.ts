import { beforeEach, expect, it, vi } from 'vitest';
const mocks = vi.hoisted(() => ({ status: vi.fn(), logout: vi.fn() }));
vi.mock('$lib/api/auth', () => ({ getAuthStatus: mocks.status, logout: mocks.logout }));
const account = (id: string, extra = {}) => ({ authenticated: true,
 preference_identity: { user_type: 'account', user_id: `hub#${id}` }, ...extra });
const alice = account('alice'), bob = account('bob'), anonymous = { authenticated: false };
function deferred<T>() {
 let resolve!: (value: T) => void, reject!: (error: unknown) => void;
 const promise = new Promise<T>((a, b) => { resolve = a; reject = b; });
 return { promise, resolve, reject };
}
let auth: (typeof import('./auth.svelte'))['auth'];
beforeEach(async () => {
 vi.resetModules(); mocks.status.mockReset(); mocks.logout.mockReset().mockResolvedValue(undefined);
 auth = (await import('./auth.svelte')).auth;
});
it('keeps the newer account when an earlier status check returns last', async () => {
 const old = deferred<unknown>();
 mocks.status.mockReturnValueOnce(old.promise).mockResolvedValueOnce(bob);
 const first = auth.checkAuth(); await auth.checkAuth();
 old.resolve(alice);
 expect(await first).toEqual(bob);
 expect(auth.current.status).toEqual(bob);
});
it('retires a pending status check immediately when logout starts', async () => {
 mocks.status.mockResolvedValueOnce(alice); await auth.checkAuth();
 const old = deferred<unknown>(), logout = deferred<void>();
 mocks.status.mockReturnValueOnce(old.promise).mockResolvedValueOnce(anonymous);
 mocks.logout.mockReturnValueOnce(logout.promise);
 const checking = auth.checkAuth(), leaving = auth.logout();
 old.resolve(alice); await checking;
 expect(auth.current.status?.authenticated).toBe(false);
 logout.resolve(); await leaving;
 expect(auth.current.status).toEqual(anonymous);
});
it('does not let the logout status response replace a later sign-in result', async () => {
 const old = deferred<unknown>();
 mocks.status.mockReturnValueOnce(old.promise).mockResolvedValueOnce(bob);
 const leaving = auth.logout();
 await vi.waitFor(() => expect(mocks.status).toHaveBeenCalledOnce());
 await auth.checkAuth();
 old.resolve(anonymous); await leaving;
 expect(auth.current.status).toEqual(bob);
});
it('waits for an in-flight logout before a status refresh reads the cookie', async () => {
 const logout = deferred<void>(); mocks.logout.mockReturnValueOnce(logout.promise);
 mocks.status.mockResolvedValueOnce(anonymous);
 const leaving = auth.logout(), checking = auth.checkAuth();
 expect(mocks.status).not.toHaveBeenCalled();
 logout.resolve(); await Promise.all([leaving, checking]);
 expect(mocks.status).toHaveBeenCalledOnce();
 expect(auth.current.status).toEqual(anonymous);
 expect(auth.current.loading).toBe(false);
});
it('ignores a superseded failure without changing the latest error/loading state', async () => {
 const old = deferred<unknown>();
 mocks.status.mockReturnValueOnce(old.promise).mockResolvedValueOnce(bob);
 const first = auth.checkAuth();
 const failed = expect(first).rejects.toThrow('old failure');
 await auth.checkAuth(); old.reject(new Error('old failure')); await failed;
 expect(auth.current).toMatchObject({ status: bob, loading: false, error: null });
});
it('still publishes fresh same-account credential and onboarding changes', async () => {
 mocks.status.mockResolvedValueOnce(alice).mockResolvedValueOnce(account('alice', {
  has_any_credential: true, onboarding_complete: true
 }));
 await auth.checkAuth(); await auth.checkAuth();
 expect(auth.current.status).toMatchObject({ preference_identity: alice.preference_identity,
  has_any_credential: true, onboarding_complete: true });
});

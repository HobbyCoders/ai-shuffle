import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render } from '@testing-library/svelte';
const calls = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: calls }));
import GoogleConnection from './GoogleConnection.svelte';
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.clearAllMocks(); });
it('keeps account connection separate from administrator credentials', async () => {
 calls.get.mockResolvedValue({configured:true,environment_managed:false,client_id:'client',redirect_uri:'http://localhost/callback'});
 const page = render(GoogleConnection, { onConnected: vi.fn() });
 expect(await page.findByRole('button',{name:'Connect with Google'})).toBeTruthy();
 expect(page.queryByLabelText('Google client secret')).toBeNull();
 await fireEvent.click(page.getByRole('button',{name:'Google app setup (administrator)'}));
 expect(page.getByLabelText('Google client secret').getAttribute('type')).toBe('password');
});
it('does not offer a nonfunctional sign-in when app setup is missing', async () => {
 calls.get.mockResolvedValue({configured:false,environment_managed:false,suggested_redirect_uri:'http://localhost/callback'});
 const page = render(GoogleConnection, { onConnected: vi.fn() });
 await page.findByRole('button',{name:'Google app setup (administrator)'});
 expect(page.queryByRole('button',{name:'Connect with Google'})).toBeNull();
});
it('reports a blocked popup before starting OAuth', async () => {
 calls.get.mockResolvedValue({configured:true,environment_managed:true});
 vi.spyOn(window,'open').mockReturnValue(null);
 const page = render(GoogleConnection, { onConnected: vi.fn() });
 await fireEvent.click(await page.findByRole('button',{name:'Connect with Google'}));
 expect(page.getByRole('alert').textContent).toContain('Allow pop-ups');
 expect(calls.post).not.toHaveBeenCalled();
});

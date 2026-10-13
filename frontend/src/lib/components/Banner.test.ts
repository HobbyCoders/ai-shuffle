import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import Banner from './Banner.svelte';
import BannerHost from './BannerHost.svelte';
import { notifications, notify } from '$lib/stores/notifications';
vi.mock('$lib/api/client', () => ({api: {post:vi.fn(() => Promise.resolve({}))}}));
vi.mock('$lib/stores/auth.svelte', () => ({auth:{current:{status:{authenticated:false}}}}));
afterEach(() => {cleanup(); for (const n of notifications.current.live) notify.dismissLive(n.id); notify.clearAll();});
describe('shared banner controls', () => {
 it.each(['inline','page'] as const)('renders accessible error and controls in %s variant', async (variant) => {
  const action = vi.fn(), dismiss = vi.fn();
  render(Banner, {variant, severity:'error', title:'Save failed', message:'Try again.', action:{label:'Retry',onClick:action}, dismissible:true, onDismiss:dismiss});
  expect(screen.getByRole('alert').textContent).toContain('Save failed');
  await fireEvent.click(screen.getByRole('button',{name:'Retry'}));
  await fireEvent.click(screen.getByRole('button',{name:'Dismiss'}));
  expect(action).toHaveBeenCalledOnce(); expect(dismiss).toHaveBeenCalledOnce();
 });
 it('announces non-error banners politely without offering an unwired dismiss control', () => {
  render(Banner, {severity:'info',message:'Ready'});
  expect(screen.getByRole('status').textContent).toContain('Ready');
  expect(screen.queryByRole('button')).toBeNull();
 });
 it('hosts only explicit banners, runs their actions and removes them', async () => {
  const action = vi.fn();
  notify.success('Saved to bell');
  notify.banner('Update available',{severity:'info',action:{label:'Refresh',onClick:action}});
  render(BannerHost);
  expect(screen.queryByText('Saved to bell')).toBeNull();
  await fireEvent.click(screen.getByRole('button',{name:'Refresh'}));
  expect(action).toHaveBeenCalledOnce();
  expect(screen.queryByText('Update available')).toBeNull();
 });
 it('dismisses a hosted banner', async () => {
  notify.banner('Connection changed'); render(BannerHost);
  await fireEvent.click(screen.getByRole('button',{name:'Dismiss'}));
  expect(notifications.current.live).toEqual([]);
 });
});

import { cleanup, render, screen } from '@testing-library/svelte';
import { afterEach, describe, expect, it } from 'vitest';
import { FileText, Pin } from 'lucide-svelte';
import ListItem from './ListItem.svelte';

afterEach(cleanup);
describe('shared panel row grammar', () => {
 it('does not repeat decorative document tiles', () => {
  const { container } = render(ListItem, { id: 'note', name: 'A note', icon: FileText, selected: false });
  expect(container.querySelector('svg')).toBeNull();
  expect(screen.getByRole('button').classList.contains('has-lead')).toBe(false);
 });
 it('keeps meaningful pinned-note art', () => {
  const { container } = render(ListItem, { id: 'note', name: 'A pinned note', icon: Pin, selected: false });
  expect(container.querySelector('svg')).toBeTruthy();
  expect(screen.getByRole('button').classList.contains('has-lead')).toBe(true);
 });
 it('offers an opt-in card row with subtitle, real metadata and a navigation chevron', () => {
  const { container } = render(ListItem, { id: 'plugin', name: 'My plugin', summary: 'Runs locally', meta: 'Off', variant: 'card', selected: false });
  expect(screen.getByRole('button').classList.contains('card')).toBe(true);
  expect(screen.getByText('Runs locally')).toBeTruthy();
  expect(screen.getByText('Off')).toBeTruthy();
  expect(container.querySelector('.chev')).toBeTruthy();
 });
 it('puts the marker in the leading slot so list-panel titles share one text edge', () => {
  const { container } = render(ListItem, { id: 'note', name: 'A pinned note', icon: Pin, selected: false });
  const lead = container.querySelector('.st-it > .lead');
  expect(lead?.querySelector('svg')).toBeTruthy();
  // The title follows the slot as its own grid column, never inside it.
  expect(lead?.textContent).not.toContain('A pinned note');
  expect(container.querySelector('.st-it > .tx')?.textContent).toContain('A pinned note');
 });
 it('exposes one-line navigation density without a page-specific selector', () => {
  render(ListItem, { id: 'open', name: 'Open', rowKind: 'navigation', selected: false });
  expect(screen.getByRole('button').classList.contains('navigation')).toBe(true);
 });
});

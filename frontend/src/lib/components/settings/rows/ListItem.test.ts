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
 it('exposes one-line navigation density without a page-specific selector', () => {
  render(ListItem, { id: 'open', name: 'Open', rowKind: 'navigation', selected: false });
  expect(screen.getByRole('button').classList.contains('navigation')).toBe(true);
 });
});

import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render } from '@testing-library/svelte';
import { AVATARS } from '$lib/api/assistant';
import AgentAvatar from './AgentAvatar.svelte';

afterEach(cleanup);

describe('AgentAvatar', () => {
 it('draws every listed avatar from its own file', () => {
  const drawings = new Set<string>();
  for (const avatar of AVATARS) {
   const { container } = render(AgentAvatar, { avatar });
   const span = container.querySelector('.agent-avatar')!;
   expect(span.getAttribute('data-avatar')).toBe(avatar);
   const svg = span.querySelector('svg')!;
   expect(svg.getAttribute('viewBox')).toBe('0 0 64 64');
   expect(svg.querySelector('.shape')).toBeTruthy();
   drawings.add(svg.innerHTML);
   cleanup();
  }
  expect(drawings.size).toBe(AVATARS.length);
 });
 it('falls back to the robot for an id this build does not know', () => {
  const { container } = render(AgentAvatar, { avatar: 'constructor', size: 30 });
  const span = container.querySelector('.agent-avatar') as HTMLElement;
  expect(span.getAttribute('data-avatar')).toBe('robot');
  expect(span.style.width).toBe('30px');
  expect(span.querySelector('svg rect.shape')).toBeTruthy();
 });
});

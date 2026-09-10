import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import SubagentMessage from './SubagentMessage.svelte';
import type { ChatMessage } from '$lib/stores/types';
import { openSessionLink } from '$lib/services/session-navigation';

vi.mock('$lib/services/session-navigation', () => ({ openSessionLink: vi.fn((event: MouseEvent) => event.preventDefault()) }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });
const message: ChatMessage = {
 id: 'subagent-worker', role: 'assistant', type: 'subagent', content: 'Private worker result',
 toolId: 'worker', agentName: 'General Purpose', agentType: 'gpt-test', agentStatus: 'running',
 agentDescription: 'Review the code', agentSessionId: 'worker-session', agentPrompt: 'Private prompt',
 agentUsage: { tool_uses: 12, total_tokens: 20000 },
 agentChildren: [{ id: 'child', type: 'tool_use', content: '', toolName: 'Bash', toolStatus: 'running' }]
};

describe('Subagent transcript card', () => {
 it('keeps progress and output out of the parent and opens the worker transcript', async () => {
  const { container } = render(SubagentMessage, { message });
  expect(container.querySelector('details')?.open).toBe(false);
  expect(screen.getByRole('status').textContent).toBe('Running');
  for (const text of ['Bash', 'Private prompt', 'Private worker result', '12 tools', '20k tokens']) {
   expect(screen.queryByText(text)).toBeNull();
  }
  await fireEvent.click(container.querySelector('summary')!);
  const link = screen.getByRole('link', { name: 'Open transcript' });
  expect(link.getAttribute('href')).toBe('/?session=worker-session');
  await fireEvent.click(link);
  expect(openSessionLink).toHaveBeenCalledWith(expect.any(MouseEvent), 'worker-session');
 });
 it('honors active tasks even when the saved agent status says completed', async () => {
  const { rerender } = render(SubagentMessage, { message: { ...message, agentStatus: 'completed' }, runningToolIds: new Set(['worker']) });
  expect(screen.getByRole('status').textContent).toBe('Running');
  await rerender({ message: { ...message, agentStatus: 'completed' }, runningToolIds: new Set() });
  expect(screen.getByRole('status').textContent).toBe('Completed');
 });
 it.each(['pending', 'error'] as const)('shows %s without exposing nested activity', (agentStatus) => {
  render(SubagentMessage, { message: { ...message, agentStatus } });
  expect(screen.getByRole('status').textContent).toBe(agentStatus === 'error' ? 'Failed' : 'Pending');
 });
 it('does not invent session links from native SDK agent IDs', async () => {
  const { container } = render(SubagentMessage, { message: { ...message, agentSessionId: undefined, agentId: 'native-agent', agentStatus: 'completed' } });
  await fireEvent.click(container.querySelector('summary')!);
  expect(screen.queryByRole('link')).toBeNull();
  expect(screen.getByText('No separate transcript is available for this agent.')).toBeTruthy();
 });
});

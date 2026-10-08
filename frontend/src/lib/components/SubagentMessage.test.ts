import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { get } from 'svelte/store';
import SubagentMessage from './SubagentMessage.svelte';
import type { ChatMessage } from '$lib/stores/types';
import { openSubagentLink } from '$lib/services/session-navigation';
import { activeSession } from '$lib/stores/active-session';
import { layout } from '$lib/stores/layout.svelte';
import { subagentChats } from '$lib/stores/subagent-chats';

vi.mock('$lib/services/session-navigation', async (importOriginal) => {
 const original = await importOriginal<typeof import('$lib/services/session-navigation')>();
 return { ...original, openSubagentLink: vi.fn(original.openSubagentLink) };
});
afterEach(() => { cleanup(); history.replaceState(null, '', '/'); vi.clearAllMocks(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
const message: ChatMessage = {
 id: 'subagent-worker', role: 'assistant', type: 'subagent', content: 'Private worker result',
 toolId: 'worker', agentName: 'General Purpose', agentType: 'gpt-test', agentStatus: 'running',
 agentDescription: 'Review the code', agentSessionId: 'worker-session', agentPrompt: 'Private prompt',
 agentUsage: { tool_uses: 12, total_tokens: 20000 },
 agentChildren: [{ id: 'child', type: 'tool_use', content: '', toolName: 'Bash', toolStatus: 'running' }]
};

describe('Subagent transcript card', () => {
 it('keeps progress and output out of the parent and opens the worker transcript', async () => {
  vi.spyOn(activeSession, 'switchSession').mockImplementation(() => {});
  layout.setSidebarOverlay(false);
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
  expect(openSubagentLink).toHaveBeenCalledWith(expect.any(MouseEvent), 'worker-session');
 });
 it.each(['/mobile/', '/assets/mobile-ui/'])('keeps the Task fallback inside %s and opens the child with the real navigation handler', async pathname => {
  history.replaceState(null, '', pathname);
  layout.setSidebarOverlay(false);
  const switchSession = vi.spyOn(activeSession, 'switchSession').mockImplementation(() => {});
  const childId = 'worker / chat &+=?#雪';
  subagentChats.mark(childId, false);
  const { container } = render(SubagentMessage, { message: { ...message, agentSessionId: childId } });
  await fireEvent.click(container.querySelector('summary')!);
  const link = screen.getByRole('link', { name: 'Open transcript' });
  const target = new URL(link.getAttribute('href')!, window.location.origin);
  expect(target.origin).toBe(window.location.origin);
  expect(target.pathname).toBe(pathname);
  expect(target.searchParams.get('session')).toBe(childId);
  const event = new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 });
  await fireEvent(link, event);
  expect(event.defaultPrevented).toBe(true);
  expect(switchSession).toHaveBeenCalledExactlyOnceWith(childId, undefined, null, null, null);
  expect(get(subagentChats).has(childId)).toBe(true);
 });
 it('shows the model the worker runs on, as the model pickers name it (#1827)', async () => {
  const { rerender } = render(SubagentMessage, { message });
  expect(screen.getByText('gpt-test')).toBeTruthy();
  await rerender({ message: { ...message, agentModel: 'agent · sonnet' } });
  const chip = screen.getByText('Sonnet');
  expect(chip.getAttribute('title')).toBe('Model: agent · sonnet');
  expect(screen.queryByText('gpt-test')).toBeNull();
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
 it('reads stopped, not failed, and says who stopped it (#1806)', async () => {
  const { container } = render(SubagentMessage, { message: { ...message, agentStatus: 'stopped', agentStopNote: 'Stopped by user' } });
  expect(screen.getByRole('status').textContent).toBe('Stopped');
  expect(container.querySelector('.sa-glyph.is-stopped')).not.toBeNull();
  await fireEvent.click(container.querySelector('summary')!);
  expect(screen.getByText('Stopped by user')).toBeTruthy();
  expect(screen.queryByText('Private worker result')).toBeNull();
 });
 it('does not invent session links from native SDK agent IDs', async () => {
  const { container } = render(SubagentMessage, { message: { ...message, agentSessionId: undefined, agentId: 'native-agent', agentStatus: 'completed' } });
  await fireEvent.click(container.querySelector('summary')!);
  expect(screen.queryByRole('link')).toBeNull();
  expect(screen.getByText('No separate transcript is available for this agent.')).toBeTruthy();
 });
});

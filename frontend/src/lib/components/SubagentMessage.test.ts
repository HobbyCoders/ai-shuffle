import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import SubagentMessage from './SubagentMessage.svelte';
import type { ChatMessage } from '$lib/stores/types';
import { openSubagentLink } from '$lib/services/session-navigation';

vi.mock('$lib/services/session-navigation', () => ({ openSubagentLink: vi.fn((event: MouseEvent) => event.preventDefault()) }));
const stopApi = vi.hoisted(() => ({ get: vi.fn(), stop: vi.fn() }));
vi.mock('$lib/api/sessions', () => ({ getSessionTaskStopState: stopApi.get, stopSessionTask: stopApi.stop }));
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
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
  expect(openSubagentLink).toHaveBeenCalledWith(expect.any(MouseEvent), 'worker-session');
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
 it.each(['completed', 'error'] as const)('exposes scoped pending-work Stop on a %s card without changing its result status', async agentStatus => {
  vi.spyOn(document, 'hidden', 'get').mockReturnValue(false);
  vi.stubGlobal('IntersectionObserver', class {
   constructor(private callback: (entries: { isIntersecting: boolean }[]) => void) {}
   observe() { this.callback([{ isIntersecting: true }]); }
   disconnect() {}
  });
  stopApi.get.mockResolvedValue({ eligible: true, turn_id: 'automatic-wake' });
  stopApi.stop.mockResolvedValue({ ok: true });
  render(SubagentMessage, { message: { ...message, agentStatus }, parentSessionId: 'parent', canControl: true });
  const label = agentStatus === 'completed' ? 'Completed' : 'Failed';
  await waitFor(() => expect(screen.getByRole('button', { name: 'Stop pending work' })).toBeTruthy());
  expect(screen.getByRole('status').textContent).toBe(label);
  await fireEvent.click(screen.getByRole('button', { name: 'Stop pending work' }));
  expect(stopApi.stop).toHaveBeenCalledExactlyOnceWith('parent', 'worker-session', 'automatic-wake');
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Stop pending work' })).toBeNull());
  expect(screen.getByRole('status').textContent).toBe(label);
 });
 it('does not invent session links from native SDK agent IDs', async () => {
  const { container } = render(SubagentMessage, { message: { ...message, agentSessionId: undefined, agentId: 'native-agent', agentStatus: 'completed' } });
  await fireEvent.click(container.querySelector('summary')!);
  expect(screen.queryByRole('link')).toBeNull();
  expect(screen.getByText('No separate transcript is available for this agent.')).toBeTruthy();
 });
});

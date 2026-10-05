import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest';
import { render, cleanup, screen, fireEvent, waitFor } from '@testing-library/svelte';
import { swarms } from '$lib/stores/swarms';
import type { SwarmSnapshot } from '$lib/utils/swarm';
import ToolUseMessage from './ToolUseMessage.svelte';
import SwarmHistory from './SwarmHistory.svelte';
const snapshot: SwarmSnapshot = {
 id: 'sw-1', session_id: 'session-swarm', title: 'Review the release', status: 'running',
 created_at: '2026-09-07T10:00:00Z', updated_at: '2026-09-07T10:01:00Z',
 tasks: [
  { id: 'code', title: 'Review code', agent: 'Explore', depends_on: [], status: 'running', worker_session_id: 'worker-1' },
  { id: 'tests', title: 'Review tests', agent: 'Explore', depends_on: [], status: 'completed', result: 'Tests cover the new route.' },
  { id: 'report', title: 'Write release report', agent: 'Writer', depends_on: ['code', 'tests'], status: 'queued' }
 ]
};

beforeEach(() => vi.stubGlobal('fetch', vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify({ swarms: [] }), { status: 200 })))));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });
describe('Swarm card integration', () => {
 it('keeps the receipt compact and exposes stages/navigation through the swarm history', async () => {
  render(ToolUseMessage, { name: 'mcp__fluid__Swarm', status: 'complete', result: JSON.stringify({ swarm: snapshot }) });
  expect(screen.queryByTestId('swarm-card')).toBeNull();
  render(SwarmHistory, { sessionId: snapshot.session_id });
  expect(screen.getByTestId('swarm-card').closest('.swarm-history')).not.toBeNull();
  expect(screen.queryByText('Stage 1')).toBeNull();
  await fireEvent.click(screen.getByRole('button', { name: /Swarm Review the release/ }));
  expect(screen.getByText('Stage 1')).toBeTruthy(); expect(screen.getByText('Stage 2')).toBeTruthy();
  expect(screen.queryByText('Tests cover the new route.')).toBeNull();
  expect(screen.queryByRole('progressbar')).toBeNull();
  await fireEvent.click(screen.getByText('Review code'));
  expect(screen.getByRole('link', { name: 'Open transcript' }).getAttribute('href')).toBe('/?session=worker-1');
  const header = screen.getByRole('button', { name: /Swarm Review the release/ });
  await fireEvent.click(header);
  expect(header.getAttribute('aria-expanded')).toBe('false');
  expect(screen.queryByText('Stage 1')).toBeNull();
 });
 it('shows updates before the tool result and requires cancellation confirmation', async () => {
  const early = { ...snapshot, id: 'early', session_id: 'early-session' };
  swarms.receive(early);
  render(ToolUseMessage, { name: 'Swarm', status: 'running', sessionId: early.session_id, input: { title: early.title, tasks: early.tasks } });
  expect(screen.queryByTestId('swarm-card')).toBeNull();
  render(SwarmHistory, { sessionId: early.session_id });
  expect(screen.getByRole('status').textContent).toBe('running');
  await fireEvent.click(screen.getByRole('button', { name: /Swarm Review the release/ }));
  await fireEvent.click(screen.getByRole('button', { name: 'Cancel workflow' }));
  expect(screen.getByText('Cancel unfinished tasks?')).toBeTruthy();
  vi.mocked(fetch).mockImplementationOnce(() => Promise.resolve(new Response(JSON.stringify({ swarm: { ...early, status: 'cancelled', updated_at: '2026-09-07T10:05:00Z' } }), { status: 200 })));
  await fireEvent.click(screen.getByRole('button', { name: 'Cancel workflow' }));
  await waitFor(() => expect(screen.getByRole('status').textContent).toBe('cancelled'));
  expect(screen.queryByRole('button', { name: 'Cancel workflow' })).toBeNull();
 });

 it('keeps failed output in the transcript and explains tasks without a session', async () => {
  const failed = { ...snapshot, id: 'failed', session_id: 'failed-session', status: 'failed', tasks: [
   { ...snapshot.tasks[0], status: 'failed', error: 'Worker stopped unexpectedly.', result: 'Private detailed output' },
   { ...snapshot.tasks[2], status: 'blocked' }
  ] };
  render(ToolUseMessage, { name: 'Swarm', status: 'complete', result: JSON.stringify({ swarm: failed }) });
  expect(screen.queryByTestId('swarm-card')).toBeNull();
  render(SwarmHistory, { sessionId: failed.session_id });
  await fireEvent.click(screen.getByRole('button', { name: /Swarm Review the release/ }));
  await fireEvent.click(screen.getByText('Review code'));
  expect(screen.getByText('Worker stopped unexpectedly.')).toBeTruthy();
  expect(screen.queryByText('Private detailed output')).toBeNull();
  expect(screen.getByRole('link', { name: 'Open transcript' }).getAttribute('href')).toBe('/?session=worker-1');
  await fireEvent.click(screen.getByText('Write release report'));
  expect(screen.getByText('Dependencies must complete before this task can start.')).toBeTruthy();
  expect(screen.getByText('No separate transcript is available for this agent.')).toBeTruthy();
 });
 it('keeps ordinary Task tool rendering intact', () => {
  render(ToolUseMessage, { name: 'Task', status: 'complete', result: 'done' });
  expect(screen.queryByTestId('swarm-card')).toBeNull();
  expect(screen.getByText('Task')).toBeTruthy();
 });
});

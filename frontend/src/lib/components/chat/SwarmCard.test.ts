import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest';
import { render, cleanup, screen, fireEvent, waitFor } from '@testing-library/svelte';
import { swarms } from '$lib/stores/swarms';
import type { SwarmSnapshot } from '$lib/utils/swarm';
import ToolUseMessage from './ToolUseMessage.svelte';
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
 it('renders dependency stages, results and real worker navigation through a prefixed tool', async () => {
  render(ToolUseMessage, { name: 'mcp__fluid__Swarm', status: 'complete', result: JSON.stringify({ swarm: snapshot }) });
  expect(screen.getByTestId('swarm-card')).toBeTruthy();
  expect(screen.getByText('Stage 1')).toBeTruthy(); expect(screen.getByText('Stage 2')).toBeTruthy();
  expect(screen.getByText('Tests cover the new route.')).toBeTruthy();
  await fireEvent.click(screen.getByText('Review code'));
  expect(screen.getByRole('link', { name: 'Open Explore session' }).getAttribute('href')).toBe('/?session=worker-1');
  const header = screen.getByRole('button', { name: /Swarm Review the release/ });
  await fireEvent.click(header);
  expect(header.getAttribute('aria-expanded')).toBe('false');
  expect(screen.queryByText('Stage 1')).toBeNull();
 });
 it('shows updates before the tool result and requires cancellation confirmation', async () => {
  const early = { ...snapshot, id: 'early', session_id: 'early-session' };
  swarms.receive(early);
  render(ToolUseMessage, { name: 'Swarm', status: 'running', sessionId: early.session_id, input: { title: early.title, tasks: early.tasks } });
  expect(screen.getByText('1 of 3 tasks completed · 1 running')).toBeTruthy();
  await fireEvent.click(screen.getByRole('button', { name: 'Cancel workflow' }));
  expect(screen.getByText('Cancel unfinished tasks?')).toBeTruthy();
  vi.mocked(fetch).mockImplementationOnce(() => Promise.resolve(new Response(JSON.stringify({ swarm: { ...early, status: 'cancelled', updated_at: '2026-09-07T10:05:00Z' } }), { status: 200 })));
  await fireEvent.click(screen.getByRole('button', { name: 'Cancel workflow' }));
  await waitFor(() => expect(screen.getByText('Workflow cancelled. Finished results are kept.')).toBeTruthy());
  expect(screen.queryByRole('button', { name: 'Cancel workflow' })).toBeNull();
 });
 it('keeps ordinary Task tool rendering intact', () => {
  render(ToolUseMessage, { name: 'Task', status: 'complete', result: 'done' });
  expect(screen.queryByTestId('swarm-card')).toBeNull();
  expect(screen.getByText('Task')).toBeTruthy();
 });
});

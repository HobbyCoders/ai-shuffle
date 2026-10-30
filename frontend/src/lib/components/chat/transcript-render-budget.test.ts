import { cleanup, render } from '@testing-library/svelte';
import { afterEach, describe, expect, it } from 'vitest';
import { initialState, reduce, releaseMessages, type WireEvent } from '$lib/stores/session/v2/reducer';
import { buildTranscriptModel } from '$lib/utils/work-log';
import { createTranscriptWindow, selectedRowRange, transcriptItemCost, TRANSCRIPT_CONTENT_BUDGET } from './transcript-window';
import RichTranscriptWindow from './test-fixtures/RichTranscriptWindow.svelte';

// Real native producer path, real grouping and real AssistantMessage markdown /
// sanitizer. Dense tables, lists and fences matter, not just 120 empty wrappers.
function richHistory() {
 let state = initialState(), seq = 0;
 const emit = (type: string, fields: Record<string, unknown> = {}) => {
  seq++; state = reduce(state, { type, id: `rich-${seq}`, seq, session_id: 'rich-budget',
   created_at: '2026-01-01T00:00:00Z', ...fields } as WireEvent);
 };
 const section = "### Review\n\n- **Read path**: keep `session_id` and ordering.\n  - Preserve **cold**, **cached**, and **remote** reads.\n- **Read path**: keep `session_id` and ordering.\n  - Preserve **cold**, **cached**, and **remote** reads.\n- **Read path**: keep `session_id` and ordering.\n  - Preserve **cold**, **cached**, and **remote** reads.\n- **Read path**: keep `session_id` and ordering.\n  - Preserve **cold**, **cached**, and **remote** reads.\n\n| Stage | Input | Expected |\n|---|---|---|\n| snapshot | `limit=120` | **bounded** |\n| snapshot | `limit=120` | **bounded** |\n| snapshot | `limit=120` | **bounded** |\n| snapshot | `limit=120` | **bounded** |\n| snapshot | `limit=120` | **bounded** |\n| snapshot | `limit=120` | **bounded** |\n| snapshot | `limit=120` | **bounded** |\n| snapshot | `limit=120` | **bounded** |\n\n```python\ndef read_window(store, session_id):\n    rows = store.read(session_id, limit=120)\n    return [{\"id\": row[\"id\"], \"text\": row[\"content\"]} for row in rows]\n```\n\n";
 for (let i = 0; i < 100; i++) {
  const turn_id = `turn-${i}`;
  emit('turn.started', { turn_id });
  emit('message.user', { turn_id, content: `Review ${i}` });
  emit('tool.use', { turn_id, tool_use_id: `tool-${i}`, name: 'Read', input: { file_path: 'src/window.ts' } });
  emit('tool.result', { turn_id, tool_use_id: `tool-${i}`, output: section });
  emit('assistant.text_delta', { turn_id, text: `## Report ${i}

${section.repeat(8)}` });
  emit('turn.completed', { turn_id });
 }
 return releaseMessages(state);
}

afterEach(() => { window.getSelection()?.removeAllRanges(); cleanup(); });
describe('rich nested DOM ratchet', () => {
 it('keeps fewer than 12,000 elements with no growth after twenty bidirectional shifts', async () => {
  const items = buildTranscriptModel(richHistory(), { sessionId: 'rich-budget', isStreaming: false, groupingEnabled: true }).items;
  const window = createTranscriptWindow({
   cost: import.meta.env.VITE_PERF_TRANSCRIPT_CONTENT_WINDOW !== '0' ? i => transcriptItemCost(items[i]) : undefined,
   budget: TRANSCRIPT_CONTENT_BUDGET
  });
  const view = render(RichTranscriptWindow, { items, range: window.setItemCount(items.length) });
  const initial = view.container.querySelectorAll('*').length;
  expect(view.container.querySelectorAll('.msg-virtualized').length).toBeLessThanOrEqual(120);
  expect(initial).toBeLessThan(12000);
  for (let i = 0; i < 20; i++) {
   const range = i % 2 === 0 ? window.extendOlder() : window.extendNewer();
   await view.rerender({ range });
   expect(view.container.querySelectorAll('*').length).toBeLessThan(12000);
  }
  await view.rerender({ range: window.toTail() });
  expect(view.container.querySelectorAll('*').length).toBe(initial);
 }, 60000);

 it.each([false, true])('retains native selected text nodes through twenty navigation attempts (reverse=%s)', async reverse => {
  const items = buildTranscriptModel(richHistory(), { sessionId: 'rich-budget', isStreaming: false, groupingEnabled: true }).items;
  let root: HTMLElement | null = null;
  const windowed = createTranscriptWindow({
   cost: i => transcriptItemCost(items[i]), budget: TRANSCRIPT_CONTENT_BUDGET,
   getPinnedRange: () => selectedRowRange(root, ':scope > [data-transcript-index]', row => Number(row.dataset.transcriptIndex))
  });
  const view = render(RichTranscriptWindow, { items, range: windowed.setItemCount(items.length) });
  root = view.container;
  const rows = root.querySelectorAll('[data-transcript-index]');
  const start = document.createTreeWalker(rows[rows.length - 3], NodeFilter.SHOW_TEXT).nextNode()!;
  const endWalker = document.createTreeWalker(rows[rows.length - 1], NodeFilter.SHOW_TEXT);
  let end: Node = endWalker.nextNode()!, next: Node | null;
  while ((next = endWalker.nextNode())) end = next;
  const selection = document.getSelection()!;
  if (reverse) selection.setBaseAndExtent(end, end.textContent!.length, start, 0);
  else selection.setBaseAndExtent(start, 0, end, end.textContent!.length);
  const selected = selection.toString();
  expect(selected.length).toBeGreaterThan(800);
  for (let i = 0; i < 20; i++) {
   const range = i < 10 ? windowed.loadOlder() : windowed.extendNewer();
   await view.rerender({ range });
   expect(start.isConnected && end.isConnected).toBe(true);
   expect(selection.toString()).toBe(selected);
   expect(root.querySelectorAll('.msg-virtualized').length).toBeLessThanOrEqual(120);
   expect(root.querySelectorAll('*').length).toBeLessThan(12000);
  }
  const before = windowed.visibleRange;
  selection.collapseToStart();
  const released = windowed.loadOlder();
  expect(released.start).toBeLessThan(before.start);
  await view.rerender({ range: released });
 }, 60000);
});

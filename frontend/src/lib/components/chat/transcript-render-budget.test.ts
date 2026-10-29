import { cleanup, render } from '@testing-library/svelte';
import { afterEach, describe, expect, it } from 'vitest';
import { initialState, reduce, releaseMessages, type WireEvent } from '$lib/stores/session/v2/reducer';
import { buildTranscriptModel } from '$lib/utils/work-log';
import { createTranscriptWindow, transcriptItemCost, TRANSCRIPT_CONTENT_BUDGET } from './transcript-window';
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

afterEach(cleanup);
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
});

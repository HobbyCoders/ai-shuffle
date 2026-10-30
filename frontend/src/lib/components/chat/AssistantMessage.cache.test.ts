import { cleanup, render } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import * as markdown from '$lib/utils/chat-markdown';
import AssistantMessage, { finishedMessageCache } from './AssistantMessage.svelte';
import { clearTables } from '$lib/utils/table-export';
import { clearCommandBlocks } from '$lib/terminal/commands';

afterEach(() => { cleanup(); finishedMessageCache.clear(); vi.restoreAllMocks(); vi.unstubAllEnvs(); });

describe('finished message render ratchet', () => {
 it('parses and sanitizes identical finished content only once across ten remounts', () => {
  const parse = vi.spyOn(markdown, 'renderChatMarkdown');
  const content = '## Cached report 806\n\nA **finished** reply with `inline code`.\n\n[Download](/api/files/by-path?path=%2Freport.txt)';
  for (let i = 0; i < 10; i++) {
   const view = render(AssistantMessage, { content, streaming: false, sessionId: `session-${i}` });
   expect(view.getByRole('link', { name: 'Download' }).getAttribute('href')).toContain(`session_id=session-${i}`);
   cleanup();
  }
  expect(parse).toHaveBeenCalledTimes(1);
 });
 it('does not reuse rewritten content, streaming bodies, or stale table/command registrations', () => {
  const parse = vi.spyOn(markdown, 'renderChatMarkdown');
  const content = '| Before | After |\n|---|---|\n| 1 | 2 |\n\n```sh\necho safe\n```';
  render(AssistantMessage, { content, streaming: false }); cleanup();
  render(AssistantMessage, { content, streaming: false }); cleanup();
  expect(parse).toHaveBeenCalledTimes(1);
  clearTables(); clearCommandBlocks();
  render(AssistantMessage, { content, streaming: false }); cleanup();
  expect(parse).toHaveBeenCalledTimes(2);
  render(AssistantMessage, { content: content + '\nChanged', streaming: false }); cleanup();
  expect(parse).toHaveBeenCalledTimes(3);
  const size = finishedMessageCache.stats().entries;
  render(AssistantMessage, { content: 'Still arriving', streaming: true }); cleanup();
  expect(finishedMessageCache.stats().entries).toBe(size);
 });
 it('bounds entries/bytes and supports the kill switch', () => {
  for (let i = 0; i < 170; i++) {
   render(AssistantMessage, { content: `Finished cached answer ${i}`, streaming: false }); cleanup();
  }
  expect(finishedMessageCache.stats().entries).toBe(160);
  expect(finishedMessageCache.stats().bytes).toBeLessThanOrEqual(8 * 1024 * 1024);
  finishedMessageCache.clear();
  vi.stubEnv('VITE_PERF_FINISHED_MARKDOWN_CACHE', '0');
  const parse = vi.spyOn(markdown, 'renderChatMarkdown');
  for (let i = 0; i < 3; i++) { render(AssistantMessage, { content: 'No cache', streaming: false }); cleanup(); }
  expect(parse).toHaveBeenCalledTimes(3);
  expect(finishedMessageCache.stats().entries).toBe(0);
 });
});

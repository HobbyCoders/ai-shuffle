import { cleanup, render, waitFor } from '@testing-library/svelte';
import { afterEach, describe, expect, it } from 'vitest';
import AssistantMessage from './AssistantMessage.svelte';

afterEach(cleanup);
describe('chat file routing', () => {
 it('routes produced markdown links and invalidates cached routing on session changes', async () => {
  const content = '[Report](/api/files/by-path?path=%2Flinux%2Freport.zip)';
  const view = render(AssistantMessage, { content, streaming: false, sessionId: 'remote-chat' });
  await waitFor(() => expect(view.getByRole('link', { name: 'Report' }).getAttribute('href')).toContain('session_id=remote-chat'));
  await view.rerender({ sessionId: 'other-chat' });
  await waitFor(() => expect(view.getByRole('link', { name: 'Report' }).getAttribute('href')).toContain('session_id=other-chat'));
 });
});

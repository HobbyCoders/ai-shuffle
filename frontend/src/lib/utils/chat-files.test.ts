import { afterEach, describe, expect, it } from 'vitest';
import { chatFileUrl, routeChatFiles } from './chat-files';
afterEach(() => { delete window.__SHUFFLE_MOBILE__; });
describe('chat file URLs', () => {
 it.each(['images', 'videos', 'audio'])('routes generated %s players and downloads', (kind) => {
  const html = `<video src="/api/generated-${kind}/clip.mp4"></video><a href="/api/generated-${kind}/by-path?path=%2Fwork%2Fa%20%26%20b.mp4&amp;session_id=old">Download</a>`;
  const routed = routeChatFiles(html, 'owner-chat');
  expect(routed).toContain(`/clip.mp4?session_id=owner-chat`);
  expect(routed).toContain('path=%2Fwork%2Fa+%26+b.mp4&amp;session_id=owner-chat');
  window.__SHUFFLE_MOBILE__ = { apiOrigin: 'https://viewer', token: 'test' };
  expect(chatFileUrl(`https://viewer/api/generated-${kind}/clip.mp4`, 'chat')).toBe(`https://viewer/api/generated-${kind}/clip.mp4?session_id=chat`);
  expect(chatFileUrl(`https://external/api/generated-${kind}/clip.mp4`, 'chat')).toBe(`https://external/api/generated-${kind}/clip.mp4`);
 });
 it('preserves special filenames and replaces stale session hints', () => {
  const path = '/work/a & # (新).zip';
  const url = new URL(chatFileUrl(`/api/files/by-path?path=${encodeURIComponent(path)}&session_id=old`, 'new'), 'https://viewer');
  expect(url.searchParams.get('path')).toBe(path);
  expect(url.searchParams.getAll('session_id')).toEqual(['new']);
 });
 it('leaves external links and escaped code alone', () => {
  expect(chatFileUrl('https://other/api/files/by-path?path=x', 'chat')).toBe('https://other/api/files/by-path?path=x');
  const code = '&lt;a href=&quot;/api/files/by-path?path=x&quot;&gt;';
  expect(routeChatFiles(code, 'chat')).toBe(code);
 });
 it('routes sanitized attributes without introducing markup', () => {
  const html = routeChatFiles('<a href="/api/files/by-path?path=%22%3E%3Cscript%3E&amp;session_id=old">File</a>', 'chat');
  expect(html).toContain('&amp;session_id=chat');
  expect(html).not.toContain('<script>');
 });
 it('keeps legacy local URLs and uses the bundled worker origin', () => {
  const href = '/api/files/by-path?path=x';
  expect(chatFileUrl(href)).toBe(href);
  window.__SHUFFLE_MOBILE__ = { apiOrigin: 'https://worker', token: 'test' };
  expect(chatFileUrl(href, 'chat')).toBe('https://worker/api/files/by-path?path=x&session_id=chat');
  expect(routeChatFiles('<img src="https://worker/api/files/by-path?path=x">', 'chat')).toContain('session_id=chat');
  const external = '<a href="https://other/api/files/by-path?path=x">External</a>';
  expect(routeChatFiles(external, 'chat')).toBe(external);
 });
});

// @vitest-environment node
import { expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { compile } from 'svelte/compiler';
import { transform } from 'esbuild';

it('eliminates the live flight-recorder reader, registration and cleanup from release JavaScript', async () => {
  const source = readFileSync(resolve('src/lib/components/layout/ChatView.svelte'), 'utf8');
  const { js } = compile(source, { filename: 'ChatView.svelte', dev: false, generate: 'client' });
  const dev = await transform(js.code, { define: { 'import.meta.env.DEV': 'true' }, minifySyntax: true });
  const release = await transform(js.code, { define: { 'import.meta.env.DEV': 'false' }, minifySyntax: true });
  expect(dev.code).toContain('__flightRecorderState');
  expect(release.code).not.toContain('__flightRecorderState');
  expect(release.code).not.toContain('document.querySelector');
});

// editImage request routing (#1796). No network: fal.subscribe and fetch are stubbed.
import { test, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fal } from '@fal-ai/client';
import { editImage } from '../dist/image-generation/editImage.js';
import { knownEditRoute, editImageFieldFromOpenapi, buildEditInput } from '../dist/providers/image/fal-ai.js';

const dir = mkdtempSync(join(tmpdir(), 'edit-image-test-'));
const source = join(dir, 'wip.png');
writeFileSync(source, Buffer.from('89504e470d0a1a0a', 'hex'));

// Input schemas as fal.ai publishes them (trimmed to the image fields).
const SCHEMAS = {
  'openai/gpt-image-2.5/flare/edit': { GptImage25FlareEditInput: { required: ['prompt', 'image_urls'], properties: { prompt: {}, image_urls: {} } } },
  'fal-ai/imagen4/preview': { Imagen4PreviewInput: { properties: { prompt: {} } } },
};

let calls;
const realFetch = globalThis.fetch;
const realSubscribe = fal.subscribe;
const env = { ...process.env };

beforeEach(() => {
  calls = { subscribe: [], schema: [] };
  process.env.FAL_KEY = 'test-key';
  process.env.GENERATED_IMAGES_DIR = dir;
  delete process.env.IMAGE_EDIT_MODEL;
  delete process.env.IMAGE_MODEL;
  delete process.env.IMAGE_EDIT_MODEL_PARAMS;
  fal.subscribe = async (endpoint, options) => {
    calls.subscribe.push({ endpoint, input: options.input });
    return { data: { images: [{ url: 'https://fal.test/out.png', content_type: 'image/png' }] } };
  };
  globalThis.fetch = async (url) => {
    const u = new URL(String(url));
    if (u.hostname === 'api.fal.ai') {
      const id = u.searchParams.get('endpoint_id');
      calls.schema.push(id);
      const schemas = SCHEMAS[id];
      const models = schemas ? [{ endpoint_id: id, openapi: { components: { schemas } } }] : [];
      return new Response(JSON.stringify({ models }), { status: 200 });
    }
    return new Response(Buffer.from('png'), { status: 200 });
  };
});

afterEach(() => {
  fal.subscribe = realSubscribe;
  globalThis.fetch = realFetch;
  process.env = { ...env };
});

test('Nano Banana edits send the image in image_urls', async () => {
  for (const model of ['fal-ai/nano-banana-2', 'fal-ai/nano-banana-pro']) {
    const result = await editImage({ prompt: 'repaint', image_path: source, model });
    assert.equal(result.success, true, result.error);
  }
  assert.deepEqual(calls.subscribe.map(c => c.endpoint), ['fal-ai/nano-banana-2/edit', 'fal-ai/nano-banana-pro/edit']);
  for (const { input } of calls.subscribe) {
    assert.ok(Array.isArray(input.image_urls) && input.image_urls[0].startsWith('data:image/png;base64,'));
    assert.equal('image_url' in input, false);
  }
  assert.deepEqual(calls.schema, []);
});

test('Kontext keeps a single image_url', () => {
  assert.deepEqual(knownEditRoute('fal-ai/flux-pro/kontext'), { endpoint: 'fal-ai/flux-pro/kontext', field: 'image_url' });
  assert.deepEqual(buildEditInput('image_url', 'p', 'data:x'), { prompt: 'p', image_url: 'data:x' });
});

test('the configured catalog edit model is used with no model passed', async () => {
  process.env.IMAGE_EDIT_MODEL = 'openai/gpt-image-2.5/flare/edit';
  const result = await editImage({ prompt: 'repaint', image_path: source });
  assert.equal(result.success, true, result.error);
  assert.equal(result.model_used, 'openai/gpt-image-2.5/flare/edit');
  assert.equal(calls.subscribe.length, 1);
  assert.equal(calls.subscribe[0].endpoint, 'openai/gpt-image-2.5/flare/edit');
  assert.ok(Array.isArray(calls.subscribe[0].input.image_urls));
});

test('a model fal.ai does not offer is refused, naming the configured one', async () => {
  process.env.IMAGE_EDIT_MODEL = 'openai/gpt-image-2.5/flare/edit';
  const result = await editImage({ prompt: 'repaint', image_path: source, model: 'gpt-image-1' });
  assert.equal(result.success, false);
  assert.match(result.error, /'gpt-image-1' is not a fal\.ai model/);
  assert.match(result.error, /openai\/gpt-image-2\.5\/flare\/edit/);
  assert.equal(calls.subscribe.length, 0);
});

test('a catalog model without a source-image field is refused', async () => {
  const result = await editImage({ prompt: 'repaint', image_path: source, model: 'fal-ai/imagen4/preview' });
  assert.equal(result.success, false);
  assert.match(result.error, /does not take a source image/);
  assert.equal(calls.subscribe.length, 0);
});

test('editImageFieldFromOpenapi reads the declared field', () => {
  assert.equal(editImageFieldFromOpenapi({ components: { schemas: { XInput: { properties: { image_url: {} } } } } }), 'image_url');
  assert.equal(editImageFieldFromOpenapi({ components: { schemas: { XInput: { properties: { image_urls: {} } } } } }), 'image_urls');
  assert.equal(editImageFieldFromOpenapi({}), null);
});

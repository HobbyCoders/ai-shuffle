// generateImage and generateWithReference model routing (#1796). No network:
// fal.subscribe and fetch are stubbed.
import { test, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fal } from '@fal-ai/client';
import { generateImage } from '../dist/image-generation/generateImage.js';
import { generateWithReference } from '../dist/image-generation/generateWithReference.js';

const dir = mkdtempSync(join(tmpdir(), 'generate-image-test-'));
const refA = join(dir, 'a.png');
const refB = join(dir, 'b.png');
writeFileSync(refA, Buffer.from('89504e470d0a1a0a', 'hex'));
writeFileSync(refB, Buffer.from('89504e470d0a1a0a', 'hex'));

// Input schemas as fal.ai publishes them (trimmed to the image fields).
const SCHEMAS = {
  'openai/gpt-image-2.5/flare/edit': { GptImage25FlareEditInput: { required: ['prompt', 'image_urls'], properties: { prompt: {}, image_urls: {} } } },
  'fal-ai/imagen4/preview': { Imagen4PreviewInput: { required: ['prompt'], properties: { prompt: {} } } },
  'fal-ai/single-ref/edit': { SingleRefEditInput: { required: ['prompt', 'image_url'], properties: { prompt: {}, image_url: {} } } },
};

let calls;
const realFetch = globalThis.fetch;
const realSubscribe = fal.subscribe;
const env = { ...process.env };

beforeEach(() => {
  calls = { subscribe: [], schema: [] };
  process.env.FAL_KEY = 'test-key';
  process.env.GENERATED_IMAGES_DIR = dir;
  for (const key of ['IMAGE_EDIT_MODEL', 'IMAGE_MODEL', 'IMAGE_MODEL_PARAMS', 'IMAGE_EDIT_MODEL_PARAMS', 'IMAGE_ASPECT_RATIO', 'IMAGE_RESOLUTION']) {
    delete process.env[key];
  }
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

test('generateImage refuses another provider\'s model, naming the configured one', async () => {
  process.env.IMAGE_MODEL = 'fal-ai/imagen4/preview';
  const result = await generateImage({ prompt: 'a cat', model: 'gpt-image-1' });
  assert.equal(result.success, false);
  assert.match(result.error, /'gpt-image-1' is not a fal\.ai model/);
  assert.match(result.error, /configured image model is 'fal-ai\/imagen4\/preview'/);
  assert.equal(calls.subscribe.length, 0);
});

test('generateImage refuses an edit endpoint that needs a source image', async () => {
  const result = await generateImage({ prompt: 'a cat', model: 'openai/gpt-image-2.5/flare/edit' });
  assert.equal(result.success, false);
  assert.match(result.error, /needs a source image; use editImage/);
  assert.equal(calls.subscribe.length, 0);
});

test('generateImage runs a catalog text-to-image model on its own endpoint', async () => {
  process.env.IMAGE_MODEL = 'fal-ai/imagen4/preview';
  const result = await generateImage({ prompt: 'a cat' });
  assert.equal(result.success, true, result.error);
  assert.deepEqual(calls.subscribe.map(c => c.endpoint), ['fal-ai/imagen4/preview']);
});

test('generateImage skips the catalog for a built-in model and names the configured edit model', async () => {
  process.env.IMAGE_EDIT_MODEL = 'openai/gpt-image-2.5/flare/edit';
  const result = await generateImage({ prompt: 'a cat', model: 'fal-ai/flux/dev' });
  assert.equal(result.success, true, result.error);
  assert.deepEqual(calls.schema, []);
  assert.equal(result.edit_with, 'editImage (configured edit model openai/gpt-image-2.5/flare/edit)');
  assert.doesNotMatch(result.edit_with, /nano-banana/);
});

test('generateWithReference sends a catalog model the references in its declared field', async () => {
  process.env.IMAGE_EDIT_MODEL = 'openai/gpt-image-2.5/flare/edit';
  const result = await generateWithReference({ prompt: 'same robot, jungle', reference_images: [refA, refB] });
  assert.equal(result.success, true, result.error);
  assert.equal(result.model_used, 'openai/gpt-image-2.5/flare/edit');
  assert.equal(calls.subscribe.length, 1);
  assert.equal(calls.subscribe[0].endpoint, 'openai/gpt-image-2.5/flare/edit');
  assert.equal(calls.subscribe[0].input.image_urls.length, 2);
  assert.equal('image_url' in calls.subscribe[0].input, false);
});

test('generateWithReference never swaps an unknown model for Kontext', async () => {
  const result = await generateWithReference({ prompt: 'p', reference_images: [refA], model: 'gpt-image-1' });
  assert.equal(result.success, false);
  assert.match(result.error, /'gpt-image-1' is not a fal\.ai model/);
  assert.equal(calls.subscribe.length, 0);
});

test('generateWithReference refuses more references than a single-image endpoint takes', async () => {
  const one = await generateWithReference({ prompt: 'p', reference_images: [refA], model: 'fal-ai/single-ref/edit' });
  assert.equal(one.success, true, one.error);
  assert.equal(typeof calls.subscribe[0].input.image_url, 'string');
  const two = await generateWithReference({ prompt: 'p', reference_images: [refA, refB], model: 'fal-ai/single-ref/edit' });
  assert.equal(two.success, false);
  assert.match(two.error, /takes one reference image; 2 were given/);
  assert.equal(calls.subscribe.length, 1);
});

test('generateWithReference keeps Nano Banana on image_urls without a catalog lookup', async () => {
  const result = await generateWithReference({ prompt: 'p', reference_images: [refA], model: 'fal-ai/nano-banana-2' });
  assert.equal(result.success, true, result.error);
  assert.equal(calls.subscribe[0].endpoint, 'fal-ai/nano-banana-2/edit');
  assert.ok(Array.isArray(calls.subscribe[0].input.image_urls));
  assert.deepEqual(calls.schema, []);
});

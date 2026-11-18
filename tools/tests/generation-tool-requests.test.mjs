// The request each media tool really sends to fal for a picked endpoint,
// pinned against tests/fixtures/generation-tool-requests.json. The Python
// compatibility check (app/core/generation_operations.tool_request) is pinned
// to the same file, so the two cannot drift. No network: fal.subscribe and
// fetch are stubbed.
import { test, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fal } from '@fal-ai/client';
import { editImage } from '../dist/image-generation/editImage.js';
import { generateWithReference } from '../dist/image-generation/generateWithReference.js';
import { imageToVideo } from '../dist/video-generation/imageToVideo.js';
import { extendVideo } from '../dist/video-generation/extendVideo.js';
import { audioToAudio } from '../dist/audio-generation/audioToAudio.js';
import { imageTo3D } from '../dist/model3d-generation/imageTo3D.js';

const { cases } = JSON.parse(readFileSync(new URL('./fixtures/generation-tool-requests.json', import.meta.url), 'utf8'));

const dir = mkdtempSync(join(tmpdir(), 'tool-requests-test-'));
const image = join(dir, 'in.png');
const video = join(dir, 'in.mp4');
const audio = join(dir, 'in.wav');
writeFileSync(image, Buffer.from('89504e470d0a1a0a', 'hex'));
writeFileSync(video, Buffer.from('000000186674797069736f6d', 'hex'));
writeFileSync(audio, Buffer.from('52494646', 'hex'));

const RUN = {
  editImage: (model) => editImage({ image_path: image, prompt: 'make it blue', model }),
  generateWithReference: (model) => generateWithReference({ prompt: 'the same cat', reference_images: [image], model }),
  imageToVideo: (model) => imageToVideo({ image_path: image, prompt: 'it moves', model }),
  extendVideo: (model) => extendVideo({ video_path: video, model }),
  audioToAudio: (model) => audioToAudio({ audio_path: audio, model }),
  imageTo3D: (model) => imageTo3D({ image_path: image, model }),
};

let calls;
let current;
const realFetch = globalThis.fetch;
const realSubscribe = fal.subscribe;
const env = { ...process.env };

beforeEach(() => {
  calls = [];
  process.env.FAL_KEY = 'test-key';
  process.env.GENERATED_IMAGES_DIR = dir;
  for (const name of ['IMAGE_EDIT_MODEL', 'IMAGE_MODEL', 'IMAGE_EDIT_MODEL_PARAMS', 'I2V_MODEL_PARAMS',
    'AUDIO_TO_AUDIO_MODEL_PARAMS', 'IMAGE_TO_3D_MODEL_PARAMS', 'VIDEO_ASPECT_RATIO', 'VIDEO_DURATION', 'VIDEO_RESOLUTION']) {
    delete process.env[name];
  }
  fal.subscribe = async (endpoint, options) => {
    calls.push({ endpoint, keys: Object.keys(options.input).sort() });
    throw new Error('stubbed: nothing is submitted');
  };
  // The published schema of the endpoint under test, as catalogOpenapi reads it.
  globalThis.fetch = async (url) => {
    const model = new URL(String(url)).searchParams.get('endpoint_id');
    const props = Object.fromEntries(current.declared.map((name) => [name, {}]));
    const schemas = { TestInput: { properties: props, required: current.required } };
    return new Response(JSON.stringify({ models: [{ endpoint_id: model, openapi: { components: { schemas } } }] }), { status: 200 });
  };
});

afterEach(() => {
  fal.subscribe = realSubscribe;
  globalThis.fetch = realFetch;
  process.env = { ...env };
});

for (const c of cases) {
  test(`${c.tool} sends ${c.refused ? 'nothing' : c.sends.join(', ')} for ${c.model}`, async () => {
    current = c;
    await RUN[c.tool](c.model);
    if (c.refused) {
      assert.deepEqual(calls, []);
      return;
    }
    assert.deepEqual(calls, [{ endpoint: c.endpoint, keys: [...c.sends].sort() }]);
  });
}

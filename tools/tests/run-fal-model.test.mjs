// runFalModel output typing (#2111). No network: fal.subscribe and fetch are stubbed.
import { test, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fal } from '@fal-ai/client';
import { executeFalModel, sniffMediaType, declaredMediaType } from '../dist/providers/fal-generic.js';

const MP3 = Buffer.concat([Buffer.from('ID3'), Buffer.from([4, 0, 0, 0, 0, 0, 0]), Buffer.alloc(32)]);
const MP3_FRAME = Buffer.concat([Buffer.from([0xff, 0xfb, 0x90, 0x64]), Buffer.alloc(32)]);
const WAV = Buffer.concat([Buffer.from('RIFF'), Buffer.from([36, 0, 0, 0]), Buffer.from('WAVEfmt '), Buffer.alloc(24)]);
const UNKNOWN = Buffer.alloc(32, 7);

let dir;
let response;
let body;
const realFetch = globalThis.fetch;
const realSubscribe = fal.subscribe;
const env = { ...process.env };

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'run-fal-model-test-'));
  process.env.FAL_KEY = 'test-key';
  process.env.GENERATED_AUDIO_DIR = dir;
  fal.subscribe = async () => response;
  globalThis.fetch = async () => new Response(body);
});

afterEach(() => {
  fal.subscribe = realSubscribe;
  globalThis.fetch = realFetch;
  process.env = { ...env };
});

test('an MP3 from a music model is saved as .mp3 with audio/mpeg (Lyria repro)', async () => {
  response = { data: { audio: { url: 'https://fal.test/files/output.wav' } } };
  body = MP3;
  const result = await executeFalModel({ endpoint_id: 'google/lyria-3.5', input: { prompt: 'x' } });
  assert.equal(result.success, true, result.error);
  assert.equal(result.mime_type, 'audio/mpeg');
  assert.match(result.filename, /\.mp3$/);
  assert.match(result.file_path, /\.mp3$/);
  assert.match(result.audio_url, /\.mp3$/);
  assert.deepEqual(readdirSync(dir), [result.filename], 'the .wav name is not left behind');
});

test('a real WAV stays .wav with audio/wav', async () => {
  response = { data: { audio: { url: 'https://fal.test/files/output', content_type: 'audio/wav' } } };
  body = WAV;
  const result = await executeFalModel({ endpoint_id: 'fal-ai/stable-audio', input: {} });
  assert.equal(result.mime_type, 'audio/wav');
  assert.match(result.filename, /\.wav$/);
});

test('fal content_type decides when the bytes are not recognised', async () => {
  response = { data: { audio: { url: 'https://fal.test/files/out', content_type: 'audio/ogg; codecs=opus' } } };
  body = UNKNOWN;
  const result = await executeFalModel({ endpoint_id: 'fal-ai/some-tts', input: {} });
  assert.equal(result.mime_type, 'audio/ogg');
  assert.match(result.filename, /\.ogg$/);
});

test('the old wav fallback applies only when nothing else is known', async () => {
  response = { data: { audio_url: 'https://fal.test/files/out' } };
  body = UNKNOWN;
  const result = await executeFalModel({ endpoint_id: 'fal-ai/some-tts', input: {} });
  assert.equal(result.mime_type, 'audio/wav');
  assert.match(result.filename, /\.wav$/);
});

test('sniffMediaType recognises common audio, video and image signatures', () => {
  assert.equal(sniffMediaType(MP3)?.extension, 'mp3');
  assert.equal(sniffMediaType(MP3_FRAME)?.extension, 'mp3');
  assert.equal(sniffMediaType(Buffer.from([0xff, 0xf1, 0x50, 0x80]))?.extension, 'aac');
  assert.equal(sniffMediaType(WAV)?.extension, 'wav');
  assert.equal(sniffMediaType(Buffer.from('OggS\0\0\0\0'))?.extension, 'ogg');
  assert.equal(sniffMediaType(Buffer.from('fLaC\0\0\0\0'))?.extension, 'flac');
  assert.equal(sniffMediaType(Buffer.from('\0\0\0\x20ftypM4A \0\0'))?.mimeType, 'audio/mp4');
  assert.equal(sniffMediaType(Buffer.from('\0\0\0\x20ftypisom\0\0'))?.mimeType, 'video/mp4');
  assert.equal(sniffMediaType(Buffer.from('89504e470d0a1a0a', 'hex'))?.extension, 'png');
  assert.equal(sniffMediaType(Buffer.from([0xff, 0xd8, 0xff, 0xe0]))?.extension, 'jpg');
  assert.equal(sniffMediaType(Buffer.from('RIFF\0\0\0\0WEBPVP8 '))?.extension, 'webp');
  assert.equal(sniffMediaType(Buffer.from('glTF\x02\0\0\0'))?.extension, 'glb');
  assert.equal(sniffMediaType(UNKNOWN), null);
  assert.equal(sniffMediaType(Buffer.from('ab')), null);
});

test('declaredMediaType reads content_type, then file_name, then the URL', () => {
  assert.deepEqual(declaredMediaType({ url: 'https://x/a', content_type: 'audio/mpeg' }), { mimeType: 'audio/mpeg', extension: 'mp3' });
  assert.deepEqual(declaredMediaType({ url: 'https://x/a', file_name: 'song.flac' }), { mimeType: 'audio/flac', extension: 'flac' });
  assert.deepEqual(declaredMediaType({ url: 'https://x/a.jpeg?sig=1' }), { mimeType: 'image/jpeg', extension: 'jpg' });
  assert.equal(declaredMediaType({ url: 'https://x/a' }), null);
});

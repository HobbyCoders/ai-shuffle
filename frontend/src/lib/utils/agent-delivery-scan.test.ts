import { describe, expect, it } from 'vitest';
import { spawnSync } from 'node:child_process';
import { heldAgentDeliveries, type AgentDelivery } from './agent-delivery';
import producer from './__fixtures__/agent-delivery-producer.json';

// Bounded oracle only. Never use the old regexes for the deadline inputs.
const ENVELOPE = String.raw`\[agent\x20message from=(['"])(.*?)\1 — (?:host-verified (?:parent assignment update|same-owner session delegation); )?a message from another (?:agent(?: in this session)?|top-level session), delivered once at this position; not part of the tool output, and not a new message when re-read from history\]\r?\n([\s\S]*?)\r?\n\[\/agent\x20message\]`;
const ANY_ENVELOPE = new RegExp(ENVELOPE, 'g');
const HELD_BLOCK = /<system-reminder>\s*Messages from other agents that arrived while this session was parked[^\n]*\n([\s\S]*?)<\/system-reminder>/g;

function legacy(text: string): AgentDelivery[] | null {
 const envelopes = (text: string): AgentDelivery[] | null => {
  const found: AgentDelivery[] = [];
  const rest = text.replace(ANY_ENVELOPE, (_all, _q, sender: string, content: string) => {
   found.push({ sender, content }); return '';
  });
  return rest.trim() ? null : found;
 };
 const deliveries: AgentDelivery[] = [];
 let blocks = 0;
 let invalid = false;
 const outside = text.replace(HELD_BLOCK, (_all, body: string) => {
  blocks++;
  const inner = envelopes(body);
  if (inner === null) invalid = true;
  else deliveries.push(...inner);
  return '\n';
 });
 if (!blocks || invalid) return null;
 const fresh = envelopes(outside);
 if (fresh === null) return null;
 deliveries.push(...fresh);
 return deliveries.length ? deliveries : null;
}

const open = '<system-reminder>';
const close = '</system-reminder>';
const header = 'Messages from other agents that arrived while this session was parked';
const wrap = (body: string, gap = '\n', tail = ':') => `${open}${gap}${header}${tail}\n${body}${close}`;
const peer = (sender = 'worker', body = 'done') => `[agent message from='${sender}' — a message from another agent, delivered once at this position; not part of the tool output, and not a new message when re-read from history]\n${body}\n[/agent message]`;

describe('linear held-delivery parsing', () => {
 it('matches the legacy parser on a grammar-aware corpus', () => {
  const whitespace = [' ', '\t', '\n', '\r', '\r\n', '\v', '\f', '\u00a0', '\u1680',
   ...Array.from({ length: 11 }, (_, i) => String.fromCharCode(0x2000 + i)),
   '\u2028', '\u2029', '\u202f', '\u205f', '\u3000', '\ufeff'];
  const frames = [peer(), peer('one', ''), peer('two', 'line\nline'),
   peer().replaceAll("'", '"'), peer().replaceAll('\n', '\r\n'),
   peer().replace('another agent,', 'another agent in this session,'),
   peer().replace('another agent,', 'another top-level session,'),
   peer().replace('— a message', '— host-verified parent assignment update; a message'),
   peer().replace('— a message', '— host-verified same-owner session delegation; a message'),
   peer('a\'b'), peer('a"b'), peer('nested [agent message from=')];
  const pieces = ['', 'text', ...whitespace, open, close, header, open + header,
   open + '\u0085' + header, open + '\r' + header, header + '\n',
   '</system•reminder>', '<SYSTEM-reminder>', '[agent message from=',
   '[/agent message]', ...frames, ...Object.values(producer.prompts)];
  const corpus: string[] = [...Object.values(producer.prompts)];
  for (const frame of frames) for (const gap of whitespace) {
   const note = wrap(frame, gap);
   corpus.push(note, gap + note + gap, note + gap + peer('fresh'),
    wrap(gap) + note, note + wrap(peer('second')), 'quoted ' + note,
    wrap(frame + 'invalid'), wrap(open + frame), wrap(frame).slice(0, -close.length),
    wrap(frame, gap, 'same-line ' + open + header));
  }
  let seed = 911;
  const random = (n: number) => { seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0; return seed % n; };
  for (let i = 0; i < 30_000; i++) {
   const frame = frames[random(frames.length)];
   const note = wrap(frame, whitespace[random(whitespace.length)]);
   const choices = [note, wrap(note), ...pieces];
   let text = '';
   for (let j = random(10); j >= 0; j--) text += choices[random(choices.length)];
   corpus.push(text);
  }
  for (const text of corpus) expect(heldAgentDeliveries(text), JSON.stringify(text)).toEqual(legacy(text));
 });

 it('cannot stall the process on incomplete held blocks or envelopes', () => {
  const script = `
import assert from 'node:assert/strict';
import { heldAgentDeliveries } from './src/lib/utils/agent-delivery.ts';
const open = ${JSON.stringify(open + '\n' + header + ':\n')};
const close = ${JSON.stringify(close)};
for (const text of [open.repeat(80_000),
 '<system-reminder>\\n'.repeat(100_000),
 '<system-reminder>' + ' '.repeat(1_000_000),
 open + "[agent message from='".repeat(300_000) + close]) {
 assert.equal(heldAgentDeliveries(text), null);
}
`;
  const child = spawnSync(process.execPath, ['--experimental-strip-types', '--input-type=module', '-e', script], {
   cwd: process.cwd(), env: Object.fromEntries(['SystemRoot', 'SYSTEMROOT', 'WINDIR', 'TMP', 'TEMP']
    .filter(key => process.env[key] !== undefined).map(key => [key, process.env[key]!])),
   encoding: 'utf8', timeout: 5_000, killSignal: 'SIGKILL',
  });
  expect(child.error).toBeUndefined();
  expect(child.status, child.stderr).toBe(0);
 }, 10_000);
});

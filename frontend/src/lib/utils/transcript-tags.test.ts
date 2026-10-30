import { describe, expect, it } from 'vitest';
import { firstTaggedText, splitTaggedBlocks } from './transcript-tags';

const tags = ['summary', 'result', 'task-notification', 'status'];

describe('literal transcript tags', () => {
 it('matches the lazy regex first-pair and global replacement semantics', () => {
  const pieces = ['', 'literal', '\n', '\r\n', ' ', '<', '>',
   ...tags.flatMap(tag => [`<${tag}>`, `</${tag}>`])];
  let seed = 911;
  const random = (n: number) => { seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0; return seed % n; };
  for (let i = 0; i < 30_000; i++) {
   const tag = tags[random(tags.length)];
   const note = `<${tag}>${pieces[random(pieces.length)]}</${tag}>`;
   const choices = [note, ...pieces];
   let text = '';
   for (let j = random(10); j >= 0; j--) text += choices[random(choices.length)];
   const oracle = new RegExp(`<${tag}>([\\s\\S]*?)</${tag}>`, 'g');
   const matches = [...text.matchAll(oracle)];
   expect(firstTaggedText(text, tag)).toBe(matches[0]?.[1]);
   expect(splitTaggedBlocks(text, tag)).toEqual({
    blocks: matches.map(match => match[0]), outside: text.replace(oracle, ''),
   });
  }
 });
});

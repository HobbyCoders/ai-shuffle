import { describe, expect, it } from 'vitest';
import { createTranscriptWindow } from './transcript-window';

describe('nested transcript mount budget', () => {
 it('atomically resumes with the incoming item vector, including an empty hydration reset', () => {
  let costs = Array(20).fill(1000);
  const window = createTranscriptWindow({ budget: 12000, cost: (i: number) => {
   if (i >= costs.length) throw new Error('charged a leaving-session row');
   return costs[i];
  } });
  window.setItemCount(costs.length);
  window.pauseWhenHidden(true);
  costs = [];
  expect(window.setItemCount(0, { hidden: false })).toEqual({ start: 0, end: 0 });
  costs = Array(40).fill(1000);
  expect(window.setItemCount(costs.length, { hidden: false })).toEqual({ start: 28, end: 40 });
 });
 it('bounds rich content as well as the 120 outer rows across twenty bidirectional shifts', () => {
  const costs = Array.from({ length: 1200 }, (_, i) => i % 3 === 0 ? 2400 : 48);
  const budget = 12000;
  const window = createTranscriptWindow({ cost: (index: number) => costs[index], budget });
  const check = () => {
   const { start, end } = window.visibleRange;
   expect(end - start).toBeLessThanOrEqual(120);
   expect(costs.slice(start, end).reduce((a, b) => a + b, 0)).toBeLessThanOrEqual(budget);
  };
  const original = window.setItemCount(costs.length);
  check();
  for (let i = 0; i < 20; i++) {
   const before = window.visibleRange;
   const next = i % 2 === 0 ? window.extendOlder() : window.extendNewer();
   expect(next).not.toEqual(before);
   expect(Math.min(before.end, next.end)).toBeGreaterThan(Math.max(before.start, next.start));
   check();
  }
  expect(window.toTail()).toEqual(original);
 });
 it('can navigate past a single oversize row without hiding or truncating its content', () => {
  const window = createTranscriptWindow({ budget: 100, cost: (i: number) => i === 50 ? 1000 : 10 });
  window.setItemCount(100);
  const seen = new Set<number>();
  for (let i = 0; i < 120; i++) {
   const range = window.extendOlder();
   for (let n = range.start; n < range.end; n++) seen.add(n);
  }
  expect(seen.has(50)).toBe(true);
  expect(window.visibleRange.start).toBe(0);
  expect(window.windowAround(50)).toEqual({ start: 50, end: 51 });
 });
});

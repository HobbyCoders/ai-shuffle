import { describe, expect, it } from 'vitest';
import { createTranscriptWindow, type TranscriptRange } from './transcript-window';

describe('nested transcript mount budget', () => {
 it('keeps the trailing mounted reply through same-count streaming growth when no viewport is available', () => {
  let costs = [6000, 2000, 2000];
  const window = createTranscriptWindow({ cost: i => costs[i], budget: 12000 });
  window.onStreamingChange(true);
  expect(window.setItemCount(3)).toEqual({ start: 0, end: 3 });
  window.setItemCount(3, { followTail: false });
  costs = [6000, 2000, 6000];
  expect(window.setItemCount(3, { followTail: false })).toEqual({ start: 1, end: 3 });
  expect(window.selectionLimited).toBe(false);
 });
 it.each([0, 1, 2])('keeps visible reading row %i, not an arbitrary range edge, when content is repriced', reading => {
  let costs = [6000, 2000, 2000];
  const window = createTranscriptWindow({ cost: i => costs[i], budget: 12000, getReadingIndex: () => reading });
  window.setItemCount(3);
  window.setItemCount(3, { followTail: false });
  costs = [6000, 2000, 6000];
  const range = window.setItemCount(3, { followTail: false });
  expect(range.start).toBeLessThanOrEqual(reading);
  expect(range.end).toBeGreaterThan(reading);
  expect(costs.slice(range.start, range.end).reduce((sum, cost) => sum + cost, 0)).toBeLessThanOrEqual(12000);
  expect(window.selectionLimited).toBe(false);
 });
 it('resolves a reading key against the new vector after a prepend and allows explicit navigation away', () => {
  let costs = [6000, 2000, 2000], reading = 2;
  const window = createTranscriptWindow({ cost: i => costs[i], budget: 12000, getReadingIndex: () => reading });
  window.setItemCount(3);
  window.setItemCount(3, { followTail: false });
  costs = [2000, 6000, 2000, 6000]; reading = 3;
  expect(window.setItemCount(4, { followTail: false })).toEqual({ start: 2, end: 4 });
  expect(window.windowAround(0)).toEqual({ start: 0, end: 3 });
 });
 it('reads viewport geometry only when repricing would evict an already-mounted row', () => {
  let costs = [6000, 2000, 2000], reads = 0;
  const window = createTranscriptWindow({ cost: i => costs[i], budget: 12000, getReadingIndex: () => { reads++; return 2; } });
  window.setItemCount(3);
  for (let i = 0; i < 20; i++) window.setItemCount(3, { followTail: false });
  expect(reads).toBe(0);
  costs = [6000, 2000, 6000];
  window.setItemCount(3, { followTail: false });
  expect(reads).toBe(1);
  for (let i = 0; i < 20; i++) window.setItemCount(3, { followTail: false });
  expect(reads).toBe(1);
 });
 it('keeps an oversize reading reply intact on its own instead of truncating or evicting it', () => {
  let costs = [6000, 2000, 2000];
  const window = createTranscriptWindow({ cost: i => costs[i], budget: 12000, getReadingIndex: () => 2 });
  window.setItemCount(3);
  window.setItemCount(3, { followTail: false });
  costs = [6000, 2000, 13000];
  expect(window.setItemCount(3, { followTail: false })).toEqual({ start: 2, end: 3 });
  expect(window.selectionLimited).toBe(false);
 });
 it('protects the reading key when a hidden view resumes after content growth', () => {
  let costs = [6000, 2000, 2000];
  const window = createTranscriptWindow({ cost: i => costs[i], budget: 12000, getReadingIndex: () => 2 });
  window.setItemCount(3);
  window.setItemCount(3, { followTail: false, hidden: true });
  costs = [6000, 2000, 6000];
  expect(window.setItemCount(3, { followTail: false, hidden: true })).toEqual({ start: 0, end: 3 });
  expect(window.pauseWhenHidden(false)).toEqual({ start: 1, end: 3 });
 });
 it.each([
  { costs: [64, 6000, 64, 9000], previous: { start: 0, end: 3 }, expected: { start: 2, end: 4 }, direction: 'newer' },
  { costs: [9000, 64, 6000, 64], previous: { start: 1, end: 4 }, expected: { start: 0, end: 2 }, direction: 'older' }
 ] as const)('retains the largest affordable overlap when $direction is blocked by a heterogeneous body', ({ costs, previous, expected, direction }) => {
  const window = createTranscriptWindow({ cost: i => costs[i], budget: 12000 });
  window.setItemCount(costs.length);
  expect(window.restoreRange(previous)).toEqual(previous);
  const next = direction === 'newer' ? window.extendNewer() : window.extendOlder();
  expect(next).toEqual(expected);
  expect(Math.min(previous.end, next.end)).toBeGreaterThan(Math.max(previous.start, next.start));
  expect(costs.slice(next.start, next.end).reduce<number>((sum, cost) => sum + cost, 0)).toBeLessThanOrEqual(12000);
 });
 it('makes progress at an unavoidable disjoint boundary without exceeding the budget', () => {
  const costs = [64, 9000, 9000, 64];
  const window = createTranscriptWindow({ cost: i => costs[i], budget: 12000 });
  window.setItemCount(costs.length);
  expect(window.restoreRange({ start: 0, end: 2 })).toEqual({ start: 0, end: 2 });
  expect(window.extendNewer()).toEqual({ start: 2, end: 4 });
  expect(window.extendOlder()).toEqual({ start: 0, end: 2 });
 });
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

 it('pins the selected span in either direction without accumulating mounted history', () => {
  let pinned: TranscriptRange | null = { start: 94, end: 96 };
  const window = createTranscriptWindow({ cap: 8, cost: () => 100, budget: 800, getPinnedRange: () => pinned });
  window.setItemCount(100);
  for (let i = 0; i < 20; i++) {
   const range = i < 10 ? window.loadOlder() : window.extendNewer();
   expect(range.start).toBeLessThanOrEqual(94);
   expect(range.end).toBeGreaterThanOrEqual(96);
   expect(range.end - range.start).toBeLessThanOrEqual(8);
   expect((range.end - range.start) * 100).toBeLessThanOrEqual(800);
  }
  window.windowAround(0);
  expect(window.selectionLimited).toBe(true);
  pinned = null;
  expect(window.windowAround(0)).toEqual({ start: 0, end: 8 });
  expect(window.selectionLimited).toBe(false);
 });
 it('also pins unweighted nested windows and releases on the next navigation after collapse', () => {
  let pinned: TranscriptRange | null = null;
  const window = createTranscriptWindow({ cap: 60, step: 20, getPinnedRange: () => pinned });
  const original = window.setItemCount(2000);
  pinned = { start: 1998, end: 2000 };
  expect(window.loadOlder()).toEqual(original);
  expect(window.selectionLimited).toBe(true);
  pinned = null;
  expect(window.loadOlder()).toEqual({ start: 1920, end: 1980 });
  expect(window.selectionLimited).toBe(false);
 });
 it('admits no unselected rows if already-selected content grows beyond the content budget', () => {
  let cost = 20, pinned: TranscriptRange | null = null;
  const window = createTranscriptWindow({ cap: 8, cost: () => cost, budget: 100, getPinnedRange: () => pinned });
  expect(window.setItemCount(20)).toEqual({ start: 15, end: 20 });
  pinned = { start: 17, end: 20 };
  cost = 50;
  expect(window.loadOlder()).toEqual(pinned);
  expect(window.selectionLimited).toBe(true);
  pinned = null;
  expect(window.toTail()).toEqual({ start: 18, end: 20 });
 });
});

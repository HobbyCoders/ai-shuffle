import { describe, expect, it } from 'vitest';
import { copyReasoningMetadata, mergeReasoningMetadata, type ReasoningCapabilities } from './model-providers';

describe('normalized reasoning evidence', () => {
 it.each([{ effort_levels: null }, { effort_levels: [] }, { effort_levels: ['none', 'minimal', 'high'] }])('round-trips levels $effort_levels without losing null/default or mandatory', ({ effort_levels }) => {
  const canonical: ReasoningCapabilities = { version: 1, effort_levels, default_effort: effort_levels?.includes('none') ? 'none' : null, mandatory: false };
  const source = { id: 'model', label: 'Model', reasoning_capabilities: canonical };
  expect(JSON.parse(JSON.stringify(copyReasoningMetadata(source)))).toEqual({ reasoning_capabilities: canonical });
 });
 it('keeps canonical metadata when a refreshed catalog only has stale raw fields', () => {
  const reasoning_capabilities: ReasoningCapabilities = { version: 1, effort_levels: ['low', 'high', 'max'], default_effort: 'max' };
  expect(mergeReasoningMetadata({ reasoning_capabilities }, { supported_reasoning_levels: [] }).reasoning_capabilities).toEqual(reasoning_capabilities);
 });
 it.each([{ effort_levels: ['low', 'high', 'max'] }, { effort_levels: [] }])('preserves known levels $effort_levels when same-version evidence is unknown', ({ effort_levels }) => {
  const reasoning_capabilities: ReasoningCapabilities = { version: 1, effort_levels, default_effort: effort_levels.includes('max') ? 'max' : null, mandatory: true };
  const fresh = { reasoning_capabilities: { version: 1 as const, effort_levels: null, default_effort: null } };
  expect(mergeReasoningMetadata({ reasoning_capabilities }, fresh).reasoning_capabilities).toEqual(reasoning_capabilities);
 });
 it('accepts fresh unsupported evidence and exact arrays that remove tiers', () => {
  const stored = { reasoning_capabilities: { version: 1 as const, effort_levels: ['low', 'high', 'max'], default_effort: 'max' } };
  for (const effort_levels of [[], ['low', 'high']]) {
   const fresh = { reasoning_capabilities: { version: 1 as const, effort_levels, default_effort: effort_levels.includes('high') ? 'high' : null } };
   expect(mergeReasoningMetadata(stored, fresh).reasoning_capabilities).toEqual(fresh.reasoning_capabilities);
  }
 });
});

import { describe, expect, it } from 'vitest';
import { creditText } from './provider-credit';

describe('remaining provider credits', () => {
 it('prefers account credit to key spending and limits', () => {
  expect(creditText({ balance_remaining: 19.46, usage: 30.54, limit_remaining: 5 }, 'openrouter_account')?.short).toBe('$19.46 left');
 });
 it('labels a restricted key limit, never as the account balance', () => {
  expect(creditText({ usage: 30, limit_remaining: 4 }, 'openrouter')?.detail).toBe('$4.00 left on this key');
  expect(creditText({ usage: 30 }, 'openrouter')).toBeNull();
  expect(creditText({ unlimited: true }, 'openrouter')).toBeNull();
 });
 it('reports positive ChatGPT extra credits without a dollar sign', () => {
  expect(creditText({ balance: 12.5 }, 'chatgpt')?.detail).toBe('12.5 extra credits available');
 });
 it.each([0, -1, NaN, Infinity, '12', true, null])('omits invalid/zero extra credits: %s', balance => {
  expect(creditText({ balance }, 'chatgpt')).toBeNull();
 });
 it('keeps an exhausted or overdrawn monetary account visible', () => {
  expect(creditText({ balance_remaining: 0 }, 'fal')?.short).toBe('$0.00 left');
  expect(creditText({ balance_remaining: -2 }, 'openrouter_account')?.short).toBe('$-2.00 left');
 });
 it('never headlines organization costs as balance', () => {
  expect(creditText({ spend_month: 12 }, 'openai')).toEqual({ short: 'Balance unavailable', detail: '$12.00 organization spend this month (UTC); balance unavailable' });
 });
});

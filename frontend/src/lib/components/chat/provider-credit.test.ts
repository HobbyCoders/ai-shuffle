import { describe, expect, it } from 'vitest';
import { creditText, formatUsd } from './provider-credit';

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
 it('keeps an exhausted or overdrawn monetary account visible and flags it', () => {
  expect(creditText({ balance_remaining: 0 }, 'fal')).toMatchObject({ short: '$0.00 left', exhausted: true });
  expect(creditText({ balance_remaining: -2.5 }, 'openrouter_account')).toMatchObject({ short: '-$2.50 left', exhausted: true });
  expect(creditText({ balance_remaining: -2.5 }, 'kimi')).toMatchObject({ detail: '-$2.50 left', exhausted: true });
  expect(creditText({ balance_remaining: 0.01 }, 'fal')?.exhausted).toBe(false);
  expect(creditText({ limit_remaining: 0 }, 'openrouter')).toMatchObject({ short: '$0.00 left on this key', exhausted: true });
 });
 it('groups thousands in large balances', () => {
  expect(creditText({ balance_remaining: 12345678.91 }, 'openrouter_account')?.short).toBe('$12,345,678.91 left');
  expect(creditText({ balance_remaining: -1234.5 }, 'fal')?.short).toBe('-$1,234.50 left');
  expect(creditText({ limit_remaining: 1500 }, 'openrouter')?.short).toBe('$1,500.00 left on this key');
 });
 it('never shows a negative zero', () => {
  expect(formatUsd(-0.001)).toBe('$0.00');
  expect(formatUsd(-0)).toBe('$0.00');
 });
 it.each([
  { balance_remaining: 42 },
  { spend_month: 12 },
  { manual_balance: 20, balance_remaining: 18, estimated_spend: 2 },
 ])('ignores retired OpenAI API credit/spend data from older workers: %s', credits => {
  expect(creditText(credits, 'openai')).toBeNull();
 });
});

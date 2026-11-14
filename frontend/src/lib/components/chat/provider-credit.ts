/** Remaining-first provider figures. Subscription credits are never dollars. */
export function creditNumber(credits: Record<string, unknown> | null, key: string): number | null {
 const value = credits?.[key];
 return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/** Dollars with the sign before the currency sign and grouped thousands: `-$1,234.50`. */
export function formatUsd(value: number): string {
 const text = Math.abs(value).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
 // An amount that rounds to zero reads as zero, never "-$0.00".
 return value < 0 && text !== '0.00' ? `-$${text}` : `$${text}`;
}

/**
 * A dollar balance or allowance as every usage display shows it: `formatUsd`
 * text, and `danger` at zero or below, when nothing is left to spend. The chat's
 * usage popover and the Settings provider pages both read balances through this.
 * Null when the worker reported no number.
 */
export function balanceDisplay(value: unknown, suffix = ''): { text: string; danger: boolean } | null {
 return typeof value === 'number' && Number.isFinite(value) ? { text: `${formatUsd(value)}${suffix}`, danger: value <= 0 } : null;
}

export interface CreditText {
 short: string;
 detail: string;
 /** A balance or key limit at or below zero: nothing left to spend. */
 exhausted?: boolean;
}

export function creditText(credits: Record<string, unknown> | null, type: string | null): CreditText | null {
 // Ignore readings from workers that still send retired OpenAI billing data.
 if (type === 'openai') return null;
 const balance = creditNumber(credits, 'balance_remaining');
 if (balance != null) {
  const left = balanceDisplay(balance, ' left')!;
  return { short: left.text, detail: left.text, exhausted: left.danger };
 }
 if (type === 'chatgpt') {
  const extra = creditNumber(credits, 'balance');
  if (extra == null || extra <= 0) return null;
  const text = `${extra.toLocaleString('en-US', { maximumFractionDigits: 2 })} extra credits`;
  return { short: text, detail: `${text} available` };
 }
 if (credits?.unlimited === true && type !== 'openrouter' && type !== 'openrouter_account') return { short: 'Credits: unlimited', detail: 'Credits: unlimited' };
 const left = creditNumber(credits, 'limit_remaining');
 if (left != null) {
  const shown = balanceDisplay(left, ' left on this key')!;
  return { short: shown.text, detail: shown.text, exhausted: shown.danger };
 }
 // Org costs and lifetime key spend are not balances. Keep optional spend
 // detail for legacy integrations, but never headline it as allowance left.
 const spend = creditNumber(credits, 'spend_month');
 if (spend != null) return { short: 'Balance unavailable', detail: `${formatUsd(spend)} organization spend this month (UTC); balance unavailable` };
 return null;
}

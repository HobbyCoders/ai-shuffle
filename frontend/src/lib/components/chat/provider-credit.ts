/** Remaining-first provider figures. Subscription credits are never dollars. */
export function creditNumber(credits: Record<string, unknown> | null, key: string): number | null {
 const value = credits?.[key];
 return typeof value === 'number' && Number.isFinite(value) ? value : null;
}
export function creditText(credits: Record<string, unknown> | null, type: string | null): { short: string; detail: string } | null {
 const balance = creditNumber(credits, 'balance_remaining');
 if (type === 'openai' && creditNumber(credits, 'manual_balance') != null && balance != null) {
  const estimate = `~$${balance.toFixed(2)} left (estimated)`;
  const spent = creditNumber(credits, 'estimated_spend') ?? 0;
  const setAt = creditNumber(credits, 'set_at');
  const spend = creditNumber(credits, 'spend_month');
  const detail = `${estimate}; ~$${spent.toFixed(2)} estimated spend since ${setAt == null ? 'balance entry' : new Date(setAt).toLocaleString()}. Spending outside this app and unpriced turns are not deducted.`;
  return {
   short: spend == null ? estimate : `$${spend.toFixed(2)} spent this month`,
   detail: spend == null ? detail : `$${spend.toFixed(2)} organization spend this month (UTC); ${detail}`,
  };
 }
 if (balance != null) return { short: `$${balance.toFixed(2)} left`, detail: `$${balance.toFixed(2)} left` };
 if (type === 'chatgpt') {
  const extra = creditNumber(credits, 'balance');
  if (extra == null || extra <= 0) return null;
  const text = `${extra.toLocaleString('en-US', { maximumFractionDigits: 2 })} extra credits`;
  return { short: text, detail: `${text} available` };
 }
 if (credits?.unlimited === true && type !== 'openrouter' && type !== 'openrouter_account') return { short: 'Credits: unlimited', detail: 'Credits: unlimited' };
 const left = creditNumber(credits, 'limit_remaining');
 if (left != null) {
  const text = `$${left.toFixed(2)} left on this key`;
  return { short: text, detail: text };
 }
 // Org costs and lifetime key spend are not balances. Keep optional spend
 // detail for legacy integrations, but never headline it as allowance left.
 const spend = creditNumber(credits, 'spend_month');
 if (spend != null) return { short: 'Balance unavailable', detail: `$${spend.toFixed(2)} organization spend this month (UTC); balance unavailable` };
 return null;
}

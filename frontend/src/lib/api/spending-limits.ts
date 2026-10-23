/**
 * The owner's daily spending limit and job time limit (Settings → Usage).
 *
 * They cover every owner chat that coordinates other work and every job it
 * starts (Task workers, StartSession chats, on any worker). Spend is in US
 * dollars at API prices, so subscription use is an estimate. The day resets
 * at midnight in the owner's time zone (else UTC). The server enforces both.
 * The route keeps its historical `/assistant/limits` path (#371 step 5).
 */
import { api } from './client';

export interface SpendingLimits {
	spend_limit_enabled: boolean;
	daily_spend_limit_usd: number;
	job_limit_enabled: boolean;
	max_job_minutes: number;
	spent_today_usd: number;
	day: string;
	timezone: string;
	resets_at: string;
	limit_reached: boolean;
	warning: boolean;
}
export type SpendingLimitsPatch = Partial<Pick<SpendingLimits, 'spend_limit_enabled' | 'daily_spend_limit_usd' | 'job_limit_enabled' | 'max_job_minutes'>> & {
	/** Required with spend_limit_enabled: false. */
	confirm_spend_limit_off?: boolean;
};
export const getSpendingLimits = () => api.get<SpendingLimits>('/assistant/limits');
export const updateSpendingLimits = (patch: SpendingLimitsPatch) => api.patch<SpendingLimits>('/assistant/limits', patch);

<script lang="ts">
	/**
	 * The panel's stand-in for Chrome's date/time popups (date, month, week,
	 * time, datetime-local). Days, weeks and months outside the page's min/max
	 * or off its step are shown disabled; time columns list only on-step values
	 * and disable those out of range. A date, week or month applies on click;
	 * time and date-and-time apply with Set. Arrows move through the calendar
	 * (PageUp/PageDown by month, with Shift by year), Enter chooses, Escape
	 * cancels.
	 */
	import { onMount, tick, untrack } from 'svelte';
	import { ChevronDown, ChevronLeft, ChevronRight } from 'lucide-svelte';
	import {
		allowed,
		anyAllowedIn,
		civilFromDays,
		daysFromCivil,
		daysInMonth,
		firstDayOfWeek,
		formatValue,
		monthGrid,
		monthIndex,
		monthOf,
		nearestAllowed,
		parseValue,
		precisionFor,
		revealIn,
		weekIndexOf,
		weekMonday,
		type DateKind,
		type Limits,
	} from '$lib/browser-panel/pickers';

	let {
		kind,
		value,
		limits,
		label,
		autofocus = true,
		locale = typeof navigator !== 'undefined' ? navigator.language : 'en-US',
		now = new Date(),
		onChoose,
		onCancel,
	}: {
		kind: DateKind;
		/** The page control's current value string. */
		value: string;
		limits: Limits;
		label: string;
		autofocus?: boolean;
		locale?: string;
		/** This device's clock (fixed in tests). */
		now?: Date;
		onChoose: (value: string) => void;
		onCancel: () => void;
	} = $props();

	const DAY = 86_400_000;
	const H = 3_600_000;
	const M = 60_000;
	const S = 1000;
	const uid = `bdp-${Math.random().toString(36).slice(2, 9)}`;

	// A picker opens for one control and is remounted for the next: its kind,
	// locale, clock and starting value are fixed when it opens.
	const init = untrack(() => ({ kind, locale, now, value, limits }));
	const k = init.kind;
	const hasCalendar = k !== 'time';
	const hasTime = k === 'time' || k === 'datetime-local';
	const firstDay = k === 'week' ? 1 : firstDayOfWeek(init.locale);

	const today = daysFromCivil(init.now.getFullYear(), init.now.getMonth() + 1, init.now.getDate());
	const nowMs = ((init.now.getHours() * 60 + init.now.getMinutes()) * 60 + init.now.getSeconds()) * 1000;
	const parsed = parseValue(k, init.value);

	// ── Formatting ──────────────────────────────────────────────────────
	const fmt = (o: Intl.DateTimeFormatOptions) => new Intl.DateTimeFormat(init.locale, { timeZone: 'UTC', ...o });
	const monthYearFmt = fmt({ month: 'long', year: 'numeric' });
	const monthShortFmt = fmt({ month: 'short' });
	const weekdayFmt = fmt({ weekday: 'narrow' });
	const weekdayLongFmt = fmt({ weekday: 'long' });
	const dayFmt = fmt({ weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' });
	const readoutDateFmt = fmt({ weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' });
	const hourFmt = fmt({ hour: 'numeric' });
	const timeFmt = fmt({ hour: 'numeric', minute: '2-digit' });
	const timeSecFmt = fmt({ hour: 'numeric', minute: '2-digit', second: '2-digit' });
	const hour12 = !!hourFmt.resolvedOptions().hour12;
	const pad = (n: number) => String(n).padStart(2, '0');
	const weekdays = Array.from({ length: 7 }, (_, i) => {
		const d = Date.UTC(2023, 0, 1 + ((firstDay + i) % 7)); // 2023-01-01 is a Sunday
		return { short: weekdayFmt.format(d), long: weekdayLongFmt.format(d) };
	});

	// ── Calendar state ──────────────────────────────────────────────────
	/** Selected day (date: the value; week: its Monday; datetime: its day). */
	let selectedDay = $state<number | null>(
		parsed === null || k === 'time' || k === 'month'
			? null
			: k === 'date'
				? parsed
				: k === 'week'
					? weekMonday(parsed)
					: Math.floor(parsed / DAY),
	);
	const selectedMonth = k === 'month' ? parsed : null;
	const dayOk = (d: number) =>
		k === 'date' ? allowed(limits, d) : k === 'week' ? allowed(limits, weekIndexOf(d)) : anyAllowedIn(limits, d * DAY, (d + 1) * DAY);
	function startDay(): number {
		const l = init.limits;
		if (parsed !== null && k !== 'time' && k !== 'month') return k === 'date' ? parsed : k === 'week' ? weekMonday(parsed) : Math.floor(parsed / DAY);
		if (k === 'date') return nearestAllowed(l, today) ?? today;
		if (k === 'week') return weekMonday(nearestAllowed(l, weekIndexOf(today)) ?? weekIndexOf(today));
		if (k === 'datetime-local') {
			const near = nearestAllowed(l, today * DAY + nowMs);
			return near === null ? today : Math.floor(near / DAY);
		}
		return today;
	}
	const firstCursor = startDay();
	let cursor = $state(firstCursor);
	const cursorCivil = $derived(civilFromDays(cursor));
	let mode = $state<'days' | 'months'>(k === 'month' ? 'months' : 'days');
	const todayMonth = monthIndex(civilFromDays(today).year, civilFromDays(today).month);
	let monthCursor = $state(
		k === 'month'
			? (selectedMonth ?? nearestAllowed(init.limits, todayMonth) ?? todayMonth)
			: monthIndex(civilFromDays(firstCursor).year, civilFromDays(firstCursor).month),
	);
	const monthYear = $derived(monthOf(monthCursor).year);

	const grid = $derived(monthGrid(cursorCivil.year, cursorCivil.month, firstDay));
	const weeks = $derived(Array.from({ length: 6 }, (_, w) => grid.slice(w * 7, w * 7 + 7)));

	function monthOk(m: number): boolean {
		if (k === 'month') return allowed(limits, m);
		const { year, month } = monthOf(m);
		const first = daysFromCivil(year, month, 1);
		const last = first + daysInMonth(year, month) - 1;
		if (k === 'date') return anyAllowedIn(limits, first, last + 1);
		if (k === 'week') return anyAllowedIn(limits, weekIndexOf(first), weekIndexOf(last) + 1);
		return anyAllowedIn(limits, first * DAY, (last + 1) * DAY);
	}

	/** Same day-of-month in another month, clamped to its length. */
	function shiftMonths(day: number, months: number): number {
		const c = civilFromDays(day);
		const target = monthOf(monthIndex(c.year, c.month) + months);
		return daysFromCivil(target.year, target.month, Math.min(c.day, daysInMonth(target.year, target.month)));
	}

	function focusCursorNow() {
		const sel = mode === 'days' ? `[data-day="${cursor}"]` : `[data-month="${monthCursor}"]`;
		(document.querySelector(`#${uid} ${sel}`) as HTMLElement | null)?.focus({ preventScroll: true });
	}
	async function focusCursor() {
		await tick();
		focusCursorNow();
	}

	function chooseDay(d: number) {
		if (!dayOk(d)) return;
		if (k === 'date') onChoose(formatValue('date', d));
		else if (k === 'week') onChoose(formatValue('week', weekIndexOf(d)));
		else {
			selectedDay = d;
			cursor = d;
			snapTime();
		}
	}

	function chooseMonth(m: number) {
		if (!monthOk(m)) return;
		if (k === 'month') {
			onChoose(formatValue('month', m));
			return;
		}
		// Jump the calendar to that month, onto its first usable day.
		const { year, month } = monthOf(m);
		const first = daysFromCivil(year, month, 1);
		const days = Array.from({ length: daysInMonth(year, month) }, (_, i) => first + i);
		const c = civilFromDays(cursor);
		cursor = days.find((d) => civilFromDays(d).day === c.day && dayOk(d)) ?? days.find(dayOk) ?? first;
		mode = 'days';
		void focusCursor();
	}

	function dayKey(e: KeyboardEvent) {
		const moves: Record<string, () => number> = {
			ArrowLeft: () => cursor - 1,
			ArrowRight: () => cursor + 1,
			ArrowUp: () => cursor - 7,
			ArrowDown: () => cursor + 7,
			PageUp: () => shiftMonths(cursor, e.shiftKey ? -12 : -1),
			PageDown: () => shiftMonths(cursor, e.shiftKey ? 12 : 1),
			Home: () => cursor - ((civilFromDays(cursor).weekday - firstDay + 7) % 7),
			End: () => cursor + 6 - ((civilFromDays(cursor).weekday - firstDay + 7) % 7),
		};
		if (moves[e.key]) {
			e.preventDefault();
			cursor = moves[e.key]();
			void focusCursor();
		} else if (e.key === 'Enter' || e.key === ' ') {
			e.preventDefault();
			chooseDay(cursor);
		}
	}

	function monthKey(e: KeyboardEvent) {
		const delta: Record<string, number> = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -3, ArrowDown: 3, PageUp: -12, PageDown: 12 };
		if (delta[e.key] !== undefined) {
			e.preventDefault();
			monthCursor += delta[e.key];
			void focusCursor();
		} else if (e.key === 'Enter' || e.key === ' ') {
			e.preventDefault();
			chooseMonth(monthCursor);
		}
	}

	// ── Time state ──────────────────────────────────────────────────────
	const dayStart = $derived(k === 'datetime-local' ? (selectedDay ?? cursor) * DAY : 0);
	const precision = $derived(precisionFor(limits));
	// Seconds show when the step, the step base (min) or the value needs them.
	const showSeconds = $derived(hasTime && (precision !== 'minute' || limits.base % M !== 0 || (parsed !== null && parsed % M !== 0)));
	/** Off-step values never show; out-of-range ones show disabled. */
	const stepOnly = $derived<Limits>({ ...limits, min: null, max: null, wraps: false });
	function startTime(): number {
		if (parsed !== null && hasTime) return k === 'time' ? parsed : parsed - Math.floor(parsed / DAY) * DAY;
		const base = k === 'datetime-local' ? firstCursor * DAY : 0;
		const rounded = Math.floor(nowMs / M) * M;
		const near = nearestAllowed(init.limits, base + rounded);
		return near === null ? rounded : ((near - base) % DAY + DAY) % DAY;
	}
	let time = $state(startTime());
	const hour = $derived(Math.floor(time / H));
	const minute = $derived(Math.floor(time / M) % 60);
	const second = $derived(Math.floor(time / S) % 60);

	type Cell = { v: number; label: string; ok: boolean };
	const hours = $derived.by<Cell[]>(() =>
		Array.from({ length: 24 }, (_, h) => h)
			.filter((h) => anyAllowedIn(stepOnly, dayStart + h * H, dayStart + (h + 1) * H))
			.map((h) => ({
				v: h,
				label: hour12 ? hourFmt.format(Date.UTC(2020, 0, 1, h)) : pad(h),
				ok: anyAllowedIn(limits, dayStart + h * H, dayStart + (h + 1) * H),
			})),
	);
	const minutes = $derived.by<Cell[]>(() => {
		const at = dayStart + hour * H;
		return Array.from({ length: 60 }, (_, m) => m)
			.filter((m) => anyAllowedIn(stepOnly, at + m * M, at + (m + 1) * M))
			.map((m) => ({ v: m, label: pad(m), ok: anyAllowedIn(limits, at + m * M, at + (m + 1) * M) }));
	});
	const seconds = $derived.by<Cell[]>(() => {
		if (!showSeconds) return [];
		const at = dayStart + hour * H + minute * M;
		return Array.from({ length: 60 }, (_, s) => s)
			.filter((s) => anyAllowedIn(stepOnly, at + s * S, at + (s + 1) * S))
			.map((s) => ({ v: s, label: pad(s), ok: anyAllowedIn(limits, at + s * S, at + (s + 1) * S) }));
	});

	const composed = $derived(dayStart + time);
	const canSet = $derived(hasTime && (k === 'time' || selectedDay !== null) && allowed(limits, k === 'time' ? time : composed));

	/** After a column change, move later columns onto values allowed with it. */
	function snapTime() {
		if (!hasTime) return;
		const v = k === 'time' ? time : dayStart + time;
		if (allowed(limits, v)) return;
		const near = nearestAllowed(limits, k === 'time' ? hour * H + minute * M : dayStart + hour * H + minute * M);
		if (near === null) return;
		const local = k === 'time' ? near : near - dayStart;
		if (local >= 0 && local < DAY) time = local;
	}

	/** The enabled cell closest to `v`. */
	function closest(cells: Cell[], v: number): number | undefined {
		let best: Cell | undefined;
		for (const c of cells) if (c.ok && (!best || Math.abs(c.v - v) < Math.abs(best.v - v))) best = c;
		return best?.v;
	}

	/** Set one column; keep it, and move only finer columns onto allowed values. */
	function pick(unit: 'h' | 'm' | 's', v: number) {
		const h = unit === 'h' ? v : hour;
		let m = unit === 'm' ? v : minute;
		let sec = unit === 's' ? v : showSeconds ? second : 0;
		time = h * H + m * M + sec * S;
		if (unit === 'h' && !minutes.find((c) => c.v === m)?.ok) m = closest(minutes, m) ?? m;
		time = h * H + m * M + sec * S;
		if (unit !== 's' && showSeconds && !seconds.find((c) => c.v === sec)?.ok) sec = closest(seconds, sec) ?? sec;
		time = h * H + m * M + sec * S;
		snapTime();
	}

	function setTime() {
		if (!hasTime) return;
		const v = k === 'time' ? time : composed;
		const near = nearestAllowed(limits, v);
		if (near === null || (k === 'datetime-local' && selectedDay === null)) return;
		onChoose(formatValue(k, near, showSeconds ? (precision === 'ms' ? 'ms' : 'second') : 'minute'));
	}

	function revealCell(unit: string, v: number, center = false) {
		const col = document.querySelector(`#${uid} [data-col="${unit}"]`) as HTMLElement | null;
		revealIn(col, col?.querySelector(`[data-v="${v}"]`), center);
	}

	function columnKey(e: KeyboardEvent, unit: 'h' | 'm' | 's', cells: Cell[], current: number) {
		const enabled = cells.filter((c) => c.ok);
		const at = enabled.findIndex((c) => c.v === current);
		const go = (i: number) => {
			const c = enabled[Math.max(0, Math.min(enabled.length - 1, i))];
			if (c) {
				pick(unit, c.v);
				void tick().then(() => revealCell(unit, c.v));
			}
		};
		const order = ['h', 'm', ...(showSeconds ? ['s'] : [])];
		if (e.key === 'ArrowDown') go(at < 0 ? 0 : at + 1);
		else if (e.key === 'ArrowUp') go(at < 0 ? enabled.length - 1 : at - 1);
		else if (e.key === 'Home') go(0);
		else if (e.key === 'End') go(enabled.length - 1);
		else if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
			const next = order[order.indexOf(unit) + (e.key === 'ArrowLeft' ? -1 : 1)];
			if (next) (document.querySelector(`#${uid} [data-col="${next}"]`) as HTMLElement | null)?.focus({ preventScroll: true });
		} else if (e.key === 'Enter') setTime();
		else return;
		e.preventDefault();
	}

	// ── Footer shortcuts ────────────────────────────────────────────────
	const todayLabel = k === 'month' ? 'This month' : k === 'week' ? 'This week' : k === 'time' ? 'Now' : k === 'datetime-local' ? 'Now' : 'Today';
	const todayValue = $derived.by<number | null>(() => {
		const c = civilFromDays(today);
		if (k === 'date') return allowed(limits, today) ? today : null;
		if (k === 'week') return allowed(limits, weekIndexOf(today)) ? weekIndexOf(today) : null;
		if (k === 'month') return allowed(limits, monthIndex(c.year, c.month)) ? monthIndex(c.year, c.month) : null;
		const v = k === 'time' ? Math.floor(nowMs / M) * M : today * DAY + Math.floor(nowMs / M) * M;
		return nearestAllowed(limits, v);
	});
	function useToday() {
		const v = todayValue;
		if (v === null) return;
		if (k === 'date' || k === 'week' || k === 'month') {
			onChoose(formatValue(k, v));
			return;
		}
		// Time kinds fill the columns; Set still applies.
		if (k === 'datetime-local') {
			selectedDay = Math.floor(v / DAY);
			cursor = selectedDay;
			time = v - selectedDay * DAY;
		} else time = v;
	}

	function keydown(e: KeyboardEvent) {
		e.stopPropagation();
		if (e.key === 'Escape') {
			e.preventDefault();
			onCancel();
		}
	}

	const readout = $derived.by(() => {
		if (!hasTime) return '';
		const t = (showSeconds ? timeSecFmt : timeFmt).format(Date.UTC(2020, 0, 1) + time);
		if (k === 'time') return t;
		return selectedDay === null ? `Pick a day · ${t}` : `${readoutDateFmt.format(selectedDay * DAY)} · ${t}`;
	});

	onMount(() => {
		// Each time column opens on its chosen value.
		revealCell('h', hour, true);
		revealCell('m', minute, true);
		if (showSeconds) revealCell('s', second, true);
		if (!autofocus) return;
		if (hasCalendar) focusCursorNow();
		else (document.querySelector(`#${uid} [data-col="h"]`) as HTMLElement | null)?.focus({ preventScroll: true });
	});
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="bdp" id={uid} onkeydown={keydown}>
	{#if hasTime}
		<div class="bdp-readout" aria-live="polite">{readout}</div>
	{/if}
	<div class="bdp-body" class:with-time={hasTime && hasCalendar}>
		{#if hasCalendar}
			<div class="bdp-cal">
				<div class="bdp-nav">
					<button
						type="button"
						class="bdp-icon"
						aria-label={mode === 'days' ? 'Previous month' : 'Previous year'}
						onclick={() => {
							if (mode === 'days') cursor = shiftMonths(cursor, -1);
							else monthCursor -= 12;
						}}><ChevronLeft size={16} strokeWidth={2} /></button
					>
					{#if k === 'month'}
						<span class="bdp-title" aria-live="polite">{monthYear}</span>
					{:else}
						<button
							type="button"
							class="bdp-title as-button"
							aria-expanded={mode === 'months'}
							aria-label={mode === 'days' ? `${monthYearFmt.format(daysFromCivil(cursorCivil.year, cursorCivil.month, 1) * DAY)}, choose a month` : `${monthYear}, back to days`}
							onclick={() => {
								if (mode === 'days') {
									monthCursor = monthIndex(cursorCivil.year, cursorCivil.month);
									mode = 'months';
								} else mode = 'days';
								void focusCursor();
							}}
						>
							<span aria-live="polite">{mode === 'days' ? monthYearFmt.format(daysFromCivil(cursorCivil.year, cursorCivil.month, 1) * DAY) : monthYear}</span>
							<ChevronDown size={14} strokeWidth={2} class={mode === 'months' ? 'flip' : ''} />
						</button>
					{/if}
					<button
						type="button"
						class="bdp-icon"
						aria-label={mode === 'days' ? 'Next month' : 'Next year'}
						onclick={() => {
							if (mode === 'days') cursor = shiftMonths(cursor, 1);
							else monthCursor += 12;
						}}><ChevronRight size={16} strokeWidth={2} /></button
					>
				</div>

				{#if mode === 'months'}
					<div class="bdp-months" role="grid" aria-label={`${label}: months of ${monthYear}`}>
						{#each [0, 1, 2, 3] as row (row)}
							<div class="bdp-mrow" role="row">
								{#each [0, 1, 2] as col (col)}
									{@const m = monthIndex(monthYear, row * 3 + col + 1)}
									{@const ok = monthOk(m)}
									<button
										type="button"
										role="gridcell"
										class="bdp-month"
										class:selected={k === 'month' ? m === selectedMonth : m === monthIndex(cursorCivil.year, cursorCivil.month)}
										class:current={m === monthIndex(civilFromDays(today).year, civilFromDays(today).month)}
										data-month={m}
										tabindex={m === monthCursor ? 0 : -1}
										aria-disabled={!ok}
										aria-selected={k === 'month' ? m === selectedMonth : undefined}
										onclick={() => {
											monthCursor = m;
											chooseMonth(m);
										}}
										onkeydown={monthKey}>{monthShortFmt.format(Date.UTC(2020, row * 3 + col, 1))}</button
									>
								{/each}
							</div>
						{/each}
					</div>
				{:else}
					<div class="bdp-days" class:weeks={k === 'week'} role="grid" aria-label={`${label}: ${monthYearFmt.format(daysFromCivil(cursorCivil.year, cursorCivil.month, 1) * DAY)}`}>
						<div class="bdp-row head" role="row">
							{#if k === 'week'}<span class="bdp-wk" role="columnheader" aria-label="Week">Wk</span>{/if}
							{#each weekdays as wd (wd.long)}
								<span class="bdp-wd" role="columnheader" aria-label={wd.long}><abbr title={wd.long}>{wd.short}</abbr></span>
							{/each}
						</div>
						{#each weeks as days (days[0])}
							{@const weekNo = Number(formatValue('week', weekIndexOf(days[0])).slice(-2))}
							{@const weekSelected = k === 'week' && selectedDay !== null && weekIndexOf(days[0]) === weekIndexOf(selectedDay)}
							<div class="bdp-row" class:week-selected={weekSelected} class:week-ok={k === 'week' && dayOk(days[0])} role="row">
								{#if k === 'week'}<span class="bdp-wk" role="rowheader">{weekNo}</span>{/if}
								{#each days as d (d)}
									{@const c = civilFromDays(d)}
									{@const ok = dayOk(d)}
									<button
										type="button"
										role="gridcell"
										class="bdp-day"
										class:outside={c.month !== cursorCivil.month}
										class:today={d === today}
										class:selected={k !== 'week' && d === selectedDay}
										data-day={d}
										tabindex={d === cursor ? 0 : -1}
										aria-label={k === 'week' ? `Week ${weekNo}, ${dayFmt.format(d * DAY)}` : dayFmt.format(d * DAY)}
										aria-selected={k === 'week' ? weekSelected : d === selectedDay}
										aria-disabled={!ok}
										aria-current={d === today ? 'date' : undefined}
										onclick={() => {
											cursor = d;
											chooseDay(d);
										}}
										onkeydown={dayKey}>{c.day}</button
									>
								{/each}
							</div>
						{/each}
					</div>
				{/if}
			</div>
		{/if}

		{#if hasTime}
			<div class="bdp-time" class:solo={!hasCalendar}>
				{#each [{ unit: 'h', name: 'Hours', cells: hours, current: hour }, { unit: 'm', name: 'Minutes', cells: minutes, current: minute }, ...(showSeconds ? [{ unit: 's', name: 'Seconds', cells: seconds, current: second }] : [])] as col (col.unit)}
					<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
					<div
						class="bdp-col"
						role="listbox"
						tabindex="0"
						aria-label={col.name}
						data-col={col.unit}
						aria-activedescendant={`${uid}-${col.unit}-${col.current}`}
						onkeydown={(e) => columnKey(e, col.unit as 'h' | 'm' | 's', col.cells, col.current)}
					>
						{#each col.cells as cell (cell.v)}
							<div
								id={`${uid}-${col.unit}-${cell.v}`}
								class="bdp-cell"
								class:selected={cell.v === col.current}
								role="option"
								aria-selected={cell.v === col.current}
								aria-disabled={!cell.ok}
								tabindex="-1"
								data-v={cell.v}
								onclick={() => { if (cell.ok) pick(col.unit as 'h' | 'm' | 's', cell.v); }}
								onkeydown={(e) => columnKey(e, col.unit as 'h' | 'm' | 's', col.cells, col.current)}
							>
								{cell.label}
							</div>
						{/each}
					</div>
				{/each}
			</div>
		{/if}
	</div>

	<div class="bdp-foot">
		<button type="button" class="bdp-link" onclick={() => onChoose('')}>Clear</button>
		<button type="button" class="bdp-link" disabled={todayValue === null} onclick={useToday}>{todayLabel}</button>
		{#if hasTime}
			<span class="bdp-spacer"></span>
			<button type="button" class="bdp-btn ghost" onclick={onCancel}>Cancel</button>
			<button type="button" class="bdp-btn primary" disabled={!canSet} onclick={setTime}>Set</button>
		{/if}
	</div>
</div>

<style>
	.bdp {
		display: flex;
		flex-direction: column;
		min-height: 0;
		flex: 1;
		container-type: inline-size;
		font-variant-numeric: tabular-nums;
	}

	.bdp-readout {
		margin: 0 12px 8px;
		padding: 8px 10px;
		font-family: var(--font-mono);
		font-size: 0.8125rem;
		font-weight: 500;
		letter-spacing: -0.01em;
		color: var(--foreground);
		background: color-mix(in oklch, var(--primary) 9%, transparent);
		border: 1px solid color-mix(in oklch, var(--primary) 22%, transparent);
		border-radius: calc(var(--radius) - 4px);
	}

	.bdp-body {
		display: flex;
		flex-wrap: wrap;
		gap: 10px 14px;
		padding: 0 10px;
		min-height: 0;
		overflow-y: auto;
		overscroll-behavior: contain;
	}
	.bdp-cal {
		flex: 1 1 252px;
		min-width: 0;
	}

	.bdp-nav {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 4px;
		margin-bottom: 6px;
	}
	.bdp-title {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		padding: 4px 8px;
		font-size: 0.875rem;
		font-weight: 600;
		letter-spacing: -0.01em;
		color: var(--foreground);
		background: transparent;
		border: 0;
		border-radius: 6px;
	}
	.bdp-title.as-button {
		cursor: pointer;
		font: inherit;
		font-size: 0.875rem;
		font-weight: 600;
	}
	.bdp-title.as-button:hover {
		background: var(--hover-overlay, color-mix(in oklch, var(--muted) 70%, transparent));
	}
	.bdp-title :global(.flip) {
		transform: rotate(180deg);
	}
	.bdp-icon {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 30px;
		height: 30px;
		color: var(--muted-foreground);
		background: transparent;
		border: 0;
		border-radius: 8px;
		cursor: pointer;
		transition: background-color 120ms ease, color 120ms ease;
	}
	.bdp-icon:hover {
		color: var(--foreground);
		background: var(--hover-overlay, color-mix(in oklch, var(--muted) 70%, transparent));
	}

	.bdp-row {
		display: grid;
		grid-template-columns: repeat(7, minmax(0, 1fr));
		gap: 2px;
	}
	.bdp-days.weeks .bdp-row {
		grid-template-columns: 28px repeat(7, minmax(0, 1fr));
		border-radius: 8px;
	}
	.bdp-row.head {
		margin-bottom: 2px;
	}
	.bdp-wd,
	.bdp-wk {
		display: flex;
		align-items: center;
		justify-content: center;
		height: 24px;
		font-size: 0.6875rem;
		font-weight: 600;
		color: var(--muted-foreground);
	}
	.bdp-wd abbr {
		text-decoration: none;
	}
	.bdp-wk {
		height: auto; /* a row header spans its row, whatever the day height */
		align-self: stretch;
		font-family: var(--font-mono);
		font-size: 0.625rem;
		color: var(--subtle-foreground, var(--muted-foreground));
	}
	.bdp-days.weeks .bdp-row.week-ok:hover:not(.head) {
		background: color-mix(in oklch, var(--primary) 9%, transparent);
	}
	.bdp-days.weeks .bdp-row.week-selected {
		background: color-mix(in oklch, var(--primary) 18%, transparent);
	}
	.bdp-days.weeks .bdp-row.week-selected .bdp-day,
	.bdp-days.weeks .bdp-row.week-selected .bdp-wk {
		color: var(--primary);
		font-weight: 600;
	}

	.bdp-day,
	.bdp-month,
	.bdp-cell {
		font-family: var(--font-mono);
		color: var(--popover-foreground);
		background: transparent;
		border: 0;
		cursor: pointer;
		transition: background-color 100ms ease, color 100ms ease, box-shadow 100ms ease;
	}
	.bdp-day {
		position: relative;
		height: 34px;
		font-size: 0.8125rem;
		border-radius: 8px;
	}
	.bdp-day:hover:not([aria-disabled='true']),
	.bdp-month:hover:not([aria-disabled='true']) {
		background: color-mix(in oklch, var(--primary) 12%, transparent);
	}
	.bdp-day.outside {
		color: var(--muted-foreground);
		opacity: 0.6;
	}
	.bdp-day.today::after {
		content: '';
		position: absolute;
		left: 50%;
		bottom: 4px;
		width: 4px;
		height: 4px;
		margin-left: -2px;
		border-radius: 50%;
		background: var(--primary);
	}
	.bdp-day.selected,
	.bdp-month.selected,
	.bdp-cell.selected {
		color: var(--primary-foreground);
		background: var(--primary);
		font-weight: 600;
		box-shadow: 0 2px 8px -2px color-mix(in oklch, var(--primary) 55%, transparent);
	}
	.bdp-day.selected.today::after {
		background: var(--primary-foreground);
	}
	.bdp-day[aria-disabled='true'],
	.bdp-month[aria-disabled='true'],
	.bdp-cell[aria-disabled='true'] {
		color: var(--subtle-foreground, var(--muted-foreground));
		opacity: 0.35;
		cursor: not-allowed;
		text-decoration: line-through;
		text-decoration-color: color-mix(in oklch, currentColor 50%, transparent);
	}
	.bdp-day:focus-visible,
	.bdp-month:focus-visible,
	.bdp-icon:focus-visible,
	.bdp-title.as-button:focus-visible,
	.bdp-link:focus-visible,
	.bdp-btn:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 1px;
	}

	.bdp-months {
		display: grid;
		gap: 4px;
		padding-top: 2px;
	}
	.bdp-mrow {
		display: grid;
		grid-template-columns: repeat(3, 1fr);
		gap: 4px;
	}
	.bdp-month {
		height: 44px;
		font-family: var(--font-sans);
		font-size: 0.8125rem;
		font-weight: 500;
		border-radius: 10px;
	}
	.bdp-month.current:not(.selected) {
		box-shadow: inset 0 0 0 1px color-mix(in oklch, var(--primary) 60%, transparent);
	}

	.bdp-time {
		flex: 1 1 168px;
		display: flex;
		gap: 4px;
		min-width: 0;
		height: 220px;
		padding: 4px;
		background: color-mix(in oklch, var(--muted) 55%, transparent);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius) - 2px);
	}
	.bdp-time.solo {
		height: 248px;
	}
	.bdp-col {
		flex: 1;
		min-width: 0;
		overflow-y: auto;
		overscroll-behavior: contain;
		touch-action: pan-y;
		scroll-snap-type: y proximity;
		scrollbar-width: none;
		border-radius: 8px;
		outline: none;
		padding: 2px;
	}
	.bdp-col::-webkit-scrollbar {
		display: none;
	}
	.bdp-col:focus-visible {
		box-shadow: inset 0 0 0 1.5px var(--ring);
	}
	.bdp-cell {
		display: flex;
		align-items: center;
		justify-content: center;
		height: 32px;
		margin-bottom: 2px;
		font-size: 0.8125rem;
		border-radius: 7px;
		white-space: nowrap;
		scroll-snap-align: center;
		user-select: none;
		-webkit-user-select: none;
	}
	.bdp-cell:hover:not([aria-disabled='true']):not(.selected) {
		background: color-mix(in oklch, var(--primary) 12%, transparent);
	}

	.bdp-foot {
		display: flex;
		align-items: center;
		gap: 4px;
		margin-top: 8px;
		padding: 8px 10px 2px;
		border-top: 1px solid var(--border);
		flex-shrink: 0;
	}
	.bdp-spacer {
		flex: 1;
	}
	.bdp-link {
		padding: 6px 8px;
		font: inherit;
		font-size: 0.75rem;
		font-weight: 600;
		color: var(--primary);
		background: transparent;
		border: 0;
		border-radius: 6px;
		cursor: pointer;
	}
	.bdp-link:hover:not(:disabled) {
		background: color-mix(in oklch, var(--primary) 12%, transparent);
	}
	.bdp-link:disabled {
		color: var(--muted-foreground);
		opacity: 0.5;
		cursor: not-allowed;
	}
	.bdp-btn {
		height: 30px;
		padding: 0 14px;
		font: inherit;
		font-size: 0.75rem;
		font-weight: 600;
		border-radius: 8px;
		cursor: pointer;
		transition: background-color 120ms ease, opacity 120ms ease, transform 80ms ease;
	}
	.bdp-btn:active:not(:disabled) {
		transform: translateY(1px);
	}
	.bdp-btn.ghost {
		color: var(--foreground);
		background: transparent;
		border: 1px solid var(--border-strong, var(--border));
	}
	.bdp-btn.ghost:hover {
		background: var(--hover-overlay, color-mix(in oklch, var(--muted) 70%, transparent));
	}
	.bdp-btn.primary {
		color: var(--primary-foreground);
		background: var(--primary);
		border: 1px solid transparent;
	}
	.bdp-btn.primary:hover:not(:disabled) {
		background: color-mix(in oklch, var(--primary) 88%, var(--foreground));
	}
	.bdp-btn:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}

	@container (max-width: 440px) {
		.bdp-time:not(.solo) {
			flex-basis: 100%;
			height: 168px;
		}
	}

	@media (pointer: coarse) {
		.bdp-day {
			height: 40px;
			font-size: 0.9375rem;
		}
		.bdp-cell {
			height: 40px;
			font-size: 0.9375rem;
		}
		.bdp-icon {
			width: 40px;
			height: 40px;
		}
		.bdp-btn {
			height: 40px;
			padding: 0 18px;
		}
		.bdp-link {
			padding: 10px 10px;
		}
	}
</style>

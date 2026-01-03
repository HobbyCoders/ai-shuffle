/**
 * Tests for date utility functions
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { formatRelativeTime, formatCost, formatTimeOfDay, getStatusDisplay } from './dateGroups';

describe('formatRelativeTime', () => {
	beforeEach(() => {
		// Mock Date.now to return a fixed time
		vi.useFakeTimers();
		vi.setSystemTime(new Date('2024-01-15T12:00:00Z'));
	});

	afterEach(() => {
		vi.useRealTimers();
	});

	it('should return "now" for times less than a minute ago', () => {
		const date = new Date('2024-01-15T11:59:30Z').toISOString();
		expect(formatRelativeTime(date)).toBe('now');
	});

	it('should return minutes for times less than an hour ago', () => {
		const date = new Date('2024-01-15T11:30:00Z').toISOString();
		expect(formatRelativeTime(date)).toBe('30m ago');
	});

	it('should return abbreviated minutes when specified', () => {
		const date = new Date('2024-01-15T11:30:00Z').toISOString();
		expect(formatRelativeTime(date, true)).toBe('30m');
	});

	it('should return hours for times less than a day ago', () => {
		const date = new Date('2024-01-15T06:00:00Z').toISOString();
		expect(formatRelativeTime(date)).toBe('6h ago');
	});

	it('should return "Yesterday" for times 1 day ago', () => {
		const date = new Date('2024-01-14T12:00:00Z').toISOString();
		expect(formatRelativeTime(date)).toBe('Yesterday');
	});

	it('should return abbreviated day for 1 day ago', () => {
		const date = new Date('2024-01-14T12:00:00Z').toISOString();
		expect(formatRelativeTime(date, true)).toBe('1d');
	});

	it('should return days for times less than a week ago', () => {
		const date = new Date('2024-01-12T12:00:00Z').toISOString();
		expect(formatRelativeTime(date)).toBe('3d ago');
	});

	it('should return weeks for times less than a month ago', () => {
		const date = new Date('2024-01-01T12:00:00Z').toISOString();
		expect(formatRelativeTime(date)).toBe('2w ago');
	});

	it('should return formatted date for times over a month ago', () => {
		const date = new Date('2023-12-01T12:00:00Z').toISOString();
		const result = formatRelativeTime(date);
		expect(result).toMatch(/Dec \d+/);
	});
});

describe('formatCost', () => {
	it('should return empty string for undefined cost', () => {
		expect(formatCost(undefined)).toBe('');
	});

	it('should return empty string for null cost', () => {
		expect(formatCost(null)).toBe('');
	});

	it('should return empty string for zero cost', () => {
		expect(formatCost(0)).toBe('');
	});

	it('should format small costs with two decimal places', () => {
		expect(formatCost(0.05)).toBe('$0.05');
		expect(formatCost(0.99)).toBe('$0.99');
	});

	it('should format costs with abbreviated format when specified', () => {
		expect(formatCost(15.50, true)).toBe('$16');
		expect(formatCost(5.75, true)).toBe('$5.8');
		expect(formatCost(0.50, true)).toBe('$0.50');
	});

	it('should format medium costs correctly', () => {
		expect(formatCost(5.50)).toBe('$5.50');
		expect(formatCost(9.99)).toBe('$9.99');
	});

	it('should round large costs when abbreviated', () => {
		expect(formatCost(25.99, true)).toBe('$26');
		expect(formatCost(100.50, true)).toBe('$101');
	});
});

describe('formatTimeOfDay', () => {
	it('should format morning time correctly', () => {
		const date = new Date('2024-01-15T09:30:00').toISOString();
		const result = formatTimeOfDay(date);
		expect(result).toMatch(/9:30\s*AM/i);
	});

	it('should format afternoon time correctly', () => {
		const date = new Date('2024-01-15T14:45:00').toISOString();
		const result = formatTimeOfDay(date);
		expect(result).toMatch(/2:45\s*PM/i);
	});

	it('should format midnight correctly', () => {
		const date = new Date('2024-01-15T00:00:00').toISOString();
		const result = formatTimeOfDay(date);
		expect(result).toMatch(/12:00\s*AM/i);
	});

	it('should format noon correctly', () => {
		const date = new Date('2024-01-15T12:00:00').toISOString();
		const result = formatTimeOfDay(date);
		expect(result).toMatch(/12:00\s*PM/i);
	});
});

describe('getStatusDisplay', () => {
	it('should return Processing status when streaming', () => {
		const result = getStatusDisplay('active', true);
		expect(result).toEqual({ label: 'Processing', color: 'bg-orange-500' });
	});

	it('should return Active Session for active status when open', () => {
		const result = getStatusDisplay('active', false, true);
		expect(result).toEqual({ label: 'Active Session', color: 'bg-teal-500' });
	});

	it('should return Previous Session for active status when not open', () => {
		const result = getStatusDisplay('active', false, false);
		expect(result).toEqual({ label: 'Previous Session', color: 'bg-teal-500' });
	});

	it('should return Error status for error status', () => {
		const result = getStatusDisplay('error', false);
		expect(result).toEqual({ label: 'Error', color: 'bg-destructive' });
	});

	it('should return null for other statuses', () => {
		const result = getStatusDisplay('completed', false);
		expect(result).toBeNull();
	});

	it('should prioritize streaming over other statuses', () => {
		const result = getStatusDisplay('error', true);
		expect(result?.label).toBe('Processing');
	});
});

import { describe, it, expect, vi } from 'vitest';
import { chromium } from 'playwright-core';
import { BrowserManager } from '../src/browser.js';

describe('CDP connection deadline', () => {
  it('bounds the Playwright handshake and reports an unresponsive browser clearly', async () => {
    const connect = vi.spyOn(chromium, 'connectOverCDP').mockRejectedValue(new Error('Timeout 4000ms exceeded'));
    try {
      const manager = new BrowserManager({ endpoint: 'http://127.0.0.1:1', maxRetries: 1 });
      await expect(manager.newPage()).rejects.toThrow('Chrome or a page may be unresponsive');
      expect(connect).toHaveBeenCalledWith('http://127.0.0.1:1', { timeout: 4000 });
    } finally { connect.mockRestore(); }
  });
});

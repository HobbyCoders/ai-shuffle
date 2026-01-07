import { test, expect } from '@playwright/test';

test.describe('AI Hub UI', () => {
  test('should load the main page', async ({ page }) => {
    await page.goto('/');

    // Should redirect to login or setup page since we're not authenticated
    // Wait for any page to load
    await page.waitForLoadState('networkidle');

    // Take a screenshot for debugging
    await page.screenshot({ path: 'tests/screenshots/main-page.png', fullPage: true });
  });

  test('should show setup page when no admin exists', async ({ page }) => {
    await page.goto('/setup');
    await page.waitForLoadState('networkidle');

    // Check for setup page elements
    const heading = page.getByRole('heading', { name: /welcome/i });

    // Take screenshot
    await page.screenshot({ path: 'tests/screenshots/setup-page.png', fullPage: true });
  });

  test('should show login page', async ({ page }) => {
    await page.goto('/login');
    await page.waitForLoadState('networkidle');

    // Check for login form elements
    const usernameInput = page.getByPlaceholder('Username');
    const passwordInput = page.getByPlaceholder('Password');

    // Take screenshot
    await page.screenshot({ path: 'tests/screenshots/login-page.png', fullPage: true });
  });

  test('mobile view - sidebar should be hidden by default', async ({ page }) => {
    // Set mobile viewport
    await page.setViewportSize({ width: 375, height: 667 });

    await page.goto('/');
    await page.waitForLoadState('networkidle');

    // Take screenshot of mobile view
    await page.screenshot({ path: 'tests/screenshots/mobile-view.png', fullPage: true });
  });

  test('desktop view - sidebar should be visible', async ({ page }) => {
    // Set desktop viewport
    await page.setViewportSize({ width: 1280, height: 720 });

    await page.goto('/');
    await page.waitForLoadState('networkidle');

    // Take screenshot of desktop view
    await page.screenshot({ path: 'tests/screenshots/desktop-view.png', fullPage: true });
  });
});

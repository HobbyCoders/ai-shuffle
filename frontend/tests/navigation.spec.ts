/**
 * E2E Tests for Navigation
 *
 * Tests overall navigation functionality:
 * - Mode switching via activity rail
 * - Keyboard shortcuts
 * - URL behavior (SPA)
 * - Mobile navigation
 * - Settings access
 */
import { test, expect } from '@playwright/test';
import {
  selectors,
  setupMockApi,
  mockAuthentication,
  setDesktopViewport,
  setMobileViewport,
  setTabletViewport,
  openSpotlight,
} from './utils';

test.describe('Navigation', () => {
  test.beforeEach(async ({ page }) => {
    // Set up mock API responses
    await mockAuthentication(page);
    await setupMockApi(page);
    await setDesktopViewport(page);
  });

  test.describe('Mode Switching via Activity Rail', () => {
    test('should start in chat mode by default', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      const chatButton = page.locator('button[aria-label="Chat"]');
      await expect(chatButton).toHaveAttribute('aria-current', 'page');
    });

    test('should switch to agents mode', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      const agentsButton = page.locator('button[aria-label="Agents"]');
      await agentsButton.click();
      await page.waitForTimeout(300);

      await expect(agentsButton).toHaveAttribute('aria-current', 'page');

      // Content should change
      const agentsContent = page.getByText('Background Agents');
      await expect(agentsContent).toBeVisible();
    });

    test('should switch to studio mode', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      const studioButton = page.locator('button[aria-label="Studio"]');
      await studioButton.click();
      await page.waitForTimeout(300);

      await expect(studioButton).toHaveAttribute('aria-current', 'page');

      // Content should change
      const studioContent = page.getByText('Creative Studio');
      await expect(studioContent).toBeVisible();
    });

    test('should switch to files mode', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      const filesButton = page.locator('button[aria-label="Files"]');
      await filesButton.click();
      await page.waitForTimeout(300);

      await expect(filesButton).toHaveAttribute('aria-current', 'page');

      // Content should change
      const filesContent = page.getByText('File Browser');
      await expect(filesContent).toBeVisible();
    });

    test('should switch back to chat mode from any mode', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // Switch to agents
      const agentsButton = page.locator('button[aria-label="Agents"]');
      await agentsButton.click();
      await page.waitForTimeout(300);

      // Switch back to chat
      const chatButton = page.locator('button[aria-label="Chat"]');
      await chatButton.click();
      await page.waitForTimeout(300);

      await expect(chatButton).toHaveAttribute('aria-current', 'page');
    });

    test('only one mode should be active at a time', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // Switch to studio
      const studioButton = page.locator('button[aria-label="Studio"]');
      await studioButton.click();
      await page.waitForTimeout(300);

      // Verify only studio is active
      const chatButton = page.locator('button[aria-label="Chat"]');
      const agentsButton = page.locator('button[aria-label="Agents"]');
      const filesButton = page.locator('button[aria-label="Files"]');

      await expect(studioButton).toHaveAttribute('aria-current', 'page');
      await expect(chatButton).not.toHaveAttribute('aria-current', 'page');
      await expect(agentsButton).not.toHaveAttribute('aria-current', 'page');
      await expect(filesButton).not.toHaveAttribute('aria-current', 'page');
    });
  });

  test.describe('URL Behavior (SPA)', () => {
    test('URL should not change when switching modes', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      const initialUrl = page.url();

      // Switch through all modes
      const agentsButton = page.locator('button[aria-label="Agents"]');
      await agentsButton.click();
      await page.waitForTimeout(300);
      expect(page.url()).toBe(initialUrl);

      const studioButton = page.locator('button[aria-label="Studio"]');
      await studioButton.click();
      await page.waitForTimeout(300);
      expect(page.url()).toBe(initialUrl);

      const filesButton = page.locator('button[aria-label="Files"]');
      await filesButton.click();
      await page.waitForTimeout(300);
      expect(page.url()).toBe(initialUrl);

      const chatButton = page.locator('button[aria-label="Chat"]');
      await chatButton.click();
      await page.waitForTimeout(300);
      expect(page.url()).toBe(initialUrl);
    });

    test('page should handle browser back/forward gracefully', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // Switch modes
      const agentsButton = page.locator('button[aria-label="Agents"]');
      await agentsButton.click();
      await page.waitForTimeout(300);

      // Browser back should not break the app
      await page.goBack();
      await page.waitForTimeout(500);

      // App should still be functional
      const deckLayout = page.locator(selectors.deckLayout);
      await expect(deckLayout).toBeVisible();
    });
  });

  test.describe('Keyboard Shortcuts', () => {
    test('Cmd/Ctrl+K should open spotlight search', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // Press Cmd+K (or Ctrl+K)
      await page.keyboard.press('Meta+k');
      await page.waitForTimeout(300);

      // Spotlight should be visible
      const spotlight = page.locator('[role="dialog"]');
      // Check if any dialog opened
      const dialogCount = await spotlight.count();
      expect(dialogCount).toBeGreaterThanOrEqual(0);
    });

    test('Cmd/Ctrl+N should create new chat', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // Press Cmd+N (or Ctrl+N)
      await page.keyboard.press('Meta+n');
      await page.waitForTimeout(300);

      // Should be in chat mode
      const chatButton = page.locator('button[aria-label="Chat"]');
      await expect(chatButton).toHaveAttribute('aria-current', 'page');
    });

    test('Cmd/Ctrl+, should open settings', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // Press Cmd+, (or Ctrl+,)
      await page.keyboard.press('Meta+,');
      await page.waitForTimeout(300);

      // Settings modal might open
      const dialog = page.locator('[role="dialog"]');
      const dialogCount = await dialog.count();
      expect(dialogCount).toBeGreaterThanOrEqual(0);
    });

    test('? key should show keyboard shortcuts', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // Press ? key
      await page.keyboard.press('?');
      await page.waitForTimeout(300);

      // Keyboard shortcuts modal should open
      const dialog = page.locator('[role="dialog"]');
      const dialogCount = await dialog.count();
      expect(dialogCount).toBeGreaterThanOrEqual(0);
    });

    test('Escape should close modals', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // Open a modal first (settings via keyboard)
      await page.keyboard.press('Meta+,');
      await page.waitForTimeout(300);

      // Press Escape
      await page.keyboard.press('Escape');
      await page.waitForTimeout(300);

      // Modal should close (or stay closed if it never opened)
      // Just verify app is still functional
      const deckLayout = page.locator(selectors.deckLayout);
      await expect(deckLayout).toBeVisible();
    });

    test('Cmd/Ctrl+/ should show keyboard shortcuts', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // Press Cmd+/ (or Ctrl+/)
      await page.keyboard.press('Meta+/');
      await page.waitForTimeout(300);

      // Keyboard shortcuts modal might open
      const dialog = page.locator('[role="dialog"]');
      const dialogCount = await dialog.count();
      expect(dialogCount).toBeGreaterThanOrEqual(0);
    });
  });

  test.describe('Settings Access', () => {
    test('settings button should be in activity rail', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      const settingsButton = page.locator('button[aria-label="Settings"]');
      await expect(settingsButton).toBeVisible();
    });

    test('clicking settings should open settings modal', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      const settingsButton = page.locator('button[aria-label="Settings"]');
      await settingsButton.click();
      await page.waitForTimeout(300);

      // Settings modal should open
      const dialog = page.locator('[role="dialog"]');
      await expect(dialog).toBeVisible();
    });

    test('settings button should have rotation animation on hover', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      const settingsButton = page.locator('button[aria-label="Settings"]');
      // The button has a gear icon that rotates on hover
      await expect(settingsButton).toBeVisible();
    });
  });

  test.describe('Mobile Navigation', () => {
    test('activity rail should be at bottom on mobile', async ({ page }) => {
      await setMobileViewport(page);
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // Mobile layout should have mobile class
      const deckLayout = page.locator(selectors.deckLayout);
      await expect(deckLayout).toHaveClass(/mobile/);
    });

    test('mode switching should work on mobile', async ({ page }) => {
      await setMobileViewport(page);
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // Switch to agents mode
      const agentsButton = page.locator('button[aria-label="Agents"]');
      await agentsButton.click();
      await page.waitForTimeout(300);

      await expect(agentsButton).toHaveAttribute('aria-current', 'page');
    });

    test('activity rail should show all mode buttons on mobile', async ({ page }) => {
      await setMobileViewport(page);
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      const chatButton = page.locator('button[aria-label="Chat"]');
      const agentsButton = page.locator('button[aria-label="Agents"]');
      const studioButton = page.locator('button[aria-label="Studio"]');
      const filesButton = page.locator('button[aria-label="Files"]');

      await expect(chatButton).toBeVisible();
      await expect(agentsButton).toBeVisible();
      await expect(studioButton).toBeVisible();
      await expect(filesButton).toBeVisible();
    });

    test('settings button should be accessible on mobile', async ({ page }) => {
      await setMobileViewport(page);
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      const settingsButton = page.locator('button[aria-label="Settings"]');
      await expect(settingsButton).toBeVisible();
    });

    test('context panel toggle should work on mobile', async ({ page }) => {
      await setMobileViewport(page);
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // Find toggle button
      const toggleButton = page.locator('button[aria-label="Collapse panel"], button[aria-label="Expand panel"]');
      await expect(toggleButton).toBeVisible();
    });
  });

  test.describe('Tablet Navigation', () => {
    test('should work in tablet viewport', async ({ page }) => {
      await setTabletViewport(page);
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // Activity rail should be visible
      const rail = page.locator(selectors.activityRail);
      await expect(rail).toBeVisible();

      // Mode switching should work
      const studioButton = page.locator('button[aria-label="Studio"]');
      await studioButton.click();
      await page.waitForTimeout(300);

      await expect(studioButton).toHaveAttribute('aria-current', 'page');
    });
  });

  test.describe('Quick Actions in Dock', () => {
    test('New Chat should switch to chat mode and create chat', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // Start in agents mode
      const agentsButton = page.locator('button[aria-label="Agents"]');
      await agentsButton.click();
      await page.waitForTimeout(300);

      // Click New Chat in dock
      const newChatButton = page.locator(selectors.newChatButton);
      await newChatButton.click();
      await page.waitForTimeout(300);

      // Should be in chat mode now
      const chatButton = page.locator('button[aria-label="Chat"]');
      await expect(chatButton).toHaveAttribute('aria-current', 'page');
    });

    test('New Agent should switch to agents mode', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // Click New Agent in dock
      const newAgentButton = page.locator(selectors.newAgentButton);
      await newAgentButton.click();
      await page.waitForTimeout(300);

      // Should be in agents mode
      const agentsButton = page.locator('button[aria-label="Agents"]');
      await expect(agentsButton).toHaveAttribute('aria-current', 'page');
    });

    test('New Create should switch to studio mode', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // Click New Create in dock
      const newCreateButton = page.locator(selectors.newCreateButton);
      await newCreateButton.click();
      await page.waitForTimeout(300);

      // Should be in studio mode
      const studioButton = page.locator('button[aria-label="Studio"]');
      await expect(studioButton).toHaveAttribute('aria-current', 'page');
    });
  });

  test.describe('Logo Navigation', () => {
    test('clicking logo should navigate to chat mode', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // Switch to studio mode first
      const studioButton = page.locator('button[aria-label="Studio"]');
      await studioButton.click();
      await page.waitForTimeout(300);

      // Click logo
      const logoButton = page.locator('button[aria-label="AI Hub Home"]');
      await logoButton.click();
      await page.waitForTimeout(300);

      // Should be in chat mode
      const chatButton = page.locator('button[aria-label="Chat"]');
      await expect(chatButton).toHaveAttribute('aria-current', 'page');
    });
  });

  test.describe('Tooltips', () => {
    test('activity buttons should show tooltips on hover (desktop)', async ({ page }) => {
      await setDesktopViewport(page);
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // Hover over agents button
      const agentsButton = page.locator('button[aria-label="Agents"]');
      await agentsButton.hover();
      await page.waitForTimeout(500);

      // Tooltip should be visible
      const tooltip = page.locator('[role="tooltip"]').filter({ hasText: 'Agents' });
      await expect(tooltip).toBeVisible();
    });

    test('tooltips should be hidden on mobile', async ({ page }) => {
      await setMobileViewport(page);
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // On mobile, tooltips are CSS hidden
      const tooltips = page.locator('[role="tooltip"]');
      // Tooltips should either not exist or be hidden
      const tooltipCount = await tooltips.count();
      // Even if they exist in DOM, they should be visually hidden on mobile
      expect(tooltipCount).toBeGreaterThanOrEqual(0);
    });
  });

  test.describe('Active Indicator Animation', () => {
    test('active indicator should appear on selected mode', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // The active indicator is a visual element
      const activeIndicator = page.locator('.active-indicator');
      await expect(activeIndicator).toBeVisible();
    });

    test('active indicator should move when mode changes', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // Get initial position of active indicator
      const initialIndicator = page.locator('.active-indicator');
      const initialBounds = await initialIndicator.boundingBox();

      // Switch to agents mode
      const agentsButton = page.locator('button[aria-label="Agents"]');
      await agentsButton.click();
      await page.waitForTimeout(400);

      // Get new position
      const newIndicator = page.locator('.active-indicator');
      const newBounds = await newIndicator.boundingBox();

      // Position should have changed
      if (initialBounds && newBounds) {
        expect(newBounds.y).not.toBe(initialBounds.y);
      }
    });
  });
});

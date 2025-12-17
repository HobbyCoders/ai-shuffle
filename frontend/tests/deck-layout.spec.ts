/**
 * E2E Tests for Deck Layout
 *
 * Tests the core Deck layout components:
 * - Activity rail with navigation icons
 * - Context panel collapse/expand
 * - Dock with quick actions
 * - Responsive layout for mobile/desktop
 */
import { test, expect } from '@playwright/test';
import {
  selectors,
  setupMockApi,
  mockAuthentication,
  setMobileViewport,
  setDesktopViewport,
  setTabletViewport,
  viewports,
} from './utils';

test.describe('Deck Layout', () => {
  test.beforeEach(async ({ page }) => {
    // Set up mock API responses
    await mockAuthentication(page);
    await setupMockApi(page);
  });

  test.describe('Activity Rail', () => {
    test('should render activity rail with all navigation icons', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // Check that activity rail is visible
      const rail = page.locator(selectors.activityRail);
      await expect(rail).toBeVisible();

      // Check for all activity mode buttons
      const chatButton = page.locator('button[aria-label="Chat"]');
      const agentsButton = page.locator('button[aria-label="Agents"]');
      const studioButton = page.locator('button[aria-label="Studio"]');
      const filesButton = page.locator('button[aria-label="Files"]');

      await expect(chatButton).toBeVisible();
      await expect(agentsButton).toBeVisible();
      await expect(studioButton).toBeVisible();
      await expect(filesButton).toBeVisible();
    });

    test('should show active indicator on current mode', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // Chat should be the default active mode
      const chatButton = page.locator('button[aria-label="Chat"]');
      await expect(chatButton).toHaveAttribute('aria-current', 'page');
    });

    test('should change mode when clicking rail icons', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // Click on Agents mode
      const agentsButton = page.locator('button[aria-label="Agents"]');
      await agentsButton.click();
      await page.waitForTimeout(300);

      // Agents button should now be active
      await expect(agentsButton).toHaveAttribute('aria-current', 'page');

      // Chat button should no longer be active
      const chatButton = page.locator('button[aria-label="Chat"]');
      await expect(chatButton).not.toHaveAttribute('aria-current', 'page');
    });

    test('should show settings button at bottom of rail', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      const settingsButton = page.locator('button[aria-label="Settings"]');
      await expect(settingsButton).toBeVisible();
    });

    test('should show app logo button at top of rail', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      const logoButton = page.locator('button[aria-label="AI Hub Home"]');
      await expect(logoButton).toBeVisible();
    });

    test('logo button should navigate to chat mode', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // First switch to agents mode
      const agentsButton = page.locator('button[aria-label="Agents"]');
      await agentsButton.click();
      await page.waitForTimeout(300);

      // Click logo to go back to chat
      const logoButton = page.locator('button[aria-label="AI Hub Home"]');
      await logoButton.click();
      await page.waitForTimeout(300);

      // Chat should now be active
      const chatButton = page.locator('button[aria-label="Chat"]');
      await expect(chatButton).toHaveAttribute('aria-current', 'page');
    });
  });

  test.describe('Context Panel', () => {
    test('should render context panel on desktop', async ({ page }) => {
      await setDesktopViewport(page);
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      const contextPanel = page.locator(selectors.contextPanel);
      await expect(contextPanel).toBeVisible();
    });

    test('should have toggle button for collapse/expand', async ({ page }) => {
      await setDesktopViewport(page);
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // Look for the toggle button (collapse panel)
      const toggleButton = page.locator('button[aria-label="Collapse panel"]');
      await expect(toggleButton).toBeVisible();
    });

    test('should collapse when toggle button is clicked', async ({ page }) => {
      await setDesktopViewport(page);
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // Get initial state - panel should be visible
      const contextPanel = page.locator(selectors.contextPanel);
      const initialIsCollapsed = await contextPanel.evaluate(el =>
        el.classList.contains('collapsed')
      );
      expect(initialIsCollapsed).toBe(false);

      // Click toggle to collapse
      const toggleButton = page.locator('button[aria-label="Collapse panel"]');
      await toggleButton.click();
      await page.waitForTimeout(400);

      // Panel should now be collapsed
      const expandButton = page.locator('button[aria-label="Expand panel"]');
      await expect(expandButton).toBeVisible();
    });

    test('should expand when toggle button is clicked on collapsed panel', async ({ page }) => {
      await setDesktopViewport(page);
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // First collapse the panel
      const collapseButton = page.locator('button[aria-label="Collapse panel"]');
      await collapseButton.click();
      await page.waitForTimeout(400);

      // Now expand it
      const expandButton = page.locator('button[aria-label="Expand panel"]');
      await expandButton.click();
      await page.waitForTimeout(400);

      // Collapse button should be visible again
      await expect(collapseButton).toBeVisible();
    });

    test('should display section headings', async ({ page }) => {
      await setDesktopViewport(page);
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // Check for section headings
      const threadsHeading = page.locator('#threads-heading');
      const agentsHeading = page.locator('#agents-heading');
      const generationsHeading = page.locator('#generations-heading');

      await expect(threadsHeading).toBeVisible();
      await expect(agentsHeading).toBeVisible();
      await expect(generationsHeading).toBeVisible();
    });

    test('should toggle section collapse when clicking header', async ({ page }) => {
      await setDesktopViewport(page);
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // Find the threads section header button
      const threadsSectionButton = page.locator('button:has(#threads-heading)');
      await threadsSectionButton.click();
      await page.waitForTimeout(300);

      // Section content should be hidden after collapse
      // The aria-expanded attribute should change
      await expect(threadsSectionButton).toHaveAttribute('aria-expanded', 'false');
    });
  });

  test.describe('Dock', () => {
    test('should render dock at bottom of layout', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      const dock = page.locator(selectors.dock);
      await expect(dock).toBeVisible();
    });

    test('should display quick action buttons', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      const newChatButton = page.locator(selectors.newChatButton);
      const newAgentButton = page.locator(selectors.newAgentButton);
      const newCreateButton = page.locator(selectors.newCreateButton);

      await expect(newChatButton).toBeVisible();
      await expect(newAgentButton).toBeVisible();
      await expect(newCreateButton).toBeVisible();
    });

    test('should have running agents section', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      const runningAgentsGroup = page.locator(selectors.runningAgentsGroup);
      await expect(runningAgentsGroup).toBeVisible();
    });

    test('should have media generations section', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      const mediaGenerationsGroup = page.locator(selectors.mediaGenerationsGroup);
      await expect(mediaGenerationsGroup).toBeVisible();
    });

    test('new chat button should create new chat', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      const newChatButton = page.locator(selectors.newChatButton);
      await newChatButton.click();
      await page.waitForTimeout(300);

      // Should be in chat mode
      const chatButton = page.locator('button[aria-label="Chat"]');
      await expect(chatButton).toHaveAttribute('aria-current', 'page');
    });

    test('new agent button should switch to agents mode', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      const newAgentButton = page.locator(selectors.newAgentButton);
      await newAgentButton.click();
      await page.waitForTimeout(300);

      // Should switch to agents mode
      const agentsButton = page.locator('button[aria-label="Agents"]');
      await expect(agentsButton).toHaveAttribute('aria-current', 'page');
    });

    test('new create button should switch to studio mode', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      const newCreateButton = page.locator(selectors.newCreateButton);
      await newCreateButton.click();
      await page.waitForTimeout(300);

      // Should switch to studio mode
      const studioButton = page.locator('button[aria-label="Studio"]');
      await expect(studioButton).toHaveAttribute('aria-current', 'page');
    });

    test('should show "No active agents" when empty', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      const noAgentsText = page.getByText('No active agents');
      await expect(noAgentsText).toBeVisible();
    });
  });

  test.describe('Responsive Layout', () => {
    test('desktop: should show full layout with rail, main, and context', async ({ page }) => {
      await setDesktopViewport(page);
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // All major components should be visible
      const rail = page.locator(selectors.activityRail);
      const main = page.locator(selectors.mainContent);
      const context = page.locator(selectors.contextPanel);
      const dock = page.locator(selectors.dock);

      await expect(rail).toBeVisible();
      await expect(main).toBeVisible();
      await expect(context).toBeVisible();
      await expect(dock).toBeVisible();
    });

    test('mobile: activity rail should move to bottom', async ({ page }) => {
      await setMobileViewport(page);
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // Check that the layout has mobile class
      const deckLayout = page.locator(selectors.deckLayout);
      await expect(deckLayout).toHaveClass(/mobile/);
    });

    test('mobile: logo should be hidden', async ({ page }) => {
      await setMobileViewport(page);
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // Logo should not be visible on mobile (CSS hides it)
      const logoButton = page.locator('.logo-container');
      // The container gets display:none on mobile
      await expect(logoButton).toBeHidden();
    });

    test('tablet: layout should adapt gracefully', async ({ page }) => {
      await setTabletViewport(page);
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // Major components should still be visible
      const rail = page.locator(selectors.activityRail);
      const main = page.locator(selectors.mainContent);
      const dock = page.locator(selectors.dock);

      await expect(rail).toBeVisible();
      await expect(main).toBeVisible();
      await expect(dock).toBeVisible();
    });

    test('should handle viewport resize gracefully', async ({ page }) => {
      // Start with desktop
      await setDesktopViewport(page);
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // Verify desktop layout
      const deckLayout = page.locator(selectors.deckLayout);
      await expect(deckLayout).not.toHaveClass(/mobile/);

      // Resize to mobile
      await setMobileViewport(page);
      await page.waitForTimeout(500);

      // Should now have mobile class
      await expect(deckLayout).toHaveClass(/mobile/);

      // Resize back to desktop
      await setDesktopViewport(page);
      await page.waitForTimeout(500);

      // Should no longer have mobile class
      await expect(deckLayout).not.toHaveClass(/mobile/);
    });
  });

  test.describe('Main Content Area', () => {
    test('should display empty state for chat mode', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // Should show welcome message when no chats exist
      const welcomeText = page.getByText('Welcome to AI Hub');
      await expect(welcomeText).toBeVisible();
    });

    test('should display empty state for agents mode', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // Switch to agents mode
      const agentsButton = page.locator('button[aria-label="Agents"]');
      await agentsButton.click();
      await page.waitForTimeout(300);

      // Should show agents empty state
      const agentsText = page.getByText('Background Agents');
      await expect(agentsText).toBeVisible();
    });

    test('should display empty state for studio mode', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // Switch to studio mode
      const studioButton = page.locator('button[aria-label="Studio"]');
      await studioButton.click();
      await page.waitForTimeout(300);

      // Should show studio empty state
      const studioText = page.getByText('Creative Studio');
      await expect(studioText).toBeVisible();
    });

    test('should display empty state for files mode', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // Switch to files mode
      const filesButton = page.locator('button[aria-label="Files"]');
      await filesButton.click();
      await page.waitForTimeout(300);

      // Should show files empty state
      const filesText = page.getByText('File Browser');
      await expect(filesText).toBeVisible();
    });
  });

  test.describe('Accessibility', () => {
    test('should have proper ARIA labels on main regions', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // Check main regions have labels
      const deck = page.locator('[aria-label="AI Hub - The Deck"]');
      const nav = page.locator('[aria-label="Main navigation"]');
      const main = page.locator('[aria-label="Main content"]');
      const context = page.locator('[aria-label="Context panel"]');
      const dock = page.locator('[aria-label="Dock"]');

      await expect(deck).toBeVisible();
      await expect(nav).toBeVisible();
      await expect(main).toBeVisible();
      await expect(context).toBeVisible();
      await expect(dock).toBeVisible();
    });

    test('activity buttons should have correct aria-current state', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // Chat is default, so it should be current
      const chatButton = page.locator('button[aria-label="Chat"]');
      await expect(chatButton).toHaveAttribute('aria-current', 'page');

      // Others should not have aria-current
      const agentsButton = page.locator('button[aria-label="Agents"]');
      await expect(agentsButton).not.toHaveAttribute('aria-current', 'page');
    });

    test('context panel toggle should have aria-expanded', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      // Toggle button should indicate expansion state
      const collapseButton = page.locator('button[aria-label="Collapse panel"]');
      await expect(collapseButton).toHaveAttribute('aria-expanded', 'true');
    });

    test('quick action buttons should have aria-labels', async ({ page }) => {
      await page.goto('/');
      await page.waitForLoadState('networkidle');

      const newChat = page.locator('button[aria-label="New chat"]');
      const newAgent = page.locator('button[aria-label="New agent"]');
      const newCreate = page.locator('button[aria-label="New creation"]');

      await expect(newChat).toBeVisible();
      await expect(newAgent).toBeVisible();
      await expect(newCreate).toBeVisible();
    });
  });
});

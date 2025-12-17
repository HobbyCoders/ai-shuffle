/**
 * Test utilities for AI Hub E2E tests
 * Provides mock helpers, common selectors, and authentication helpers
 */
import { type Page, type BrowserContext } from '@playwright/test';

// =============================================================================
// Common Selectors
// =============================================================================

export const selectors = {
  // Activity Rail
  activityRail: '[aria-label="Main navigation"]',
  activityButton: (mode: string) => `button[aria-label="${mode}"]`,
  activeIndicator: '.active-indicator',
  settingsButton: '[aria-label="Settings"]',
  logoButton: '[aria-label="AI Hub Home"]',

  // Deck Layout
  deckLayout: '[aria-label="AI Hub - The Deck"]',
  mainContent: '[aria-label="Main content"]',

  // Context Panel
  contextPanel: '[aria-label="Context panel"]',
  contextToggle: '[aria-label="Expand panel"], [aria-label="Collapse panel"]',
  activeThreadsSection: '#threads-heading',
  runningAgentsSection: '#agents-heading',
  generationsSection: '#generations-heading',
  sessionInfoSection: '#session-heading',

  // Dock
  dock: '[aria-label="Dock"]',
  runningAgentsGroup: '[aria-label="Running agents"]',
  mediaGenerationsGroup: '[aria-label="Media generations"]',
  quickActionsGroup: '[aria-label="Quick actions"]',
  newChatButton: '[aria-label="New chat"]',
  newAgentButton: '[aria-label="New agent"]',
  newCreateButton: '[aria-label="New creation"]',

  // Chat
  chatInput: 'textarea[placeholder="Type a message..."]',
  sendButton: 'button:has-text("Send")',
  messageList: '.message-list',
  streamingIndicator: '.streaming-indicator',

  // Modals
  settingsModal: '[role="dialog"]',
  spotlightSearch: '[role="dialog"]',
  keyboardShortcutsModal: '[role="dialog"]',

  // Empty states
  emptyStateChat: 'text=Welcome to AI Hub',
  emptyStateAgents: 'text=Background Agents',
  emptyStateStudio: 'text=Creative Studio',
  emptyStateFiles: 'text=File Browser',

  // Loading states
  loadingSpinner: '.animate-pulse',
  loadingText: 'text=Loading...',
};

// =============================================================================
// Mock WebSocket Helper
// =============================================================================

export interface MockMessage {
  type: 'assistant' | 'user' | 'tool_use' | 'streaming_start' | 'streaming_end';
  content?: string;
  toolName?: string;
}

/**
 * Sets up mock WebSocket responses for testing
 * The actual app uses real WebSocket connections, so we intercept at the network level
 */
export async function setupMockWebSocket(page: Page): Promise<void> {
  // Note: Full WebSocket mocking requires more complex setup
  // For now, we focus on testing UI without backend
  await page.route('**/api/ws/**', route => {
    // Allow WebSocket upgrade but don't fully mock the connection
    route.continue();
  });
}

// =============================================================================
// Mock API Responses
// =============================================================================

export const mockResponses = {
  sessions: [
    {
      id: 'session-1',
      title: 'Test Chat Session',
      preview: 'Hello, how can I help?',
      updated_at: new Date().toISOString(),
      total_tokens_in: 100,
      total_tokens_out: 200,
      total_cost_usd: 0.01,
    },
    {
      id: 'session-2',
      title: 'Another Session',
      preview: 'Working on code...',
      updated_at: new Date(Date.now() - 3600000).toISOString(),
      total_tokens_in: 500,
      total_tokens_out: 1000,
      total_cost_usd: 0.05,
    },
  ],

  profiles: [
    { id: 'default', name: 'Default', description: 'Default profile' },
    { id: 'coding', name: 'Coding', description: 'Code assistance' },
  ],

  projects: [
    { id: 'project-1', name: 'AI Hub', path: '/workspace/ai-hub' },
  ],

  authCheck: {
    authenticated: true,
    username: 'testuser',
    is_admin: true,
    claude_authenticated: true,
  },

  agents: [
    {
      id: 'agent-1',
      name: 'Test Agent',
      status: 'running',
      progress: 50,
      branch: 'feature/test',
      startedAt: new Date(),
      currentTask: 'Running tests',
    },
  ],

  generations: [
    {
      id: 'gen-1',
      type: 'image',
      prompt: 'A beautiful sunset',
      status: 'completed',
      thumbnailUrl: '/placeholder.png',
    },
  ],
};

/**
 * Sets up mock API responses for all common endpoints
 */
export async function setupMockApi(page: Page): Promise<void> {
  // Auth check endpoint
  await page.route('**/api/auth/check', async route => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(mockResponses.authCheck),
    });
  });

  // Sessions endpoint
  await page.route('**/api/sessions**', async route => {
    if (route.request().method() === 'GET') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(mockResponses.sessions),
      });
    } else {
      await route.continue();
    }
  });

  // Profiles endpoint
  await page.route('**/api/profiles**', async route => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(mockResponses.profiles),
    });
  });

  // Projects endpoint
  await page.route('**/api/projects**', async route => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(mockResponses.projects),
    });
  });
}

// =============================================================================
// Authentication Helper
// =============================================================================

/**
 * Handles authentication flow in tests
 * Checks if logged in, and performs login or setup if needed
 */
export async function ensureAuthenticated(page: Page): Promise<void> {
  await page.goto('/');
  await page.waitForLoadState('networkidle');

  const currentUrl = page.url();

  if (currentUrl.includes('/setup')) {
    // First-time setup
    console.log('Performing first-time setup...');
    await page.fill('input[placeholder="admin"]', 'admin');
    await page.fill('input[placeholder="Enter password"]', 'testpass123');
    await page.fill('input[placeholder="Confirm password"]', 'testpass123');
    await page.click('button[type="submit"]');
    await page.waitForURL('**/');
    await page.waitForLoadState('networkidle');
  } else if (currentUrl.includes('/login')) {
    // Login required
    console.log('Logging in...');
    await page.fill('input[placeholder="Username"]', 'admin');
    await page.fill('input[placeholder="Password"]', 'testpass123');
    await page.click('button[type="submit"]');
    await page.waitForURL('**/');
    await page.waitForLoadState('networkidle');
  }
}

/**
 * Sets up authentication by mocking the auth check API
 */
export async function mockAuthentication(page: Page): Promise<void> {
  await page.route('**/api/auth/check', async route => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(mockResponses.authCheck),
    });
  });
}

// =============================================================================
// Viewport Helpers
// =============================================================================

export const viewports = {
  desktop: { width: 1280, height: 720 },
  tablet: { width: 768, height: 1024 },
  mobile: { width: 375, height: 667 },
  mobileLandscape: { width: 667, height: 375 },
};

/**
 * Sets the viewport to a mobile size
 */
export async function setMobileViewport(page: Page): Promise<void> {
  await page.setViewportSize(viewports.mobile);
}

/**
 * Sets the viewport to a desktop size
 */
export async function setDesktopViewport(page: Page): Promise<void> {
  await page.setViewportSize(viewports.desktop);
}

/**
 * Sets the viewport to a tablet size
 */
export async function setTabletViewport(page: Page): Promise<void> {
  await page.setViewportSize(viewports.tablet);
}

// =============================================================================
// Wait Helpers
// =============================================================================

/**
 * Waits for the deck layout to be fully loaded
 */
export async function waitForDeckLayout(page: Page): Promise<void> {
  await page.waitForSelector(selectors.deckLayout, { state: 'visible' });
  // Wait for any loading states to complete
  const loadingIndicator = page.locator(selectors.loadingText);
  if (await loadingIndicator.isVisible()) {
    await loadingIndicator.waitFor({ state: 'hidden', timeout: 10000 });
  }
}

/**
 * Waits for the activity rail to be rendered
 */
export async function waitForActivityRail(page: Page): Promise<void> {
  await page.waitForSelector(selectors.activityRail, { state: 'visible' });
}

/**
 * Waits for streaming to complete
 */
export async function waitForStreamingComplete(page: Page, timeout = 30000): Promise<void> {
  // Wait for streaming indicator to appear then disappear
  const indicator = page.locator(selectors.streamingIndicator);
  try {
    await indicator.waitFor({ state: 'visible', timeout: 5000 });
    await indicator.waitFor({ state: 'hidden', timeout });
  } catch {
    // Streaming indicator might not appear if response is instant
  }
}

// =============================================================================
// Interaction Helpers
// =============================================================================

/**
 * Switches to a different activity mode
 */
export async function switchToMode(page: Page, mode: 'Chat' | 'Agents' | 'Studio' | 'Files'): Promise<void> {
  const button = page.locator(selectors.activityButton(mode));
  await button.click();
  await page.waitForTimeout(300); // Wait for transition
}

/**
 * Toggles the context panel
 */
export async function toggleContextPanel(page: Page): Promise<void> {
  const toggle = page.locator(selectors.contextToggle);
  await toggle.click();
  await page.waitForTimeout(300); // Wait for transition
}

/**
 * Opens spotlight search with keyboard shortcut
 */
export async function openSpotlight(page: Page): Promise<void> {
  const isMac = process.platform === 'darwin';
  if (isMac) {
    await page.keyboard.press('Meta+k');
  } else {
    await page.keyboard.press('Control+k');
  }
  await page.waitForTimeout(300);
}

/**
 * Creates a new chat
 */
export async function createNewChat(page: Page): Promise<void> {
  const newChatButton = page.locator(selectors.newChatButton);
  await newChatButton.click();
  await page.waitForTimeout(300);
}

/**
 * Sends a chat message
 */
export async function sendMessage(page: Page, message: string): Promise<void> {
  const input = page.locator(selectors.chatInput);
  await input.fill(message);

  const sendButton = page.locator(selectors.sendButton);
  await sendButton.click();
}

// =============================================================================
// Screenshot Helpers
// =============================================================================

/**
 * Takes a screenshot with a descriptive name
 */
export async function takeScreenshot(page: Page, name: string): Promise<void> {
  await page.screenshot({
    path: `tests/screenshots/${name}.png`,
    fullPage: true
  });
}

// =============================================================================
// Test Data Helpers
// =============================================================================

/**
 * Generates a unique test session ID
 */
export function generateTestId(): string {
  return `test-${Date.now()}-${Math.random().toString(36).substring(7)}`;
}

/**
 * Creates mock message data for testing
 */
export function createMockMessages(count: number): MockMessage[] {
  const messages: MockMessage[] = [];
  for (let i = 0; i < count; i++) {
    messages.push({
      type: i % 2 === 0 ? 'user' : 'assistant',
      content: `Test message ${i + 1}`,
    });
  }
  return messages;
}

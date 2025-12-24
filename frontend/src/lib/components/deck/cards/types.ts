/**
 * Card types for The Deck - Draggable window system
 */

export type CardType = 'chat' | 'agent' | 'studio' | 'terminal' | 'settings' | 'profile' | 'subagent' | 'project';

export interface DeckCard {
  id: string;
  type: CardType;
  title: string;
  x: number;
  y: number;
  width: number;
  height: number;
  zIndex: number;
  minimized: boolean;
  maximized: boolean;
  focused: boolean;
  snappedTo?: 'left' | 'right' | 'top' | 'bottom' | 'topleft' | 'topright' | 'bottomleft' | 'bottomright';
  restoreGeometry?: { x: number; y: number; width: number; height: number };
  data?: any;
  createdAt?: Date;
  updatedAt?: Date;
}

export const DEFAULT_CARD_SIZES: Record<CardType, { width: number; height: number }> = {
  chat: { width: 480, height: 640 },
  agent: { width: 400, height: 500 },
  studio: { width: 520, height: 580 },
  terminal: { width: 600, height: 400 },
  settings: { width: 700, height: 650 },
  profile: { width: 600, height: 620 },
  subagent: { width: 550, height: 580 },
  project: { width: 480, height: 520 },
};

export const MIN_CARD_SIZES: Record<CardType, { width: number; height: number }> = {
  chat: { width: 360, height: 400 },
  agent: { width: 320, height: 300 },
  studio: { width: 400, height: 450 },
  terminal: { width: 400, height: 200 },
  settings: { width: 500, height: 400 },
  profile: { width: 450, height: 400 },
  subagent: { width: 400, height: 350 },
  project: { width: 360, height: 300 },
};

export const SNAP_THRESHOLD = 20;
export const SNAP_GRID = 10;
export const CARD_SNAP_THRESHOLD = 12; // Threshold for card-to-card snapping

/**
 * Workspace padding to keep cards visible and accessible
 * Left padding accounts for floating activity pill (56px wide + 16px from edge + 12px gap)
 */
export const WORKSPACE_PADDING = {
  top: 10,       // Small top padding
  bottom: 10,    // Small bottom padding
  left: 84,      // Space for floating pill bar (56px + 16px + 12px gap)
  right: 10,     // Small right padding
  minVisible: 100 // Minimum visible area of card (header must stay visible)
};

/**
 * Represents a snap guide line for card-to-card alignment
 */
export interface SnapGuide {
  type: 'horizontal' | 'vertical';
  position: number; // x for vertical, y for horizontal
  start: number;    // start of the line
  end: number;      // end of the line
}

/**
 * Result of checking for card-to-card snap alignment
 */
export interface SnapResult {
  x: number;
  y: number;
  guides: SnapGuide[];
}

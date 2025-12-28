/**
 * Card types for The Deck - Draggable window system
 */

export type CardType =
  | 'chat'
  | 'terminal'
  | 'settings'
  | 'profile'
  | 'subagent'
  | 'project'
  | 'user-settings'
  | 'image-studio'
  | 'model-studio'
  | 'audio-studio'
  | 'file-browser';

export interface DeckCard {
  id: string;
  type: CardType;
  title: string;
  x: number;
  y: number;
  width: number;
  height: number;
  zIndex: number;
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
  terminal: { width: 600, height: 400 },
  settings: { width: 700, height: 650 },
  profile: { width: 600, height: 620 },
  subagent: { width: 550, height: 580 },
  project: { width: 480, height: 520 },
  'user-settings': { width: 650, height: 550 },
  'image-studio': { width: 800, height: 600 },
  'model-studio': { width: 800, height: 600 },
  'audio-studio': { width: 700, height: 500 },
  'file-browser': { width: 600, height: 500 },
};

export const MIN_CARD_SIZES: Record<CardType, { width: number; height: number }> = {
  chat: { width: 360, height: 400 },
  terminal: { width: 400, height: 200 },
  settings: { width: 500, height: 400 },
  profile: { width: 450, height: 400 },
  subagent: { width: 400, height: 350 },
  project: { width: 360, height: 300 },
  'user-settings': { width: 500, height: 400 },
  'image-studio': { width: 500, height: 400 },
  'model-studio': { width: 500, height: 400 },
  'audio-studio': { width: 450, height: 350 },
  'file-browser': { width: 400, height: 350 },
};

/**
 * Workspace padding to keep cards visible and accessible
 * Now with floating dealer button in bottom-left corner
 */
export const WORKSPACE_PADDING = {
  top: 10,       // Small top padding
  bottom: 90,    // Space for floating dealer button (56px + 24px + some margin)
  left: 10,      // Small left padding (no more sidebar)
  right: 10,     // Small right padding
  minVisible: 100 // Minimum visible area of card (header must stay visible)
};

/**
 * Card types for The Deck - Draggable window system
 */

export type CardType = 'chat' | 'agent' | 'canvas' | 'terminal';

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
  canvas: { width: 600, height: 500 },
  terminal: { width: 600, height: 400 },
};

export const MIN_CARD_SIZES: Record<CardType, { width: number; height: number }> = {
  chat: { width: 360, height: 400 },
  agent: { width: 320, height: 300 },
  canvas: { width: 400, height: 300 },
  terminal: { width: 400, height: 200 },
};

export const SNAP_THRESHOLD = 20;
export const SNAP_GRID = 10;

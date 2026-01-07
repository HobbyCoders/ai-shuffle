/**
 * Swipe gesture action for Svelte
 * Usage: <div use:swipe on:swipeleft={handleLeft} on:swiperight={handleRight}>
 */

export interface SwipeOptions {
  /** Minimum distance (in pixels) to trigger a swipe. Default: 50 */
  threshold?: number;
  /** Maximum time (in ms) for the swipe gesture. Default: 300 */
  timeout?: number;
  /** Minimum velocity (pixels/ms) to trigger a swipe. Default: 0.3 */
  velocity?: number;
  /** Whether to prevent default on touch events. Default: false */
  preventDefault?: boolean;
  /** Only trigger if swipe starts from edge. Set to number of pixels from edge. Default: 0 (disabled) */
  edgeOnly?: number;
}

export interface SwipeEvent {
  direction: 'left' | 'right' | 'up' | 'down';
  deltaX: number;
  deltaY: number;
  velocity: number;
}

const defaultOptions: Required<SwipeOptions> = {
  threshold: 50,
  timeout: 300,
  velocity: 0.3,
  preventDefault: false,
  edgeOnly: 0
};

export function swipe(node: HTMLElement, options: SwipeOptions = {}) {
  const config = { ...defaultOptions, ...options };

  let startX = 0;
  let startY = 0;
  let startTime = 0;
  let isTracking = false;

  function handleTouchStart(event: TouchEvent) {
    const touch = event.touches[0];

    // Check edge-only constraint
    if (config.edgeOnly > 0) {
      const isLeftEdge = touch.clientX < config.edgeOnly;
      const isRightEdge = touch.clientX > window.innerWidth - config.edgeOnly;

      if (!isLeftEdge && !isRightEdge) {
        return;
      }
    }

    startX = touch.clientX;
    startY = touch.clientY;
    startTime = Date.now();
    isTracking = true;
  }

  function handleTouchMove(event: TouchEvent) {
    if (!isTracking) return;

    if (config.preventDefault) {
      event.preventDefault();
    }
  }

  function handleTouchEnd(event: TouchEvent) {
    if (!isTracking) return;

    isTracking = false;

    const touch = event.changedTouches[0];
    const deltaX = touch.clientX - startX;
    const deltaY = touch.clientY - startY;
    const deltaTime = Date.now() - startTime;

    // Check timeout
    if (deltaTime > config.timeout) {
      return;
    }

    const absX = Math.abs(deltaX);
    const absY = Math.abs(deltaY);

    // Calculate velocity
    const velocity = Math.max(absX, absY) / deltaTime;

    // Check velocity threshold
    if (velocity < config.velocity) {
      return;
    }

    // Determine direction (horizontal takes precedence if delta is larger)
    let direction: SwipeEvent['direction'];

    if (absX > absY) {
      // Horizontal swipe
      if (absX < config.threshold) return;
      direction = deltaX > 0 ? 'right' : 'left';
    } else {
      // Vertical swipe
      if (absY < config.threshold) return;
      direction = deltaY > 0 ? 'down' : 'up';
    }

    const detail: SwipeEvent = {
      direction,
      deltaX,
      deltaY,
      velocity
    };

    // Dispatch specific direction event
    node.dispatchEvent(new CustomEvent(`swipe${direction}`, { detail }));

    // Dispatch generic swipe event
    node.dispatchEvent(new CustomEvent('swipe', { detail }));
  }

  node.addEventListener('touchstart', handleTouchStart, { passive: true });
  node.addEventListener('touchmove', handleTouchMove, { passive: !config.preventDefault });
  node.addEventListener('touchend', handleTouchEnd, { passive: true });

  return {
    update(newOptions: SwipeOptions) {
      Object.assign(config, { ...defaultOptions, ...newOptions });
    },
    destroy() {
      node.removeEventListener('touchstart', handleTouchStart);
      node.removeEventListener('touchmove', handleTouchMove);
      node.removeEventListener('touchend', handleTouchEnd);
    }
  };
}

/**
 * Hook for programmatic swipe detection
 */
export function createSwipeHandler(options: SwipeOptions = {}) {
  const config = { ...defaultOptions, ...options };

  let startX = 0;
  let startY = 0;
  let startTime = 0;

  return {
    onTouchStart(event: TouchEvent) {
      const touch = event.touches[0];
      startX = touch.clientX;
      startY = touch.clientY;
      startTime = Date.now();
    },

    onTouchEnd(event: TouchEvent): SwipeEvent | null {
      const touch = event.changedTouches[0];
      const deltaX = touch.clientX - startX;
      const deltaY = touch.clientY - startY;
      const deltaTime = Date.now() - startTime;

      if (deltaTime > config.timeout) {
        return null;
      }

      const absX = Math.abs(deltaX);
      const absY = Math.abs(deltaY);
      const velocity = Math.max(absX, absY) / deltaTime;

      if (velocity < config.velocity) {
        return null;
      }

      let direction: SwipeEvent['direction'];

      if (absX > absY) {
        if (absX < config.threshold) return null;
        direction = deltaX > 0 ? 'right' : 'left';
      } else {
        if (absY < config.threshold) return null;
        direction = deltaY > 0 ? 'down' : 'up';
      }

      return { direction, deltaX, deltaY, velocity };
    }
  };
}

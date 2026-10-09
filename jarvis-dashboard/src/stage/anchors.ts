// Where each highlight is on screen, so the Stage can draw a beam from the orb to it.
// Panels register a getter for the highlights they show; the beam calls the active one
// every frame. Getters return viewport coordinates, or null when the mark isn't visible.

export interface AnchorRect { x: number; y: number; w: number; h: number }

const anchors = new Map<string, () => AnchorRect | null>();

export function setAnchor(highlightId: string, getRect: () => AnchorRect | null) {
  anchors.set(highlightId, getRect);
  return () => { if (anchors.get(highlightId) === getRect) anchors.delete(highlightId); };
}

export function getAnchor(highlightId: string | null): AnchorRect | null {
  if (!highlightId) return null;
  try {
    return anchors.get(highlightId)?.() ?? null;
  } catch {
    return null;
  }
}

export function toAnchor(r: DOMRect | null | undefined): AnchorRect | null {
  return r && r.width + r.height > 0 ? { x: r.left, y: r.top, w: r.width, h: r.height } : null;
}

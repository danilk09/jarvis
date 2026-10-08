import { useEffect } from 'react';
import { jarvisHighlight, jarvisHighlightRect } from '../pageScripts';
import { setAnchor, toAnchor } from '../anchors';
import type { Highlight } from '../StageContext';

/**
 * Mark a quote inside a panel the dashboard renders itself (notes, summaries,
 * reader views). Put `data-reader={panelId}` on the scrollable, position:relative
 * element that holds the text. `contentKey` re-applies the mark when the text changes.
 */
export function useTextHighlight(panelId: string, highlight: Highlight | null, contentKey: string) {
  const quote = highlight?.target.quote ?? '';
  const hlId = highlight?.id ?? '';
  const label = highlight?.label ?? '';
  useEffect(() => {
    const selector = `[data-reader="${panelId}"]`;
    // Wait a frame so freshly rendered markdown is in the DOM
    const raf = requestAnimationFrame(() => jarvisHighlight(quote, label, panelId, selector));
    if (!quote || !hlId) return () => cancelAnimationFrame(raf);
    const release = setAnchor(hlId, () => {
      const r = jarvisHighlightRect(panelId);
      return r ? toAnchor(new DOMRect(r.x, r.y, r.w, r.h)) : null;
    });
    return () => { cancelAnimationFrame(raf); release(); };
  }, [panelId, quote, hlId, label, contentKey]);
}

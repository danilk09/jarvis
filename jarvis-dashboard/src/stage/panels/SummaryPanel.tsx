import { useEffect, useRef } from 'react';
import { stageApi } from '../StageContext';
import { useTextHighlight } from './useTextHighlight';
import type { PanelProps } from './types';

interface Point { text: string; quote: string }

// Key points of an article. Each point is linked to the sentence it came from:
// clicking it (or Jarvis reading it out) highlights that sentence in the live page.

export default function SummaryPanel({ panel, highlight, highlights = [] }: PanelProps) {
  const { overview = '', points = [], source } = panel.data as { overview: string; points: Point[]; source: string };
  const listRef = useRef<HTMLDivElement>(null);
  const sourceMark = highlights.find(h => h.panel === source);
  const activeIdx = sourceMark
    ? points.findIndex(p => p.quote && p.quote === sourceMark.target.quote)
    : (panel.data.active ?? -1);

  useTextHighlight(panel.id, highlight, `${points.length}`);

  // Keep the point being discussed in view
  useEffect(() => {
    const box = listRef.current;
    const el = box?.querySelector<HTMLElement>(`[data-point="${activeIdx}"]`);
    if (box && el) box.scrollTo({ top: el.offsetTop - box.clientHeight / 3, behavior: 'smooth' });
  }, [activeIdx]);

  const show = (p: Point, i: number) => {
    if (p.quote && source) stageApi.post('highlight', { panel: source, target: { quote: p.quote }, label: `Point ${i + 1}` });
  };

  return (
    <div className="summaryPanel" ref={listRef} data-reader={panel.id}>
      {overview && <p className="summaryOverview">{overview}</p>}
      <ol className="summaryPoints">
        {points.map((p, i) => (
          <li key={i} data-point={i}>
            <button
              className={`summaryPoint ${i === activeIdx ? 'summaryPointActive' : ''} ${p.quote ? '' : 'summaryPointNoQuote'}`}
              onClick={() => show(p, i)}
              title={p.quote ? `Show in the article: “${p.quote}”` : 'No matching sentence found in the page'}
            >
              <span className="summaryNum">{i + 1}</span>
              <span className="summaryText">{p.text}</span>
              {p.quote && <span className="summaryJump">↳</span>}
            </button>
          </li>
        ))}
      </ol>
    </div>
  );
}

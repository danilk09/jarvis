import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { useTextHighlight } from './useTextHighlight';
import type { PanelProps } from './types';

// A card of Markdown Jarvis wrote for the screen: lists, steps, tables, comparisons.

export default function NotePanel({ panel, highlight }: PanelProps) {
  const md: string = panel.data.markdown ?? '';
  useTextHighlight(panel.id, highlight, `${md.length}`);
  return (
    <div className="notePanel markdownBody" data-reader={panel.id}>
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{md}</ReactMarkdown>
    </div>
  );
}

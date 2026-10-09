import { useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { stageApi } from '../StageContext';
import { useTextHighlight } from './useTextHighlight';
import type { PanelProps } from './types';

// A card of Markdown Jarvis wrote for the screen: lists, steps, tables, comparisons.
// Double-click (or Edit) to change it yourself; edits save as you type.

export default function NotePanel({ panel, highlight }: PanelProps) {
  const md: string = panel.data.markdown ?? '';
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(md);
  const saveTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const textRef = useRef<HTMLTextAreaElement>(null);
  useTextHighlight(panel.id, highlight, `${md.length}${editing}`);

  const editingRef = useRef(editing);
  editingRef.current = editing;

  // Take Jarvis's version when it changes, unless you're typing
  useEffect(() => { if (!editingRef.current) setDraft(md); }, [md]);

  useEffect(() => {
    if (editing) textRef.current?.focus();
  }, [editing]);

  const save = (text: string) => {
    clearTimeout(saveTimer.current);
    if (text !== md) stageApi.post(`note/${panel.id}`, { markdown: text });
  };

  const onChange = (text: string) => {
    setDraft(text);
    clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(() => save(text), 600);
  };

  const finish = () => {
    save(draft);
    setEditing(false);
  };

  useEffect(() => () => clearTimeout(saveTimer.current), []);

  return (
    <div className="noteWrap">
      <button
        className={`noteEditBtn ${editing ? 'noteEditBtnOn' : ''}`}
        onMouseDown={e => e.preventDefault()}   // keep the textarea focused so Done isn't a blur + reopen
        onClick={() => (editing ? finish() : setEditing(true))}>
        {editing ? 'Done' : '✎ Edit'}
      </button>
      {editing ? (
        <textarea
          ref={textRef}
          className="noteEditor"
          value={draft}
          spellCheck={false}
          onChange={e => onChange(e.target.value)}
          onBlur={finish}
          onKeyDown={e => { if (e.key === 'Escape') finish(); }}
        />
      ) : (
        <div className="notePanel markdownBody" data-reader={panel.id} onDoubleClick={() => setEditing(true)}>
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{draft}</ReactMarkdown>
        </div>
      )}
    </div>
  );
}

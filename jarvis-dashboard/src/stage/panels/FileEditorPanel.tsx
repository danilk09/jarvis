import { useCallback, useEffect, useRef, useState } from 'react';
import Editor, { DiffEditor, BeforeMount, OnMount } from '@monaco-editor/react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { stageApi } from '../StageContext';
import { setAnchor } from '../anchors';
import type { PanelProps } from './types';

// A file Jarvis wrote, open in Monaco (VS Code's editor). Edits save automatically.
// When Jarvis proposes changes ("edit_file"), they show as a side-by-side diff to
// accept or reject — by button or by voice. Jarvis can also point at lines.

const defineTheme: BeforeMount = monaco => {
  monaco.editor.defineTheme('jarvis', {
    base: 'vs-dark',
    inherit: true,
    rules: [
      { token: 'comment', foreground: '4f7a63', fontStyle: 'italic' },
      { token: 'keyword', foreground: '3dff8f' },
      { token: 'string', foreground: 'a8ffd0' },
      { token: 'number', foreground: '7fd4ff' },
    ],
    colors: {
      'editor.background': '#030b07',
      'editor.foreground': '#d4f4e2',
      'editor.lineHighlightBackground': '#0a1a12',
      'editorLineNumber.foreground': '#2c5240',
      'editorLineNumber.activeForeground': '#3dff8f',
      'editorCursor.foreground': '#3dff8f',
      'editor.selectionBackground': '#1a664066',
      'editorIndentGuide.background1': '#0f2419',
      'diffEditor.insertedTextBackground': '#3dff8f22',
      'diffEditor.removedTextBackground': '#ff5f5f22',
      'scrollbarSlider.background': '#1a664055',
    },
  });
};

const OPTIONS = {
  minimap: { enabled: false },
  fontSize: 13,
  fontFamily: "'Cascadia Code', Consolas, monospace",
  scrollBeyondLastLine: false,
  wordWrap: 'on' as const,
  automaticLayout: true,
  smoothScrolling: true,
  padding: { top: 8 },
};

type Status = 'saved' | 'saving' | 'unsaved' | 'error';

export default function FileEditorPanel({ panel, highlight }: PanelProps) {
  const { content = '', version = 0, proposal = null, proposal_note = '', language = 'plaintext' } = panel.data;
  const editorRef = useRef<any>(null);
  const monacoRef = useRef<any>(null);
  const decorations = useRef<string[]>([]);
  const valueRef = useRef<string>(content);
  const localVersion = useRef<number>(version);
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [ready, setReady] = useState(false);
  const [status, setStatus] = useState<Status>('saved');
  const [preview, setPreview] = useState(false);
  const isMarkdown = language === 'markdown';

  const save = useCallback(async () => {
    if (saveTimer.current) { clearTimeout(saveTimer.current); saveTimer.current = null; }
    setStatus('saving');
    try {
      const res = await fetch(`/api/stage/file/${panel.id}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: valueRef.current }),
      });
      setStatus(res.ok ? 'saved' : 'error');
    } catch {
      setStatus('error');
    }
  }, [panel.id]);

  // Jarvis changed the file (or a proposal was accepted): load the new text
  useEffect(() => {
    if (version === localVersion.current) return;
    localVersion.current = version;
    valueRef.current = content;
    editorRef.current?.setValue(content);
    setStatus('saved');
  }, [version, content]);

  const onMount: OnMount = (editor, monaco) => {
    editorRef.current = editor;
    monacoRef.current = monaco;
    // Monaco mounts asynchronously; text may have changed since (e.g. a proposal was accepted)
    if (editor.getValue() !== valueRef.current) editor.setValue(valueRef.current);
    editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => { save(); });
    setReady(true);
  };

  const onChange = (v?: string) => {
    valueRef.current = v ?? '';
    setStatus('unsaved');
    if (saveTimer.current) clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(save, 800);
  };

  // Lines Jarvis is pointing at
  const lines = highlight?.target.lines;
  const first = lines?.[0] ?? 0, last = lines?.[1] ?? 0;
  const hlId = highlight?.id ?? '';
  const label = highlight?.label ?? '';
  useEffect(() => {
    const ed = editorRef.current, monaco = monacoRef.current;
    if (!ready || !ed || !monaco || proposal !== null) return;
    if (!first) {
      decorations.current = ed.deltaDecorations(decorations.current, []);
      return;
    }
    const model = ed.getModel();
    const a = Math.min(first, model.getLineCount()), b = Math.min(Math.max(last, a), model.getLineCount());
    decorations.current = ed.deltaDecorations(decorations.current, [
      { range: new monaco.Range(a, 1, b, 1),
        options: { isWholeLine: true, className: 'jarvisLineHL', linesDecorationsClassName: 'jarvisLineGutter' } },
      ...(label ? [{ range: new monaco.Range(a, model.getLineMaxColumn(a), a, model.getLineMaxColumn(a)),
                     options: { after: { content: `  ◀ ${label}`, inlineClassName: 'jarvisLineLabel' } } }] : []),
    ]);
    ed.revealLinesInCenter(a, b, 0);
    return setAnchor(hlId, () => {
      const dom = ed.getDomNode()?.getBoundingClientRect();
      if (!dom) return null;
      const top = ed.getTopForLineNumber(a) - ed.getScrollTop();
      const height = ed.getTopForLineNumber(b + 1) - ed.getTopForLineNumber(a) || 19;
      if (top + height < 0 || top > dom.height) return null;
      return { x: dom.left + 40, y: dom.top + Math.max(0, top), w: dom.width - 50, h: height };
    });
  }, [ready, first, last, hlId, label, proposal]);

  useEffect(() => () => { if (saveTimer.current) clearTimeout(saveTimer.current); }, []);

  // The editor unmounts for the preview and the diff; drop the dead instance
  useEffect(() => {
    if (preview || proposal !== null) {
      editorRef.current = null;
      setReady(false);
    }
  }, [preview, proposal]);

  if (proposal !== null) {
    const decide = (decision: 'accept' | 'reject') => stageApi.post(`file/${panel.id}`, { decision });
    return (
      <div className="filePanel">
        <div className="fileBar fileBarProposal">
          <span className="fileProposalLabel">Jarvis proposed changes</span>
          {proposal_note && <span className="fileProposalNote" title={proposal_note}>— {proposal_note}</span>}
          <span className="fileBarSpacer" />
          <button className="fileBtn fileBtnReject" onClick={() => decide('reject')}>Reject</button>
          <button className="fileBtn fileBtnAccept" onClick={() => decide('accept')}>Accept</button>
        </div>
        <div className="fileEditor">
          <DiffEditor original={content} modified={proposal} language={language} theme="jarvis"
                      beforeMount={defineTheme} options={{ ...OPTIONS, readOnly: true, renderSideBySide: true }} />
        </div>
      </div>
    );
  }

  return (
    <div className="filePanel">
      <div className="fileBar">
        <span className="fileLang">{language}</span>
        {isMarkdown && (
          <div className="fileToggle">
            <button className={!preview ? 'on' : ''} onClick={() => setPreview(false)}>Edit</button>
            <button className={preview ? 'on' : ''} onClick={() => setPreview(true)}>Preview</button>
          </div>
        )}
        <span className="fileBarSpacer" />
        <span className={`fileStatus fileStatus-${status}`}>
          {status === 'saved' ? 'Saved' : status === 'saving' ? 'Saving…' : status === 'unsaved' ? 'Unsaved' : 'Save failed'}
        </span>
      </div>
      {preview
        ? <div className="filePreview markdownBody"><ReactMarkdown remarkPlugins={[remarkGfm]}>{valueRef.current}</ReactMarkdown></div>
        : (
          <div className="fileEditor">
            <Editor defaultValue={valueRef.current} language={language} theme="jarvis" beforeMount={defineTheme}
                    onMount={onMount} onChange={onChange} options={OPTIONS}
                    loading={<div className="panelEmpty">Loading editor…</div>} />
          </div>
        )}
    </div>
  );
}

import { useEffect, useRef } from 'react';
import type { PanelProps } from './types';

// Live view of Claude Code working on a project (core/coding.py runs it in the
// background): what it edits, the commands it runs, subagents, its own notes, and
// the summary Jarvis reads out at the end. Click a changed file to open it in the editor.

interface CodeEvent {
  kind: 'request' | 'text' | 'edit' | 'command' | 'agent' | 'read' | 'todo' | 'tool' | 'error' | 'log';
  text: string;
  time: number;
  path?: string;
  op?: string;
  note?: string;
  agent?: string;
  settings?: { mode: string; effort: string; model: string };
}

const STATUS: Record<string, string> = {
  running: 'WORKING', done: 'DONE', planned: 'PLAN READY', error: 'ERROR',
  stopped: 'STOPPED', interrupted: 'INTERRUPTED', idle: 'IDLE',
};

const ICON: Record<string, string> = {
  text: '›', edit: '✎', command: '$', agent: '⑂', read: '◦', todo: '☰', tool: '•', error: '!', log: '·',
};

function post(path: string, body: object = {}) {
  return fetch(`/api/coding/${path}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  }).catch(() => undefined);
}

function elapsed(ms?: number) {
  if (!ms) return '';
  const s = Math.round(ms / 1000);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

export default function CodePanel({ panel }: PanelProps) {
  const d = panel.data;
  const events: CodeEvent[] = d.events ?? [];
  const files: string[] = d.files ?? [];
  const denials: { tool: string; command: string }[] = d.denials ?? [];
  const status: string = d.status ?? 'idle';
  const logRef = useRef<HTMLDivElement>(null);
  const stick = useRef(true);       // follow new output unless you've scrolled up

  useEffect(() => {
    const el = logRef.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [events.length, status]);

  const openFile = (path?: string) => {
    if (!path) return;
    const full = /^[a-zA-Z]:[\\/]|^\//.test(path) ? path : `${d.path}\\${path}`;
    post('file', { path: full });
  };

  // Show only this run's activity in full; earlier runs collapse to their request line
  const lastRequest = events.map(e => e.kind).lastIndexOf('request');

  return (
    <div className="codePanel">
      <div className="codeBar">
        <span className={`codeStatus codeStatus-${status}`}>
          {status === 'running' && <span className="codeSpinner" />}{STATUS[status] ?? status.toUpperCase()}
        </span>
        {d.settings && (
          <span className="codeChips">
            {d.settings.mode !== 'acceptEdits' && <span className="codeChip">{d.settings.mode}</span>}
            {d.settings.effort !== 'default' && <span className="codeChip">effort {d.settings.effort}</span>}
            {d.settings.model !== 'default' && <span className="codeChip">{d.settings.model}</span>}
          </span>
        )}
        <span className="codePath" title={d.path}>{d.path}</span>
        {status !== 'running' && d.duration && (
          <span className="codeMeta">{elapsed(d.duration)}{d.cost ? ` · $${d.cost.toFixed(2)}` : ''}</span>
        )}
        {status === 'running'
          ? <button className="fileBtn fileBtnReject" onClick={() => post('stop')}>Stop</button>
          : (
            <>
              <button className="fileBtn" title="Open the project in VS Code" onClick={() => post('vscode')}>VS Code</button>
              <button className="fileBtn" title="Continue this conversation in an interactive Claude Code window"
                      onClick={() => post('terminal')}>Terminal</button>
            </>
          )}
      </div>

      <div className="codeLog" ref={logRef}
           onScroll={e => { const el = e.currentTarget; stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40; }}>
        {events.length === 0 && <div className="panelEmpty">Waiting for Claude Code…</div>}
        {events.map((e, i) => {
          if (e.kind === 'request') {
            return <div key={i} className="codeRequest"><span>▸</span>{e.text}</div>;
          }
          if (i < lastRequest) return null;
          if (e.kind === 'text' && d.summary && status !== 'running' && e.text.trim() === String(d.summary).trim()) return null;
          if (e.kind === 'read') return <div key={i} className="codeEv codeEv-read">{ICON.read} {e.text}</div>;
          return (
            <div key={i} className={`codeEv codeEv-${e.kind}`}>
              <span className="codeIcon">{ICON[e.kind] ?? '•'}</span>
              {e.kind === 'edit'
                ? <button className="codeFile" onClick={() => openFile(e.path)} title="Open in the editor">
                    <span className="codeOp">{e.op}</span>{e.text}
                  </button>
                : e.kind === 'agent'
                  ? <span>Subagent{e.agent ? ` (${e.agent})` : ''}: {e.text}</span>
                  : <span className="codeText">{e.text}{e.note && e.kind === 'command' ? <em> — {e.note}</em> : null}</span>}
            </div>
          );
        })}
        {d.summary && status !== 'running' && (
          <div className={`codeSummary codeSummary-${status}`}>{d.summary}</div>
        )}
      </div>

      {denials.length > 0 && status !== 'running' && (
        <div className="codeDenied">
          <div className="codeDeniedText">
            Needs your permission to run:
            {denials.map((x, i) => <code key={i}>{x.command}</code>)}
          </div>
          <button className="fileBtn fileBtnAccept" onClick={() => post('approve')}>Allow &amp; continue</button>
        </div>
      )}

      {files.length > 0 && (
        <div className="codeFiles">
          <span className="codeFilesLabel">FILES</span>
          {files.slice(-24).map(f => (
            <button key={f} className="codeFileChip" title={f} onClick={() => openFile(f)}>{f.split(/[\\/]/).pop()}</button>
          ))}
        </div>
      )}
    </div>
  );
}

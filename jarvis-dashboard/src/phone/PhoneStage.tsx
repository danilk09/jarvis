import { useEffect, useMemo, useState } from 'react';
import { createPortal } from 'react-dom';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { useStage, stageApi, Highlight, Panel } from '../stage/StageContext';
import WebPanel from '../stage/panels/WebPanel';
import SummaryPanel from '../stage/panels/SummaryPanel';
import NotePanel from '../stage/panels/NotePanel';
import ImagePanel from '../stage/panels/ImagePanel';
import FileEditorPanel from '../stage/panels/FileEditorPanel';
import MapPanel from '../stage/panels/MapPanel';
import PlacesPanel from '../stage/panels/PlacesPanel';
import CodePanel from '../stage/panels/CodePanel';
import type { PanelProps } from '../stage/panels/types';

// The Stage on the phone. A list of short preview cards that never scroll on their own (so
// a swipe always scrolls the list), each showing the gist: a note's start, Claude Code's
// status and summary with its Allow/Stop buttons, the places, a thumbnail. Tapping a card
// opens the real panel full screen, where it can scroll, zoom and pan; ✕ (top) or the
// phone's back gesture closes it, ‹ Newer / Older › (bottom) step through the panels.

const KIND_NAME: Record<string, string> = {
  page: 'PAGE', summary: 'KEY POINTS', file: 'FILE', image: 'IMAGE', note: 'NOTE', map: 'GLOBE',
  places: 'PLACES', code: 'CLAUDE CODE',
};

const CODE_STATUS: Record<string, string> = {
  running: 'WORKING', done: 'DONE', planned: 'PLAN READY', error: 'ERROR',
  stopped: 'STOPPED', interrupted: 'INTERRUPTED', idle: 'IDLE',
};

function coding(path: string, project: string) {
  return fetch(`/api/coding/${path}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ project }),
  }).catch(() => undefined);
}

function PanelContent(props: PanelProps) {
  switch (props.panel.kind) {
    case 'page':    return <WebPanel {...props} />;
    case 'summary': return <SummaryPanel {...props} />;
    case 'note':    return <NotePanel {...props} />;
    case 'image':   return <ImagePanel {...props} />;
    case 'file':    return <FileEditorPanel {...props} />;
    case 'map':     return <MapPanel {...props} />;
    case 'places':  return <PlacesPanel {...props} />;
    case 'code':    return <CodePanel {...props} />;
    default:        return <div className="panelEmpty">Unknown panel</div>;
  }
}

/** The card's gist: plain, short, no scrolling, no maps or editors (those open full screen). */
function Preview({ panel }: { panel: Panel }) {
  const d = panel.data || {};
  switch (panel.kind) {
    case 'note':
      return <div className="phPrevText markdownBody phFade"><ReactMarkdown remarkPlugins={[remarkGfm]}>{d.markdown || ''}</ReactMarkdown></div>;
    case 'summary':
      return (
        <div className="phPrevText phFade">
          {d.overview && <p>{d.overview}</p>}
          <ul>{(d.points || []).slice(0, 4).map((p: any, i: number) => <li key={i}>{p.text}</li>)}</ul>
        </div>
      );
    case 'code': {
      const events: any[] = d.events || [];
      const last = [...events].reverse().find(e => ['text', 'edit', 'command'].includes(e.kind));
      const status: string = d.status || 'idle';
      const denials: any[] = d.denials || [];
      return (
        <div className="phPrevCode">
          <div className="phPrevRow">
            <span className={`codeStatus codeStatus-${status}`}>
              {status === 'running' && <span className="codeSpinner" />}{CODE_STATUS[status] || status.toUpperCase()}
            </span>
            <span className="phPrevDim">{d.project}</span>
          </div>
          {d.request && <div className="phPrevLine"><span className="phPrevDim">Asked: </span>{d.request}</div>}
          {status === 'running'
            ? last && <div className="phPrevLine phPrevMono">{last.text}</div>
            : d.summary && <div className="phPrevLine phClamp">{d.summary}</div>}
          {denials.length > 0 && (
            <div className="phPrevLine phPrevWarn">Wants to run: {denials.map(x => x.command).join('; ')}</div>
          )}
          {(status === 'running' || denials.length > 0) && (
            <div className="phPrevActions" onClick={e => e.stopPropagation()}>
              {denials.length > 0 && status !== 'running' &&
                <button className="phPill phPillGo" onClick={() => coding('approve', d.project)}>Allow &amp; continue</button>}
              {status === 'running' && <button className="phPill" onClick={() => coding('stop', d.project)}>Stop</button>}
            </div>
          )}
        </div>
      );
    }
    case 'image':
      return d.src ? <img className="phPrevImg" src={d.src} alt={d.caption || panel.title} /> : null;
    case 'places':
    case 'map': {
      const places: any[] = d.places || [];
      if (!places.length) return <div className="phPrevDim">Open to see the map</div>;
      return (
        <ol className="phPrevList">
          {places.slice(0, 5).map((p, i) => <li key={i}>{p.name}</li>)}
          {places.length > 5 && <li className="phPrevDim">and {places.length - 5} more</li>}
        </ol>
      );
    }
    case 'page': {
      let host = '';
      try { host = new URL(d.url).hostname.replace(/^www\./, ''); } catch { /* no url */ }
      return <div className="phPrevDim">{host || 'Web page'} · tap to read</div>;
    }
    case 'file':
      return <pre className="phPrevMono phFade phPrevFile">{String(d.content || '').split('\n').slice(0, 8).join('\n')}</pre>;
    default:
      return null;
  }
}

export default function PhoneStage({ hidden, focus, onFocused }: { hidden: boolean; focus: string | null; onFocused: () => void }) {
  const { stage } = useStage();
  const [open, setOpen] = useState<string | null>(null);
  const latest = useMemo(() => {
    const byPanel = new Map<string, Highlight>();
    for (const h of stage.highlights) byPanel.set(h.panel, h);
    return byPanel;
  }, [stage.highlights]);
  // numbered like the PC's Stage ("close panel 2"); newest first
  const numbered = stage.panels.map((p, n) => ({ p, n: n + 1 }));
  const shown = [...numbered].sort((a, b) => b.p.created - a.p.created);
  const index = shown.findIndex(x => x.p.id === open);
  const current = index >= 0 ? shown[index] : null;

  // "View" on the chat's toast opens that panel
  useEffect(() => {
    if (focus && !hidden) { setOpen(focus); onFocused(); }
  }, [focus, hidden, onFocused]);

  // The phone's back gesture closes the viewer instead of leaving the page
  useEffect(() => {
    if (!open) return;
    window.history.pushState({ viewer: true }, '');
    const back = () => setOpen(null);
    window.addEventListener('popstate', back);
    return () => window.removeEventListener('popstate', back);
  }, [open !== null]);   // eslint-disable-line react-hooks/exhaustive-deps
  const close = () => { if (window.history.state?.viewer) window.history.back(); else setOpen(null); };

  // The list behind stays still while a panel is open (iOS passes swipes through otherwise)
  useEffect(() => {
    document.querySelector('.phStageWrap')?.classList.toggle('phLocked', open !== null);
  }, [open]);

  // Closed on the PC (or by voice) while open here
  useEffect(() => { if (open && index < 0) setOpen(null); }, [open, index]);

  if (!stage.panels.length) {
    return <div className="phEmpty">Nothing on the Stage. Ask Jarvis to show you something: a map, a page, your agenda…</div>;
  }
  return (
    <>
      <div className="phStage">
        {shown.map(({ p, n }) => (
          <div key={p.id} role="button" tabIndex={0}
               className={`phCard phCard-${p.kind} ${latest.has(p.id) ? 'phCardMarked' : ''}`}
               onClick={() => setOpen(p.id)}>
            <div className="phCardHead">
              <span className="panelNum">{n}</span>
              <span className="panelKind">{KIND_NAME[p.kind]}</span>
              <span className="phCardTitle">{p.title}</span>
              <button className="phIconBtn phIconSmall" aria-label="Close panel"
                      onClick={e => { e.stopPropagation(); stageApi.post(`panel/${p.id}/close`); }}>✕</button>
            </div>
            <div className="phCardPreview"><Preview panel={p} /></div>
          </div>
        ))}
        {stage.undo && <button className="phPill phUndo" onClick={() => stageApi.post('restore')}>Undo: {stage.undo}</button>}
      </div>

      {/* rendered at the page's root, outside the scrolling list: iOS mis-places fixed
          elements inside scroll areas (and .phone carries the theme's colors) */}
      {current && createPortal(
        <div className="phViewer" role="dialog" aria-label={current.p.title}>
          <div className="phViewerHead">
            <div className="phViewerTitle">
              <span className="panelKind">{current.n} · {KIND_NAME[current.p.kind]}</span>
              <span className="phCardTitle">{current.p.title}</span>
            </div>
            <button className="phViewerClose" aria-label="Close" onClick={close}>✕</button>
          </div>
          <div key={current.p.id} className={`phViewerBody phViewerBody-${current.p.kind}`}>
            <div className="panelBody">
              <PanelContent key={current.p.id} panel={current.p} highlight={latest.get(current.p.id) || null}
                            hidden={hidden} all={stage.panels as Panel[]} highlights={stage.highlights} />
            </div>
          </div>
          <div className="phViewerNav">
            <button className="phNavBtn" disabled={index <= 0} onClick={() => setOpen(shown[index - 1].p.id)}>‹ Newer</button>
            <span className="phNavCount">{index + 1} of {shown.length}</span>
            <button className="phNavBtn" disabled={index >= shown.length - 1} onClick={() => setOpen(shown[index + 1].p.id)}>Older ›</button>
          </div>
        </div>,
        document.querySelector('.phone') ?? document.body,
      )}
    </>
  );
}

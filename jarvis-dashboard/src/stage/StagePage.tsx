import { useEffect, useMemo, useRef, useState } from 'react';
import GridLayout, { verticalCompactor, Layout, LayoutItem } from 'react-grid-layout';
import 'react-grid-layout/css/styles.css';
import ParticleOrb from '../ParticleOrb';
import { useJarvis } from '../JarvisStatus';
import { useStage, stageApi, GridItem, Highlight, Panel } from './StageContext';
import PanelFrame from './PanelFrame';
import StageBeam from './StageBeam';
import WebPanel from './panels/WebPanel';
import SummaryPanel from './panels/SummaryPanel';
import NotePanel from './panels/NotePanel';
import ImagePanel from './panels/ImagePanel';
import FileEditorPanel from './panels/FileEditorPanel';
import MapPanel from './panels/MapPanel';
import CodePanel from './panels/CodePanel';
import type { PanelProps } from './panels/types';
import './Stage.css';

// The Stage: panels Jarvis opens while it works, tiled to fill the screen. Drag a
// panel by its header (drop it on another to swap), resize from its edges, or say
// "focus on the map", "put them side by side", "close panel 2"...

const ROWS = 12;
const MARGIN = 10;
const PADDING = 12;

const VIEWS: { mode: string; label: string; title: string }[] = [
  { mode: 'auto',    label: 'Auto',    title: 'Tile panels by how much room each needs' },
  { mode: 'focus',   label: 'Focus',   title: 'One panel large, the rest beside it' },
  { mode: 'columns', label: 'Columns', title: 'Side by side' },
  { mode: 'rows',    label: 'Rows',    title: 'Stacked' },
];

const STATE_LABELS: Record<string, string> = {
  idle: 'STANDBY', activated: 'LISTENING', thinking: 'THINKING', speaking: 'SPEAKING', error: 'ERROR',
};

function PanelContent(props: PanelProps) {
  switch (props.panel.kind) {
    case 'page':    return <WebPanel {...props} />;
    case 'summary': return <SummaryPanel {...props} />;
    case 'note':    return <NotePanel {...props} />;
    case 'image':   return <ImagePanel {...props} />;
    case 'file':    return <FileEditorPanel {...props} />;
    case 'map':     return <MapPanel {...props} />;
    case 'code':    return <CodePanel {...props} />;
    default:        return <div className="panelEmpty">Unknown panel</div>;
  }
}

export default function StagePage({ visible }: { visible: boolean }) {
  const { stage, connected } = useStage();
  const jarvis = useJarvis();
  const gridRef = useRef<HTMLDivElement>(null);
  const orbRef = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const [local, setLocal] = useState<GridItem[]>(stage.layout);
  const [dragging, setDragging] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);
  const dragStart = useRef<GridItem[]>([]);

  // "Clear stage" needs a second click within a few seconds
  useEffect(() => {
    if (!confirmClear) return;
    const t = setTimeout(() => setConfirmClear(false), 3500);
    return () => clearTimeout(t);
  }, [confirmClear]);

  // The server's layout wins whenever it changes (voice commands, new panels)
  useEffect(() => { setLocal(stage.layout); }, [stage.layout]);

  useEffect(() => {
    const el = gridRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setSize({ w: el.clientWidth, h: el.clientHeight }));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const rowHeight = Math.max(24, (size.h - PADDING * 2 - MARGIN * (ROWS - 1)) / ROWS);
  const panels = stage.panels;
  const focusId = stage.view.mode === 'focus' ? stage.view.focus : null;

  const latestMark = useMemo(() => {
    const byPanel = new Map<string, Highlight>();
    for (const h of stage.highlights) byPanel.set(h.panel, h);
    return byPanel;
  }, [stage.highlights]);
  const active = stage.highlights.find(h => h.id === stage.active) ?? null;

  const onDragStart = () => {
    dragStart.current = local;
    setDragging(true);
  };

  // Dropping a panel onto another swaps them; dropping into free space keeps the new spot
  const onDragStop = (layout: Layout, _old: LayoutItem | null, moved: LayoutItem | null) => {
    setDragging(false);
    if (!moved) return;
    const before = dragStart.current;
    const cx = moved.x + moved.w / 2, cy = moved.y + moved.h / 2;
    const target = before.find(it => it.i !== moved.i && cx >= it.x && cx < it.x + it.w && cy >= it.y && cy < it.y + it.h);
    const self = before.find(it => it.i === moved.i);
    let next: GridItem[];
    if (target && self) {
      next = before.map(it =>
        it.i === self.i ? { ...it, x: target.x, y: target.y, w: target.w, h: target.h }
        : it.i === target.i ? { ...it, x: self.x, y: self.y, w: self.w, h: self.h }
        : it);
    } else {
      next = layout.map(({ i, x, y, w, h }) => ({ i, x, y, w, h }));
    }
    setLocal(next);
    stageApi.post('layout', { layout: next });
  };

  const onResizeStop = (layout: Layout) => {
    setDragging(false);
    const next = layout.map(({ i, x, y, w, h }) => ({ i, x, y, w, h }));
    setLocal(next);
    stageApi.post('layout', { layout: next });
  };

  const lastJarvis = [...jarvis.log].reverse().find(m => m.role === 'jarvis');
  const caption = jarvis.orbState === 'speaking' && lastJarvis
    ? lastJarvis.text
    : jarvis.transcript ? `“${jarvis.transcript}”` : 'Say “Jarvis” or click the orb';

  return (
    <div className={`stagePage ${visible ? '' : 'stageHidden'}`} aria-hidden={!visible}>
      <div className="stageBar">
        <div ref={orbRef} className="stageOrb">
          <ParticleOrb compact paused={!visible} state={jarvis.orbState} beat={jarvis.speechBeat}
                       speech={jarvis.speech} onActivate={jarvis.activate} />
        </div>
        <div className="stageStatus">
          <div className={`stageState stageState-${jarvis.orbState}`}>
            <span className="stageStateDot" />{STATE_LABELS[jarvis.orbState]}
            {!connected && <span className="stageOffline">· STAGE OFFLINE</span>}
          </div>
          <div className="stageCaption" title={caption}>{caption}</div>
        </div>
        <div className="stageControls">
          <div className="segmented" role="group" aria-label="Layout">
            {VIEWS.map(v => (
              <button key={v.mode} title={v.title} disabled={!panels.length}
                      className={(stage.view.mode === v.mode) ? 'on' : ''}
                      onClick={() => stageApi.post('arrange', { mode: v.mode, panel: focusId ?? undefined })}>
                {v.label}
              </button>
            ))}
          </div>
          {stage.undo && (
            <button className="stageBtn stageBtnUndo" title={`Undo: ${stage.undo}`}
                    onClick={() => stageApi.post('restore')}>↶ Undo</button>
          )}
          <button className="stageBtn" disabled={!stage.highlights.length} title="Remove Jarvis's marks"
                  onClick={() => stageApi.post('highlights/clear')}>Clear marks</button>
          <button className={`stageBtn stageBtnDanger ${confirmClear ? 'stageBtnConfirm' : ''}`}
                  disabled={!panels.length}
                  title="Close every panel (you can undo it)"
                  onClick={() => {
                    if (!confirmClear) { setConfirmClear(true); return; }
                    setConfirmClear(false);
                    stageApi.post('clear');
                  }}>
            {confirmClear ? 'Click to confirm' : 'Clear stage'}
          </button>
        </div>
      </div>

      <div ref={gridRef} className={`stageGrid ${dragging ? 'isDragging' : ''}`}>
        {panels.length === 0 ? (
          <div className="stageEmpty">
            <div className="stageEmptyRing" />
            <h2>The Stage is clear</h2>
            {stage.undo && (
              <button className="stageBtn stageBtnUndo stageEmptyRestore" onClick={() => stageApi.post('restore')}>
                ↶ Bring back what was here
              </button>
            )}
            <p>Jarvis puts what it's working on here — live pages with key points, files you can edit,
               images, notes and a 3-D globe — and points things out as it talks.</p>
            <ul>
              <li>“Jarvis, find an article about fusion energy and summarize it”</li>
              <li>“Jarvis, show me Kyoto and Osaka on the map”</li>
              <li>“Jarvis, write a packing list for a ski trip”</li>
              <li>“Jarvis, focus on the map” · “put them side by side” · “close panel 2”</li>
              <li>“Jarvis, clear the stage” · “restore the stage” — the Stage is kept between sessions</li>
            </ul>
          </div>
        ) : size.w > 0 && (
          <GridLayout
            width={size.w}
            layout={local}
            gridConfig={{ cols: 12, rowHeight, margin: [MARGIN, MARGIN], containerPadding: [PADDING, PADDING] }}
            dragConfig={{ enabled: true, handle: '.panelDrag', cancel: '.panelActions' }}
            resizeConfig={{ enabled: true, handles: ['se', 'e', 's'] }}
            compactor={verticalCompactor}
            onDragStart={onDragStart}
            onDragStop={onDragStop}
            onResizeStart={() => setDragging(true)}
            onResizeStop={onResizeStop}
          >
            {panels.map((p: Panel, n) => (
              <div key={p.id}>
                <PanelFrame
                  panel={p}
                  number={n + 1}
                  focused={focusId === p.id}
                  marked={active?.panel === p.id}
                  onFocus={() => stageApi.post('arrange', focusId === p.id ? { mode: 'auto' } : { mode: 'focus', panel: p.id })}
                  onClose={() => stageApi.post(`panel/${p.id}/close`)}
                >
                  <PanelContent panel={p} highlight={latestMark.get(p.id) ?? null} hidden={!visible}
                                all={panels} highlights={stage.highlights} />
                </PanelFrame>
              </div>
            ))}
          </GridLayout>
        )}
      </div>

      <StageBeam active={active} originRef={orbRef} paused={!visible} />
    </div>
  );
}

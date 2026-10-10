import { useEffect, useMemo, useRef, useState } from 'react';
import GridLayout, { getCompactor, verticalCompactor, Layout, LayoutItem } from 'react-grid-layout';
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
import PlacesPanel from './panels/PlacesPanel';
import CodePanel from './panels/CodePanel';
import type { PanelProps } from './panels/types';
import './Stage.css';

// The Stage: panels Jarvis opens while it works, tiled to fill the screen. Drag a
// panel by its header (drop it on another to swap), resize from its edges, or say
// "focus on the map", "put them side by side", "close panel 2"...

const ROWS = 12;
const MARGIN = 10;
const PADDING = 12;

const MIN_SIZE = 2;                                   // smallest panel, in grid cells
const RESIZE_HANDLES = ['n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw'] as const;
const freeCompactor = getCompactor(null, true);       // while resizing: nothing gets pushed around

type Axis = 'x' | 'y';
const SIZE = { x: 'w', y: 'h' } as const;
const CROSS = { x: 'y', y: 'x' } as const;

// Move the border at `line` (a grid line on `axis`) to `to`, like a window splitter:
// every panel along that stretch of border shrinks or grows with it, so dragging any
// edge of a panel takes space from — or gives it back to — the panels beside it.
function moveBorder(items: GridItem[], selfId: string, axis: Axis, line: number, to: number): GridItem[] {
  if (line === to) return items;
  const size = SIZE[axis], cross = CROSS[axis], crossSize = SIZE[cross];
  const self = items.find(it => it.i === selfId)!;
  const before = (it: GridItem) => it[axis] + it[size] === line;   // ends at the border
  const after = (it: GridItem) => it[axis] === line;               // starts at it

  // The resized panel plus everything touching the same stretch of border
  const group = new Set([selfId]);
  let lo = self[cross], hi = self[cross] + self[crossSize];
  for (let grew = true; grew;) {
    grew = false;
    for (const it of items) {
      if (group.has(it.i) || !(before(it) || after(it))) continue;
      if (it[cross] < hi && it[cross] + it[crossSize] > lo) {
        group.add(it.i);
        lo = Math.min(lo, it[cross]);
        hi = Math.max(hi, it[cross] + it[crossSize]);
        grew = true;
      }
    }
  }

  let target = to;   // no panel in the group may end up smaller than MIN_SIZE
  for (const it of items) {
    if (!group.has(it.i)) continue;
    if (before(it)) target = Math.max(target, it[axis] + MIN_SIZE);
    else target = Math.min(target, it[axis] + it[size] - MIN_SIZE);
  }
  return items.map(it => {
    if (!group.has(it.i)) return it;
    if (before(it)) return { ...it, [size]: target - it[axis] };
    return { ...it, [axis]: target, [size]: it[axis] + it[size] - target };
  });
}

function overlaps(items: GridItem[]) {
  return items.some((a, n) => items.some((b, m) => m > n &&
    a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h));
}

// Apply a resize (from `start`, the layout before it) edge by edge
function resizeTiled(start: GridItem[], id: string, to: LayoutItem): GridItem[] {
  let items = start;
  const cur = () => items.find(it => it.i === id)!;
  items = moveBorder(items, id, 'x', cur().x, to.x);
  items = moveBorder(items, id, 'x', cur().x + cur().w, to.x + to.w);
  items = moveBorder(items, id, 'y', cur().y, to.y);
  items = moveBorder(items, id, 'y', cur().y + cur().h, to.y + to.h);
  return overlaps(items) ? start : items;   // an odd hand-made layout it can't untangle: keep it as it was
}

const VIEWS: { mode: string; label: string; title: string }[] = [
  { mode: 'auto',    label: 'Auto',    title: 'Tile panels by how much room each needs' },
  { mode: 'focus',   label: 'Focus',   title: 'One panel large, the rest beside it' },
  { mode: 'columns', label: 'Columns', title: 'Side by side' },
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
    case 'places':  return <PlacesPanel {...props} />;
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
  const [resizing, setResizing] = useState(false);
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

  const onResizeStart = () => {
    dragStart.current = local;
    setDragging(true);
    setResizing(true);
  };

  const onResizeStop = (_layout: Layout, _old: LayoutItem | null, resized: LayoutItem | null) => {
    setDragging(false);
    setResizing(false);
    if (!resized) return;
    const next = resizeTiled(dragStart.current, resized.i, resized);
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
            resizeConfig={{ enabled: true, handles: [...RESIZE_HANDLES] }}
            compactor={resizing ? freeCompactor : verticalCompactor}
            onDragStart={onDragStart}
            onDragStop={onDragStop}
            onResizeStart={onResizeStart}
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

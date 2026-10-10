import { createContext, useContext, useEffect, useRef, useState, ReactNode } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';

// Live Stage state from the server (Server-Sent Events, /api/stage/events). The
// server is the single source of truth: voice commands and mouse edits both go
// through it, and every change comes back here as a full snapshot.

export type PanelKind = 'page' | 'summary' | 'file' | 'image' | 'note' | 'map' | 'places' | 'code';

export interface Panel {
  id: string;
  kind: PanelKind;
  title: string;
  data: any;
  weight: number;
  created: number;
}

export interface GridItem { i: string; x: number; y: number; w: number; h: number }

export interface HighlightTarget {
  quote?: string;            // page / note / summary
  region?: number[];         // image: [x, y, w, h] as fractions
  lines?: number[];          // file: [first, last]
  place?: number;            // map / places: index into data.places
}

export interface Highlight {
  id: string;
  panel: string;
  target: HighlightTarget;
  label: string;
  time: number;
}

export interface StageState {
  rev: number;
  panels: Panel[];
  layout: GridItem[];
  view: { mode: string; focus: string | null };
  highlights: Highlight[];
  active: string | null;
  route: string;
  /** What the last close/clear removed ("Clear stage", "Close <title>"), if it can be undone. */
  undo: string | null;
}

const EMPTY: StageState = { rev: 0, panels: [], layout: [], view: { mode: 'auto', focus: null },
                            highlights: [], active: null, route: '/', undo: null };

export const isDesktop = !!window.jarvisDesktop?.isDesktop;

// Page panels register a reader for Jarvis's "extract" requests (text of the live page)
export const extractors = new Map<string, () => Promise<unknown>>();

export const stageApi = {
  post(path: string, body: object = {}) {
    return fetch(`/api/stage/${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).catch(() => undefined);
  },
};

const StageContext = createContext<{ stage: StageState; connected: boolean }>({ stage: EMPTY, connected: false });

export function useStage() {
  return useContext(StageContext);
}

export function StageProvider({ children }: { children: ReactNode }) {
  const [stage, setStage] = useState<StageState>(EMPTY);
  const [connected, setConnected] = useState(false);
  const navigate = useNavigate();
  const location = useLocation();
  const pathRef = useRef(location.pathname);
  pathRef.current = location.pathname;
  // react-router hands out a new navigate() on every route change; keep the latest in a
  // ref so the connection below is opened once (reconnecting used to bounce you back to
  // the Stage, because the first message says where Jarvis last showed it)
  const navRef = useRef(navigate);
  navRef.current = navigate;
  const serverRoute = useRef('/');

  // Switching tabs yourself tells the server, so it remembers where you actually are
  useEffect(() => {
    const p = location.pathname === '/stage' ? '/stage' : location.pathname === '/' ? '/' : null;
    if (p && p !== serverRoute.current) {
      serverRoute.current = p;
      stageApi.post('route', { to: p });
    }
  }, [location.pathname]);

  useEffect(() => {
    let first = true;
    const es = new EventSource(`/api/stage/events?client=${isDesktop ? 'desktop' : 'web'}`);
    const go = (to: string) => {
      serverRoute.current = to;
      if (to && to !== pathRef.current) navRef.current(to);
    };

    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);     // EventSource reconnects by itself
    es.onmessage = ev => {
      let msg: any;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.type === 'state') {
        setStage(msg.state);
        // A window opened because Jarvis put something on the Stage: go straight there
        if (first && msg.state.route === '/stage' && msg.state.panels.length && pathRef.current === '/') go('/stage');
        first = false;
      } else if (msg.type === 'navigate') {
        go(msg.to);
      } else if (msg.type === 'extract') {
        const read = extractors.get(msg.panel);
        if (read) {
          read().then(result => stageApi.post(`extract/${msg.request}`, result as object))
                .catch(() => stageApi.post(`extract/${msg.request}`, {}));
        }
      }
    };
    return () => es.close();
  }, []);

  return <StageContext.Provider value={{ stage, connected }}>{children}</StageContext.Provider>;
}

import { useEffect, useRef, useState } from 'react';
import { extractors, isDesktop, stageApi } from '../StageContext';
import { callInPage, jarvisExtract, jarvisHighlight, jarvisHighlightRect } from '../pageScripts';
import { setAnchor, toAnchor, AnchorRect } from '../anchors';
import type { PanelProps } from './types';

// A live web page. In the desktop app it is a real browser view (<webview>) that the
// dashboard can read, scroll and highlight; in a plain browser tab it falls back to a
// reader view of the text Jarvis fetched.

export default function WebPanel(props: PanelProps) {
  return isDesktop ? <LivePage {...props} /> : <ReaderPage {...props} />;
}

function hostOf(url: string) {
  try { return new URL(url).hostname.replace(/^www\./, ''); } catch { return url; }
}

function LivePage({ panel, highlight, hidden }: PanelProps) {
  const url: string = panel.data.url;
  const [initialUrl] = useState(url);               // later changes go through loadURL
  const ref = useRef<WebviewElement | null>(null);
  const [nav, setNav] = useState({ back: false, fwd: false, url });
  const [loading, setLoading] = useState(true);
  const [loadTick, setLoadTick] = useState(0);      // bumps after each page load
  const markRect = useRef<AnchorRect | null>(null);
  const serverUrl = useRef(url);                    // the last URL the server and page agreed on

  useEffect(() => {
    const wv = ref.current;
    if (!wv) return;
    const sync = () => {
      try {
        setNav({ back: wv.canGoBack(), fwd: wv.canGoForward(), url: wv.getURL() });
      } catch { /* not attached yet */ }
    };
    const onNavigate = (e: any) => {
      if (e.isMainFrame === false) return;
      sync();
      serverUrl.current = wv.getURL();
      stageApi.post(`page/${panel.id}`, { url: wv.getURL(), title: wv.getTitle() });
    };
    const onTitle = (e: any) => stageApi.post(`page/${panel.id}`, { title: e.title });
    const onStart = () => setLoading(true);
    const onStop = () => { setLoading(false); sync(); setLoadTick(t => t + 1); };
    wv.addEventListener('did-navigate', onNavigate);
    wv.addEventListener('did-navigate-in-page', onNavigate);
    wv.addEventListener('page-title-updated', onTitle);
    wv.addEventListener('did-start-loading', onStart);
    wv.addEventListener('did-stop-loading', onStop);
    extractors.set(panel.id, () => wv.executeJavaScript(callInPage(jarvisExtract)));
    return () => {
      wv.removeEventListener('did-navigate', onNavigate);
      wv.removeEventListener('did-navigate-in-page', onNavigate);
      wv.removeEventListener('page-title-updated', onTitle);
      wv.removeEventListener('did-start-loading', onStart);
      wv.removeEventListener('did-stop-loading', onStop);
      extractors.delete(panel.id);
    };
  }, [panel.id]);

  // Jarvis pointed the panel at a different page. Only react to the server's URL
  // changing — the page's own navigation reaches the server first and comes back equal.
  useEffect(() => {
    const wv = ref.current;
    if (!wv || !url || url === serverUrl.current) return;
    serverUrl.current = url;
    try {
      if (wv.getURL() !== url) wv.loadURL(url).catch(() => {});
    } catch {
      wv.src = url;      // not attached yet
    }
  }, [url]);

  // Mute pages while the Stage is hidden behind the dashboard
  useEffect(() => {
    try { (ref.current as any)?.setAudioMuted?.(!!hidden); } catch { /* not attached yet */ }
  }, [hidden, loadTick]);

  // Apply / clear Jarvis's mark inside the page, and track where it is for the beam
  const quote = highlight?.target.quote ?? '';
  const hlId = highlight?.id ?? '';
  const label = highlight?.label ?? '';
  useEffect(() => {
    const wv = ref.current;
    if (!wv || !loadTick) return;
    markRect.current = null;
    wv.executeJavaScript(callInPage(jarvisHighlight, quote, label, 'page', null)).catch(() => {});
    if (!quote || !hlId) return;
    let stopped = false;
    const poll = async () => {
      while (!stopped) {
        try {
          const r = await wv.executeJavaScript<{ x: number; y: number; w: number; h: number } | null>(
            callInPage(jarvisHighlightRect, 'page'));
          const box = wv.getBoundingClientRect();
          markRect.current = r && box.width
            ? { x: box.left + r.x, y: box.top + Math.max(0, r.y), w: Math.min(r.w, box.width), h: r.h }
            : null;
        } catch { markRect.current = null; }
        await new Promise(res => setTimeout(res, 120));
      }
    };
    poll();
    const release = setAnchor(hlId, () => markRect.current);
    return () => { stopped = true; release(); };
  }, [quote, hlId, label, loadTick]);

  const wv = () => ref.current;
  return (
    <div className="webPanel">
      <div className="webBar">
        <button className="webBtn" title="Back" disabled={!nav.back} onClick={() => wv()?.goBack()}>‹</button>
        <button className="webBtn" title="Forward" disabled={!nav.fwd} onClick={() => wv()?.goForward()}>›</button>
        <button className="webBtn" title="Reload" onClick={() => wv()?.reload()}>⟳</button>
        <div className="webUrl" title={nav.url}><span className="webHost">{hostOf(nav.url)}</span>{nav.url.replace(/^https?:\/\/[^/]+/, '')}</div>
        <button className="webBtn" title="Open in your browser" onClick={() => window.open(nav.url, '_blank')}>↗</button>
      </div>
      {loading && <div className="webLoading" />}
      <webview ref={ref as any} src={initialUrl} partition="persist:stage" className="webView" />
    </div>
  );
}

function ReaderPage({ panel, highlight }: PanelProps) {
  const paragraphs: string[] | null = panel.data.reader ?? null;
  const url: string = panel.data.url;
  const selector = `[data-reader="${panel.id}"]`;
  const quote = highlight?.target.quote ?? '';
  const hlId = highlight?.id ?? '';
  const label = highlight?.label ?? '';
  const readerKey = paragraphs ? `${paragraphs.length}:${(paragraphs[0] ?? '').slice(0, 40)}` : '';

  useEffect(() => {
    if (!readerKey) return;
    jarvisHighlight(quote, label, panel.id, selector);
    if (!quote || !hlId) return;
    return setAnchor(hlId, () => {
      const r = jarvisHighlightRect(panel.id);
      return r ? toAnchor(new DOMRect(r.x, r.y, r.w, r.h)) : null;
    });
  }, [quote, hlId, label, readerKey, panel.id, selector]);

  return (
    <div className="webPanel">
      <div className="webBar">
        <div className="webUrl" title={url}><span className="webHost">{hostOf(url)}</span>{url.replace(/^https?:\/\/[^/]+/, '')}</div>
        <a className="webBtn" href={url} target="_blank" rel="noreferrer" title="Open the live page">↗</a>
      </div>
      <div className="readerBody" data-reader={panel.id}>
        <div className="readerNote">Reader view — live pages need the J.A.R.V.I.S desktop app.</div>
        {paragraphs === null
          ? <div className="panelEmpty">Reading the page…</div>
          : paragraphs.length === 0
            ? <div className="panelEmpty">This page couldn't be read here. Open it with ↗.</div>
            : paragraphs.map((p, i) => <p key={i}>{p}</p>)}
      </div>
    </div>
  );
}

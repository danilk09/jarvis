import { useEffect, useRef, useState } from 'react';
import { setAnchor } from '../anchors';
import { stageApi } from '../StageContext';
import type { PanelProps } from './types';

// Places to choose between ("pizza in Honolulu"): a numbered list with what each is known
// for, beside a dark street map (Leaflet + Esri's keyless dark basemap, loaded on first use).
// Clicking an option or its pin — or Jarvis talking about it — flies the map there.

const LEAFLET = 'https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/';
const ESRI = 'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/';
const BASE_TILES = ESRI + 'World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}';
const LABEL_TILES = ESRI + 'World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}';
const ATTRIBUTION = 'Tiles © Esri, HERE, Garmin, © OpenStreetMap contributors';

let leafletPromise: Promise<any> | null = null;
function loadLeaflet(): Promise<any> {
  if ((window as any).L) return Promise.resolve((window as any).L);
  if (!leafletPromise) {
    leafletPromise = new Promise((resolve, reject) => {
      const css = document.createElement('link');
      css.rel = 'stylesheet';
      css.href = LEAFLET + 'leaflet.css';
      document.head.appendChild(css);
      // Same trick as the globe: Monaco's AMD `define` would swallow the UMD bundle
      const fail = () => { leafletPromise = null; reject(new Error('Could not load the map (offline?)')); };
      fetch(LEAFLET + 'leaflet.js')
        .then(r => (r.ok ? r.text() : Promise.reject()))
        .then(code => {
          const url = URL.createObjectURL(new Blob([`(function (define) {\n${code}\n})(undefined);`],
                                                   { type: 'text/javascript' }));
          const script = document.createElement('script');
          script.src = url;
          script.onload = () => {
            URL.revokeObjectURL(url);
            (window as any).L ? resolve((window as any).L) : fail();
          };
          script.onerror = fail;
          document.head.appendChild(script);
        })
        .catch(fail);
    });
  }
  return leafletPromise;
}

interface Place {
  name: string; lat: number; lon: number;
  address?: string; description?: string; website?: string; hours?: string;
}

export default function PlacesPanel({ panel, highlight, hidden }: PanelProps) {
  const { places = [], active = -1, flight = 0 } = panel.data as { places: Place[]; active: number; flight: number };
  const marked: number = highlight?.target.place ?? active;
  const rootRef = useRef<HTMLDivElement>(null);
  const hostRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<any>(null);
  const layerRef = useRef<any>(null);
  const pinsRef = useRef<any[]>([]);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState('');
  const [wide, setWide] = useState(true);

  // Create the map once
  useEffect(() => {
    let gone = false;
    loadLeaflet().then(L => {
      if (gone || !hostRef.current) return;
      const map = L.map(hostRef.current, { zoomControl: false, worldCopyJump: true });
      // the basemap stops at zoom 16; beyond that it's just enlarged
      L.tileLayer(BASE_TILES, { attribution: ATTRIBUTION, maxNativeZoom: 16, maxZoom: 19 }).addTo(map);
      L.tileLayer(LABEL_TILES, { maxNativeZoom: 16, maxZoom: 19 }).addTo(map);
      L.control.zoom({ position: 'bottomright' }).addTo(map);
      map.setView([20, 0], 2);
      mapRef.current = map;
      layerRef.current = L.layerGroup().addTo(map);
      setReady(true);
    }).catch(e => setError(e.message));
    return () => {
      gone = true;
      mapRef.current?.remove();
      mapRef.current = null;
    };
  }, []);

  // Side by side when there's room, stacked when the panel is narrow; keep the map sized to its box
  useEffect(() => {
    const el = rootRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      setWide(el.clientWidth > 620);
      mapRef.current?.invalidateSize();
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  useEffect(() => { if (!hidden) mapRef.current?.invalidateSize(); }, [hidden, wide]);

  const select = (i: number) =>
    stageApi.post('highlight', { panel: panel.id, target: { place: i }, label: places[i]?.name ?? '' });

  // Numbered pins; a new search frames all of them
  const placesKey = JSON.stringify(places.map(p => [p.name, p.lat, p.lon]));
  useEffect(() => {
    const L = (window as any).L, map = mapRef.current, layer = layerRef.current;
    if (!ready || !L || !map || !layer) return;
    layer.clearLayers();
    pinsRef.current = places.map((p, i) => {
      const pin = L.marker([p.lat, p.lon], {
        icon: L.divIcon({ className: 'placePin', html: `<span><b>${i + 1}</b></span>`, iconSize: [28, 28], iconAnchor: [14, 28] }),
        riseOnHover: true,
      });
      const tip = document.createElement('div');
      tip.textContent = p.name;
      pin.bindTooltip(tip, { direction: 'top', offset: [0, -28], className: 'placeTip' });
      pin.on('click', () => select(i));
      return pin.addTo(layer);
    });
    if (places.length) {
      map.fitBounds(L.latLngBounds(places.map(p => [p.lat, p.lon])), { padding: [48, 48], maxZoom: 15 });
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, placesKey, flight]);

  // The option being talked about: highlighted pin, map flies to it, list scrolls to it
  useEffect(() => {
    const map = mapRef.current;
    pinsRef.current.forEach((pin, i) => {
      pin.getElement()?.classList.toggle('placePinOn', i === marked);
      if (i === marked) { pin.setZIndexOffset(1000); pin.openTooltip(); } else { pin.setZIndexOffset(0); pin.closeTooltip(); }
    });
    const p = places[marked];
    if (ready && map && p) map.flyTo([p.lat, p.lon], Math.max(map.getZoom(), 15), { duration: 1.2 });
    const box = listRef.current;
    const el = box?.querySelector<HTMLElement>(`[data-place="${marked}"]`);
    if (box && el) box.scrollTo({ top: el.offsetTop - box.clientHeight / 3, behavior: 'smooth' });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, marked, placesKey]);

  // Where the marked pin is on screen, for the beam from the orb
  const hlId = highlight?.id ?? '';
  useEffect(() => {
    const pin = pinsRef.current[marked];
    if (!ready || !hlId || !pin) return;
    return setAnchor(hlId, () => {
      const r = pin.getElement()?.getBoundingClientRect();
      const box = hostRef.current?.getBoundingClientRect();
      if (!r || !box || r.right < box.left || r.left > box.right || r.bottom < box.top || r.top > box.bottom) return null;
      return { x: r.left, y: r.top, w: r.width, h: r.height };
    });
  }, [ready, hlId, marked, placesKey]);

  return (
    <div ref={rootRef} className={`placesPanel ${wide ? 'placesWide' : ''}`}>
      <div className="placesList" ref={listRef}>
        {places.length === 0 && <div className="panelEmpty">No places</div>}
        <ol>
          {places.map((p, i) => (
            <li key={i} data-place={i}>
              <button className={`summaryPoint placeItem ${i === marked ? 'summaryPointActive' : ''}`} onClick={() => select(i)}>
                <span className="summaryNum">{i + 1}</span>
                <span className="placeBody">
                  <span className="placeName">{p.name}</span>
                  {p.description && <span className="placeDesc">{p.description}</span>}
                  {(p.address || p.hours) && (
                    <span className="placeMeta">{[p.address, p.hours].filter(Boolean).join(' · ')}</span>
                  )}
                  {p.website && /^https?:/i.test(p.website) && (
                    <a className="placeLink" href={p.website} target="_blank" rel="noreferrer"
                       onClick={e => e.stopPropagation()}>Website ↗</a>
                  )}
                </span>
              </button>
            </li>
          ))}
        </ol>
      </div>
      <div className="placesMap">
        <div ref={hostRef} className="placesMapHost" />
        {!ready && !error && <div className="panelEmpty mapStatus">Loading the map…</div>}
        {error && <div className="panelEmpty mapStatus">{error}</div>}
      </div>
    </div>
  );
}

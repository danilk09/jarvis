import { useEffect, useRef, useState } from 'react';
import { setAnchor } from '../anchors';
import type { PanelProps } from './types';

// A 3-D globe (CesiumJS) styled for Jarvis: darkened satellite imagery, a glowing
// lat/long grid, a green-shifted atmosphere and labelled beacons for places. New
// places are reached by flying up to orbit and down again; with nothing to do the
// globe slowly turns. A free Cesium ion token (CESIUM_ION_TOKEN in .env) adds 3-D
// terrain and holographic 3-D buildings. Your own location is never used.

const CESIUM_VERSION = '1.146.0';
const BASE = `https://cdn.jsdelivr.net/npm/cesium@${CESIUM_VERSION}/Build/Cesium/`;
const GREEN = '#3dff8f';

let cesiumPromise: Promise<any> | null = null;
function loadCesium(): Promise<any> {
  if (window.Cesium) return Promise.resolve(window.Cesium);
  if (!cesiumPromise) {
    cesiumPromise = new Promise((resolve, reject) => {
      window.CESIUM_BASE_URL = BASE;
      const css = document.createElement('link');
      css.rel = 'stylesheet';
      css.href = BASE + 'Widgets/widgets.css';
      document.head.appendChild(css);
      // Monaco's AMD loader puts a global `define` on the page, and libraries bundled inside
      // Cesium (punycode, URI.js) call it when present, which breaks both. Run the bundle
      // inside a wrapper that shadows `define` (and lift its `var Cesium` onto window).
      const fail = () => { cesiumPromise = null; reject(new Error('Could not load CesiumJS (offline?)')); };
      fetch(BASE + 'Cesium.js')
        .then(r => (r.ok ? r.text() : Promise.reject()))
        .then(code => {
          const wrapped = `(function (define) {\n${code}\n;window.Cesium = Cesium;\n})(undefined);`;
          const url = URL.createObjectURL(new Blob([wrapped], { type: 'text/javascript' }));
          const script = document.createElement('script');
          script.src = url;
          script.onload = () => {
            URL.revokeObjectURL(url);
            window.Cesium ? resolve(window.Cesium) : fail();
          };
          script.onerror = fail;
          document.head.appendChild(script);
        })
        .catch(fail);
    });
  }
  return cesiumPromise;
}

interface Place { name: string; lat: number; lon: number; detail?: string; bbox?: number[] }

export default function MapPanel({ panel, highlight, hidden }: PanelProps) {
  const { places = [], route = false, focus = -1, flight = 0 } = panel.data as
    { places: Place[]; route: boolean; focus: number; flight: number };
  const hostRef = useRef<HTMLDivElement>(null);
  const creditRef = useRef<HTMLDivElement>(null);
  const viewerRef = useRef<any>(null);
  const lastInput = useRef(0);
  const [error, setError] = useState('');
  const [ready, setReady] = useState(false);

  // Create the viewer once
  useEffect(() => {
    let destroyed = false;
    (async () => {
      let Cesium: any;
      try {
        Cesium = await loadCesium();
      } catch (e: any) {
        setError(e.message);
        return;
      }
      if (destroyed || !hostRef.current) return;
      const cfg = await fetch('/api/stage/config').then(r => r.json()).catch(() => ({}));
      const token: string = cfg.cesiumToken || '';
      if (token) Cesium.Ion.defaultAccessToken = token;

      const viewer = new Cesium.Viewer(hostRef.current, {
        baseLayer: false,
        animation: false, timeline: false, baseLayerPicker: false, geocoder: false,
        homeButton: false, sceneModePicker: false, navigationHelpButton: false,
        fullscreenButton: false, infoBox: false, selectionIndicator: false,
        creditContainer: creditRef.current!,
        terrain: token ? Cesium.Terrain.fromWorldTerrain() : undefined,
        msaaSamples: 4,
      });
      viewerRef.current = viewer;
      const { scene } = viewer;
      const layers = viewer.imageryLayers;

      // Imagery: satellite, darkened and desaturated so the green overlays read clearly
      try {
        const satellite = token
          ? await Cesium.createWorldImageryAsync({ style: Cesium.IonWorldImageryStyle.AERIAL })
          : new Cesium.UrlTemplateImageryProvider({
              url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
              maximumLevel: 19,
              credit: 'Imagery © Esri, Maxar, Earthstar Geographics',
            });
        const sat = layers.addImageryProvider(satellite);
        sat.brightness = 0.62; sat.contrast = 1.3; sat.saturation = 0.35; sat.gamma = 1.1;
      } catch (e) {
        console.warn('Satellite imagery unavailable', e);
      }
      const labels = layers.addImageryProvider(new Cesium.UrlTemplateImageryProvider({
        url: 'https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}',
        maximumLevel: 13,
        credit: 'Labels © Esri',
      }));
      labels.alpha = 0.85;
      const grid = layers.addImageryProvider(new Cesium.GridImageryProvider({
        cells: 4,
        color: Cesium.Color.fromCssColorString(GREEN).withAlpha(0.22),
        glowColor: Cesium.Color.fromCssColorString(GREEN).withAlpha(0.06),
        backgroundColor: Cesium.Color.TRANSPARENT,
        glowWidth: 4,
      }));
      grid.alpha = 0.55;

      // Atmosphere and space
      scene.backgroundColor = Cesium.Color.fromCssColorString('#020806');
      scene.globe.baseColor = Cesium.Color.fromCssColorString('#04120b');
      scene.skyAtmosphere.hueShift = -0.27;          // blue → green
      scene.skyAtmosphere.saturationShift = -0.1;
      scene.skyAtmosphere.brightnessShift = -0.15;
      scene.globe.atmosphereHueShift = -0.27;
      scene.globe.showGroundAtmosphere = true;
      scene.globe.enableLighting = false;
      scene.fog.enabled = true;
      scene.highDynamicRange = false;

      // Holographic 3-D buildings (needs the free ion token)
      if (token) {
        try {
          const buildings = await Cesium.createOsmBuildingsAsync();
          buildings.style = new Cesium.Cesium3DTileStyle({ color: `color('${GREEN}', 0.42)` });
          scene.primitives.add(buildings);
        } catch (e) {
          console.warn('3-D buildings unavailable', e);
        }
      }

      // Slow spin while idle and high up
      const markInput = () => { lastInput.current = performance.now(); };
      const canvas: HTMLCanvasElement = scene.canvas;
      ['pointerdown', 'wheel'].forEach(ev => canvas.addEventListener(ev, markInput, { passive: true }));
      let prev = performance.now();
      scene.preRender.addEventListener(() => {
        const now = performance.now();
        const dt = Math.min((now - prev) / 1000, 0.1);
        prev = now;
        const height = scene.camera.positionCartographic.height;
        // the holographic grid is for the view from orbit; fade it out on the way down
        grid.alpha = Math.max(0, Math.min(0.55, (height - 400_000) / 4_000_000));
        if (now - lastInput.current > 5000 && !viewer.__flying && height > 4_000_000) {
          scene.camera.rotate(Cesium.Cartesian3.UNIT_Z, -0.045 * dt);
        }
      });

      scene.camera.setView({ destination: Cesium.Cartesian3.fromDegrees(-30, 25, 20_000_000) });
      if (!destroyed) setReady(true);
    })();
    return () => {
      destroyed = true;
      try { viewerRef.current?.destroy(); } catch { /* already gone */ }
      viewerRef.current = null;
    };
  }, []);

  // Pause rendering while the Stage is hidden
  useEffect(() => {
    if (viewerRef.current) viewerRef.current.useDefaultRenderLoop = !hidden;
  }, [hidden, ready]);

  // Place beacons, labels and the route
  const placesKey = JSON.stringify(places.map(p => [p.name, p.lat, p.lon])) + route;
  const markedIdx = highlight?.target.place ?? -1;
  useEffect(() => {
    const viewer = viewerRef.current, Cesium = window.Cesium;
    if (!ready || !viewer || !Cesium) return;
    viewer.entities.removeAll();
    const green = Cesium.Color.fromCssColorString(GREEN);
    places.forEach((p, i) => {
      const marked = i === markedIdx || (markedIdx < 0 && i === focus);
      const ground = Cesium.Cartesian3.fromDegrees(p.lon, p.lat, 0);
      viewer.entities.add({
        id: `place-${i}`,
        position: ground,
        point: { pixelSize: marked ? 14 : 9, color: green, outlineColor: Cesium.Color.fromCssColorString('#02140a'),
                 outlineWidth: 2, disableDepthTestDistance: Number.POSITIVE_INFINITY },
        label: {
          text: marked && highlight?.label ? `${p.name}  ◀` : p.name,
          font: `600 ${marked ? 15 : 13}px "Segoe UI", sans-serif`,
          fillColor: marked ? Cesium.Color.fromCssColorString('#02140a') : green,
          showBackground: true,
          backgroundColor: marked ? green : Cesium.Color.fromCssColorString('#020806').withAlpha(0.78),
          backgroundPadding: new Cesium.Cartesian2(8, 5),
          pixelOffset: new Cesium.Cartesian2(0, -22),
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
        },
      });
      // a beam of light standing up from the place
      viewer.entities.add({
        polyline: {
          positions: [ground, Cesium.Cartesian3.fromDegrees(p.lon, p.lat, marked ? 600_000 : 250_000)],
          width: marked ? 10 : 6,
          material: new Cesium.PolylineGlowMaterialProperty({ glowPower: 0.3, color: green.withAlpha(marked ? 0.9 : 0.6) }),
        },
      });
    });
    if (route && places.length > 1) {
      for (let i = 1; i < places.length; i++) {
        const a = places[i - 1], b = places[i];
        const arc = [];
        for (let s = 0; s <= 48; s++) {
          const t = s / 48;
          const lon = a.lon + (b.lon - a.lon) * t, lat = a.lat + (b.lat - a.lat) * t;
          const dist = Math.hypot(b.lon - a.lon, b.lat - a.lat);
          arc.push(Cesium.Cartesian3.fromDegrees(lon, lat, Math.sin(Math.PI * t) * dist * 9000));
        }
        viewer.entities.add({
          polyline: { positions: arc, width: 5,
                      material: new Cesium.PolylineGlowMaterialProperty({ glowPower: 0.25, color: green.withAlpha(0.8) }) },
        });
      }
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, placesKey, markedIdx, focus, highlight?.label]);

  // Fly to the focused place: up to orbit, around the globe, and down
  useEffect(() => {
    const viewer = viewerRef.current, Cesium = window.Cesium;
    const target = places[markedIdx >= 0 ? markedIdx : focus];
    if (!ready || !viewer || !Cesium || !target) return;
    let destination;
    if (target.bbox && target.bbox.length === 4) {
      const [w, s, e, n] = target.bbox;
      const padLat = Math.max((n - s) * 0.35, 0.01), padLon = Math.max((e - w) * 0.35, 0.01);
      destination = Cesium.Rectangle.fromDegrees(w - padLon, s - padLat, e + padLon, n + padLat);
    } else {
      destination = Cesium.Cartesian3.fromDegrees(target.lon, target.lat, 1_500_000);
    }
    viewer.__flying = true;
    viewer.camera.flyTo({
      destination,
      duration: 4.5,
      maximumHeight: 9_000_000,
      easingFunction: Cesium.EasingFunction.QUADRATIC_IN_OUT,
      complete: () => { viewer.__flying = false; lastInput.current = performance.now(); },
      cancel: () => { viewer.__flying = false; },
    });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, flight, markedIdx, placesKey]);

  // Where the marked place is on screen, for the beam from the orb
  const hlId = highlight?.id ?? '';
  useEffect(() => {
    const viewer = viewerRef.current, Cesium = window.Cesium;
    const p = places[markedIdx];
    if (!ready || !viewer || !Cesium || !hlId || !p) return;
    return setAnchor(hlId, () => {
      const pos = Cesium.Cartesian3.fromDegrees(p.lon, p.lat, 0);
      const win = viewer.scene.cartesianToCanvasCoordinates(pos);
      const rect = viewer.scene.canvas.getBoundingClientRect();
      if (!win || win.x < 0 || win.y < 0 || win.x > rect.width || win.y > rect.height) return null;
      return { x: rect.left + win.x - 8, y: rect.top + win.y - 8, w: 16, h: 16 };
    });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, hlId, markedIdx, placesKey]);

  return (
    <div className="mapPanel">
      <div ref={hostRef} className="mapHost" />
      <div ref={creditRef} className="mapCredits" />
      {!ready && !error && <div className="panelEmpty mapStatus">Bringing the globe online…</div>}
      {error && <div className="panelEmpty mapStatus">{error}</div>}
      {places.length > 0 && (
        <div className="mapLegend">
          {places.map((p, i) => <span key={i} className={i === (markedIdx >= 0 ? markedIdx : focus) ? 'on' : ''}>{p.name}</span>)}
        </div>
      )}
    </div>
  );
}

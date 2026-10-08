declare module '*.css';
declare module 'bootstrap/dist/css/bootstrap.min.css';

interface Window {
  /** Set by the Electron desktop app's preload script (desktop/preload.js). */
  jarvisDesktop?: { isDesktop: boolean; platform: string };
  /** CesiumJS, loaded on demand from the CDN by the map panel. */
  Cesium?: any;
  CESIUM_BASE_URL?: string;
}

/** Electron's <webview> element (only rendered inside the desktop app). */
interface WebviewElement extends HTMLElement {
  src: string;
  loadURL(url: string): Promise<void>;
  getURL(): string;
  getTitle(): string;
  goBack(): void;
  goForward(): void;
  canGoBack(): boolean;
  canGoForward(): boolean;
  reload(): void;
  executeJavaScript<T = unknown>(code: string): Promise<T>;
}

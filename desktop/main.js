// JARVIS desktop window. Loads the dashboard served by jarvis.py and enables the
// <webview> tag, so Stage panels can show live web pages that the dashboard can
// read, scroll and highlight. Started by core/desktop.py (JARVIS_URL is passed in).

const { app, BrowserWindow, session, shell } = require('electron');
const path = require('path');

const DASH_URL = process.env.JARVIS_URL || 'http://localhost:5151';
const STAGE_PARTITION = 'persist:stage';   // cookies/logins for Stage pages persist between runs

let win = null;

// Screenshot mode renders even when the window is covered by other windows
if (process.env.JARVIS_CAPTURE) app.commandLine.appendSwitch('disable-features', 'CalculateNativeWinOcclusion');

// (screenshot mode may run next to your normal window)
if (!process.env.JARVIS_CAPTURE && !app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (!win) return;
    if (win.isMinimized()) win.restore();
    win.focus();
  });
  app.whenReady().then(start);
}

app.on('window-all-closed', () => app.quit());

// The dashboard's own origin, e.g. http://localhost:5151
const dashOrigin = new URL(DASH_URL).origin;

async function start() {
  // Block ads, trackers and cookie banners inside Stage pages
  try {
    const { ElectronBlocker } = require('@ghostery/adblocker-electron');
    const blocker = await ElectronBlocker.fromPrebuiltFull(fetch);
    blocker.enableBlockingInSession(session.fromPartition(STAGE_PARTITION));
  } catch (e) {
    console.warn('Ad blocker unavailable:', e.message);
  }

  // Never hand out location (or camera/mic) to web pages; the dashboard itself needs none either
  const allowed = new Set(['fullscreen', 'clipboard-sanitized-write']);
  for (const s of [session.defaultSession, session.fromPartition(STAGE_PARTITION)]) {
    s.setPermissionRequestHandler((_wc, permission, cb) => cb(allowed.has(permission)));
    s.setPermissionCheckHandler((_wc, permission) => allowed.has(permission));
  }

  win = new BrowserWindow({
    width: 1600,
    height: 960,
    minWidth: 960,
    minHeight: 600,
    title: 'J.A.R.V.I.S',
    backgroundColor: '#020806',
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      webviewTag: true,
      backgroundThrottling: !process.env.JARVIS_CAPTURE,
    },
  });
  win.removeMenu();
  win.webContents.on('before-input-event', (event, input) => {
    if (input.type !== 'keyDown') return;
    if (input.key === 'F11') { win.setFullScreen(!win.isFullScreen()); event.preventDefault(); }
    if (input.key === 'F5' || (input.control && input.key.toLowerCase() === 'r')) { win.reload(); event.preventDefault(); }
    if (input.key === 'F12') { win.webContents.toggleDevTools(); event.preventDefault(); }
  });
  loadDashboard();

  if (process.env.JARVIS_CAPTURE) captureAndQuit(process.env.JARVIS_CAPTURE);
}

// Jarvis may still be starting: show a holding page and retry until the server answers
function loadDashboard() {
  win.loadURL(DASH_URL).catch(() => {
    const html = `<body style="margin:0;height:100vh;display:grid;place-items:center;background:#020806;
      color:#5a8a70;font:14px 'Segoe UI',sans-serif;letter-spacing:.2em">CONNECTING TO JARVIS…</body>`;
    win.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(html)).catch(() => {});
    setTimeout(loadDashboard, 1500);
  });
}

app.on('web-contents-created', (_e, contents) => {
  // Every <webview> gets locked-down settings, whatever the page asks for
  contents.on('will-attach-webview', (event, prefs, params) => {
    delete prefs.preload;
    prefs.nodeIntegration = false;
    prefs.contextIsolation = true;
    prefs.sandbox = true;
    if (!/^https?:/i.test(params.src || '')) event.preventDefault();
  });

  if (contents.getType() === 'webview') {
    // Links that open a new window stay inside the same Stage panel
    contents.setWindowOpenHandler(({ url }) => {
      if (/^https?:/i.test(url)) contents.loadURL(url);
      return { action: 'deny' };
    });
    return;
  }

  // The dashboard window: external links open in the normal browser
  contents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/i.test(url)) shell.openExternal(url);
    return { action: 'deny' };
  });
  contents.on('will-navigate', (event, url) => {
    if (!url.startsWith(dashOrigin) && !url.startsWith('data:')) {
      event.preventDefault();
      if (/^https?:/i.test(url)) shell.openExternal(url);
    }
  });
});

// Development aid: JARVIS_CAPTURE=<file.png> [JARVIS_CAPTURE_DELAY=ms] saves a screenshot and exits
function captureAndQuit(file) {
  const delay = Number(process.env.JARVIS_CAPTURE_DELAY || 6000);
  win.webContents.on('console-message', e => console.log(`[page ${e.level}] ${e.message}`));
  setTimeout(async () => {
    try {
      if (process.env.JARVIS_CAPTURE_EVAL) {
        console.log('EVAL:', JSON.stringify(await win.webContents.executeJavaScript(process.env.JARVIS_CAPTURE_EVAL)));
        await new Promise(r => setTimeout(r, 1500));   // let a click or navigation settle
      }
      const image = await win.webContents.capturePage();
      require('fs').writeFileSync(file, image.toPNG());
    } catch (e) {
      console.error('Capture failed:', e);
    }
    app.quit();
  }, delay);
}

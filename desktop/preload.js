// Tells the dashboard it is running in the desktop app, so Stage page panels use
// live <webview> pages instead of the reader-view fallback used in a browser.
const { contextBridge } = require('electron');

contextBridge.exposeInMainWorld('jarvisDesktop', {
  isDesktop: true,
  platform: process.platform,
});

import { useState, useEffect, useRef, useCallback } from 'react';
import { Link } from 'react-router-dom';
import { authedFetch } from './auth';
import './WorkspaceManager.css';

type ItemType = 'url' | 'vscode' | 'file' | 'app';
interface WsItem { type: ItemType; path: string }
interface Workspace { description?: string; items: WsItem[] }
type Data = Record<string, Workspace>

const PLACEHOLDER: Record<ItemType, string> = {
  url: 'https://...',
  vscode: '/path/to/project',
  file: '/path/to/file.pdf',
  app: 'Spotify / Slack / ...',
};

export default function WorkspaceManager() {
  const [data, setData] = useState<Data>({});
  const [selected, setSelected] = useState<string | null>(null);
  const [toast, setToast] = useState<{ msg: string; ok: boolean } | null>(null);
  const [loaded, setLoaded] = useState(false);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const saveTimer  = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    authedFetch('/api/workspaces')
      .then(r => r.json())
      .then((d: Data) => {
        setData(d);
        setSelected(Object.keys(d)[0] ?? null);
        setLoaded(true);
      })
      .catch(() => setLoaded(true));
  }, []);

  const showToast = (msg: string, ok = true) => {
    setToast({ msg, ok });
    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), 2600);
  };

  const persist = useCallback(async (d: Data, silent = false) => {
    try {
      const res = await authedFetch('/api/workspaces', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(d),
      });
      if (!silent) showToast(res.ok ? 'Saved ✓' : 'Save failed', res.ok);
    } catch {
      showToast('Save failed — is Jarvis running?', false);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const scheduleSave = (d: Data) => {
    if (saveTimer.current) clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(() => persist(d, true), 800);
  };

  const copyJSON = (d: Data) => {
    navigator.clipboard.writeText(JSON.stringify(d, null, 2));
    showToast('JSON copied ✓');
  };

  const mutate = (name: string, patch: Partial<Workspace>): Data => {
    const next = { ...data, [name]: { ...data[name], ...patch } };
    setData(next);
    return next;
  };

  const addWorkspace = () => {
    let name = 'new workspace';
    let i = 2;
    while (data[name]) name = `new workspace ${i++}`;
    const next = { ...data, [name]: { description: '', items: [] as WsItem[] } };
    setData(next);
    setSelected(name);
  };

  const deleteWorkspace = (name: string) => {
    if (!window.confirm(`Delete workspace "${name}"?`)) return;
    const next = { ...data };
    delete next[name];
    setData(next);
    setSelected(Object.keys(next)[0] ?? null);
    persist(next);
  };

  const rename = (oldName: string, newName: string) => {
    newName = newName.trim().toLowerCase();
    if (!newName || newName === oldName) return;
    if (data[newName]) { showToast('Name already taken', false); return; }
    const entries = Object.entries(data);
    const idx = entries.findIndex(([k]) => k === oldName);
    entries.splice(idx, 1, [newName, entries[idx][1]]);
    const next = Object.fromEntries(entries);
    setData(next);
    setSelected(newName);
    persist(next, true);
  };

  const ws = selected ? data[selected] : null;

  if (!loaded) {
    return <div className="wm-shell"><div className="wm-loading">Loading workspaces...</div></div>;
  }

  return (
    <div className="wm-shell">
      <header className="wm-header">
        <div className="wm-logo">J</div>
        <h1 className="wm-title">JARVIS <span>Workspace Manager</span></h1>
        <nav className="wm-header-nav">
          <Link to="/" className="wm-btn wm-btn-ghost">← Dashboard</Link>
          <button className="wm-btn wm-btn-ghost" onClick={() => copyJSON(data)}>⬇ Copy JSON</button>
          <button className="wm-btn wm-btn-primary" onClick={() => persist(data)}>✓ Save</button>
        </nav>
      </header>

      <div className="wm-layout">
        <aside className="wm-sidebar">
          <div className="wm-sidebar-label">Workspaces</div>
          {Object.keys(data).length === 0
            ? <div className="wm-sidebar-empty">No workspaces yet</div>
            : Object.entries(data).map(([name, w]) => (
                <div
                  key={name}
                  className={`wm-ws-item${selected === name ? ' active' : ''}`}
                  onClick={() => setSelected(name)}
                >
                  <div className="wm-ws-dot" />
                  <div className="wm-ws-name">{name}</div>
                  <div className="wm-ws-count">{(w.items ?? []).length}</div>
                </div>
              ))
          }
          <button className="wm-add-ws-btn" onClick={addWorkspace}>＋ New workspace</button>
        </aside>

        <main className="wm-main">
          {!selected || !ws ? (
            <div className="wm-empty-state">
              <div className="wm-empty-icon">⚡</div>
              <div className="wm-empty-title">No workspace selected</div>
              <div className="wm-empty-sub">Click a workspace or create a new one</div>
            </div>
          ) : (
            <div key={selected}>
              <div className="wm-ws-header">
                <div className="wm-ws-title-area">
                  <input
                    className="wm-name-input"
                    defaultValue={selected}
                    placeholder="workspace name"
                    onBlur={e => rename(selected, e.target.value)}
                    onKeyDown={e => { if (e.key === 'Enter') e.currentTarget.blur(); }}
                  />
                  <input
                    className="wm-desc-input"
                    value={ws.description ?? ''}
                    placeholder="Optional description..."
                    onChange={e => {
                      const next = mutate(selected, { description: e.target.value });
                      scheduleSave(next);
                    }}
                  />
                </div>
                <button className="wm-btn wm-btn-danger" onClick={() => deleteWorkspace(selected)}>
                  ✕ Delete
                </button>
              </div>

              <div className="wm-section-title">Items — opened when you say this workspace name</div>

              <div className="wm-items-list">
                {(ws.items ?? []).map((item, i) => (
                  <div key={i} className="wm-item-row">
                    <select
                      className={`wm-type-select wm-type-${item.type}`}
                      value={item.type}
                      onChange={e => {
                        const items = ws.items.map((it, idx) =>
                          idx === i ? { ...it, type: e.target.value as ItemType } : it
                        );
                        const next = mutate(selected, { items });
                        persist(next, true);
                      }}
                    >
                      <option value="url">url</option>
                      <option value="vscode">vscode</option>
                      <option value="file">file</option>
                      <option value="app">app</option>
                    </select>
                    <input
                      className="wm-path-input"
                      value={item.path}
                      placeholder={PLACEHOLDER[item.type]}
                      onChange={e => {
                        const items = ws.items.map((it, idx) =>
                          idx === i ? { ...it, path: e.target.value } : it
                        );
                        const next = mutate(selected, { items });
                        scheduleSave(next);
                      }}
                    />
                    <button
                      className="wm-btn wm-btn-danger"
                      onClick={() => {
                        const items = ws.items.filter((_, idx) => idx !== i);
                        const next = mutate(selected, { items });
                        persist(next);
                      }}
                    >✕</button>
                  </div>
                ))}
              </div>

              <button
                className="wm-add-item-btn"
                onClick={() => {
                  const items = [...(ws.items ?? []), { type: 'url' as ItemType, path: '' }];
                  mutate(selected, { items });
                }}
              >
                ＋ Add URL, file, app, or VSCode path
              </button>

              <div className="wm-tip">
                <strong>Voice trigger:</strong> Say <em>"open {selected}"</em> — Claude will fuzzy-match it.
              </div>

              <div className="wm-divider" />

              <div className="wm-json-panel">
                <div className="wm-json-header">
                  <span className="wm-json-label">workspaces.json preview</span>
                  <button className="wm-btn wm-btn-ghost wm-btn-sm" onClick={() => copyJSON(data)}>
                    Copy all
                  </button>
                </div>
                <pre className="wm-json-output">{JSON.stringify(data, null, 2)}</pre>
              </div>
            </div>
          )}
        </main>
      </div>

      {toast && (
        <div className={`wm-toast${toast.ok ? ' success' : ' error'}`}>{toast.msg}</div>
      )}
    </div>
  );
}

import { useState, useEffect, useRef, useCallback, DragEvent } from 'react';
import styles from './InputPanel.module.css';

interface InputFile {
  name: string;
  size: number;
  modified: number;
  type: 'image' | 'pdf' | 'code' | 'text' | 'file';
}

interface ArchiveFile {
  name: string;
  size: number;
  type: string;
}

interface ArchiveSession {
  session: string;
  files: ArchiveFile[];
}

const TYPE_ICONS: Record<string, string> = {
  image: '🖼',
  pdf:   '📄',
  code:  '💻',
  text:  '📝',
  file:  '📦',
};

function formatBytes(bytes: number) {
  if (bytes < 1024)       return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatSession(sess: string) {
  const m = sess.match(/session_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})/);
  if (!m) return sess;
  return `${m[1]}-${m[2]}-${m[3]}  ${m[4]}:${m[5]}:${m[6]}`;
}

export default function InputPanel() {
  const [files,         setFiles]         = useState<InputFile[]>([]);
  const [sessions,      setSessions]      = useState<ArchiveSession[]>([]);
  const [archiveOpen,   setArchiveOpen]   = useState(false);
  const [dragging,      setDragging]      = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const fetchFiles = useCallback(async () => {
    try {
      const res  = await fetch('/api/input');
      const data = await res.json();
      setFiles(data.files ?? []);
    } catch { /* server not ready */ }
  }, []);

  const fetchArchive = useCallback(async () => {
    try {
      const res  = await fetch('/api/input/archive');
      const data = await res.json();
      setSessions(data.sessions ?? []);
    } catch { /* ignore */ }
  }, []);

  useEffect(() => {
    fetchFiles();
  }, [fetchFiles]);

  useEffect(() => {
    if (archiveOpen) fetchArchive();
  }, [archiveOpen, fetchArchive]);

  async function uploadFile(file: File) {
    const fd = new FormData();
    fd.append('file', file);
    try {
      await fetch('/api/input/upload', { method: 'POST', body: fd });
      await fetchFiles();
    } catch { /* ignore */ }
  }

  function onFileInputChange(e: React.ChangeEvent<HTMLInputElement>) {
    const picked = Array.from(e.target.files ?? []);
    picked.forEach(uploadFile);
    e.target.value = '';
  }

  function onDrop(e: DragEvent<HTMLDivElement>) {
    e.preventDefault();
    setDragging(false);
    Array.from(e.dataTransfer.files).forEach(uploadFile);
  }

  async function restore(session: string, filename: string) {
    await fetch('/api/input/restore', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ session, filename }),
    });
    await fetchFiles();
    await fetchArchive();
  }

  return (
    <aside className={styles.panel}>
      <div className={styles.header}>
        <span className={styles.label}>INPUT FOLDER</span>
        <span className={styles.count}>{files.length}</span>
      </div>

      <div className={styles.body}>
        {/* Drop zone */}
        <div
          className={`${styles.dropZone} ${dragging ? styles.dropZoneOver : ''}`}
          onClick={() => fileInputRef.current?.click()}
          onDragOver={e => { e.preventDefault(); setDragging(true); }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
        >
          <span className={styles.dropIcon}>⊕</span>
          <span className={styles.dropLabel}>DROP FILES OR CLICK TO UPLOAD</span>
          <span className={styles.dropHint}>images · text · code · PDF · URLs</span>
          <input
            ref={fileInputRef}
            type="file"
            multiple
            style={{ display: 'none' }}
            onChange={onFileInputChange}
          />
        </div>

        {/* Current files */}
        {files.length === 0 ? (
          <div className={styles.empty}>
            <div className={styles.emptyIcon}>⊡</div>
            <div className={styles.emptyText}>Input folder is empty</div>
            <div className={styles.emptyHint}>Drop files here or say "process input folder"</div>
          </div>
        ) : (
          <div className={styles.section}>
            <div className={styles.sectionTitle}>PENDING</div>
            {files.map(f => (
              <div key={f.name} className={styles.fileRow}>
                <span className={styles.typeIcon}>{TYPE_ICONS[f.type] ?? '📦'}</span>
                <div className={styles.fileInfo}>
                  <div className={styles.fileName}>{f.name}</div>
                  <div className={styles.fileMeta}>
                    <span className={styles.badge}>{f.type.toUpperCase()}</span>
                    {formatBytes(f.size)}
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}

        {/* Archive toggle */}
        <button
          className={styles.archiveToggle}
          onClick={() => setArchiveOpen(o => !o)}
        >
          ARCHIVE
          <span className={`${styles.archiveChevron} ${archiveOpen ? styles.archiveChevronOpen : ''}`}>›</span>
        </button>

        {archiveOpen && (
          sessions.length === 0 ? (
            <div className={styles.empty} style={{ flex: 'none', padding: '12px 20px' }}>
              <div className={styles.emptyHint}>No archived sessions yet</div>
            </div>
          ) : (
            sessions.map(sess => (
              <div key={sess.session} className={styles.archiveSession}>
                <div className={styles.sessionLabel}>{formatSession(sess.session)}</div>
                {sess.files.map(f => (
                  <div key={f.name} className={styles.archiveRow}>
                    <span className={styles.typeIcon} style={{ fontSize: 12 }}>
                      {TYPE_ICONS[f.type] ?? '📦'}
                    </span>
                    <div className={styles.fileInfo}>
                      <div className={styles.fileName}>{f.name}</div>
                      <div className={styles.fileMeta}>{formatBytes(f.size)}</div>
                    </div>
                    <button
                      className={styles.restoreBtn}
                      onClick={() => restore(sess.session, f.name)}
                      title="Restore to input folder"
                    >
                      ↩
                    </button>
                  </div>
                ))}
              </div>
            ))
          )
        )}
      </div>
    </aside>
  );
}

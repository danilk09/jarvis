import { useState } from 'react';
import styles from './FilePanel.module.css';

export interface JarvisFile {
  name: string;
  content: string;
  time: string;
  size: string;
}

interface Props {
  files: JarvisFile[];
}

function ext(name: string) {
  return name.split('.').pop()?.toUpperCase() ?? 'TXT';
}

function FileViewer({ file, onClose }: { file: JarvisFile; onClose: () => void }) {
  const download = () => {
    const blob = new Blob([file.content], { type: 'text/plain' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = file.name;
    a.click();
  };

  return (
    <div className={styles.overlay} onClick={onClose}>
      <div className={styles.modal} onClick={e => e.stopPropagation()}>
        <div className={styles.modalHeader}>
          <span className={styles.modalTitle}>{file.name}</span>
          <div className={styles.modalActions}>
            <button className={styles.btn} onClick={download}>↓ EXPORT</button>
            <button className={styles.btn} onClick={onClose}>✕ CLOSE</button>
          </div>
        </div>
        <pre className={styles.modalContent}>{file.content}</pre>
      </div>
    </div>
  );
}

export default function FilePanel({ files }: Props) {
  const [selected, setSelected] = useState<JarvisFile | null>(null);

  return (
    <aside className={styles.panel}>
      <div className={styles.header}>
        <span className={styles.label}>OUTPUT FILES</span>
        <span className={styles.count}>{files.length}</span>
      </div>

      <div className={styles.list}>
        {files.length === 0 ? (
          <div className={styles.empty}>
            <div className={styles.emptyIcon}>◫</div>
            <div className={styles.emptyText}>No files yet</div>
            <div className={styles.emptyHint}>Ask JARVIS to write or code something</div>
          </div>
        ) : (
          [...files].reverse().map((f, i) => (
            <div
              key={`${f.name}-${i}`}
              className={`${styles.card} ${i === 0 ? styles.cardNew : ''}`}
              onClick={() => setSelected(f)}
            >
              <div className={styles.cardName}>{f.name}</div>
              <div className={styles.cardMeta}>
                <span className={styles.badge}>{ext(f.name)}</span>
                <span>{f.time}</span>
                <span>{f.size}</span>
              </div>
            </div>
          ))
        )}
      </div>

      {selected && <FileViewer file={selected} onClose={() => setSelected(null)} />}
    </aside>
  );
}

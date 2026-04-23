import { useState } from 'react';
import ReactMarkdown from 'react-markdown';
import styles from './FilePanel.module.css';

export interface JarvisFile {
  name: string;
  content: string;
  time: string;
  size: string;
}

interface Props {
  files: JarvisFile[];
  style?: React.CSSProperties;
}

function ext(name: string) {
  return name.split('.').pop()?.toUpperCase() ?? 'TXT';
}

function isMarkdown(name: string) {
  return /\.md$/i.test(name);
}

/* Convert markdown to Word-compatible HTML for .doc export */
function mdToWordHtml(md: string): string {
  const esc = (s: string) =>
    s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

  const inline = (s: string) =>
    esc(s)
      .replace(/\*\*\*(.+?)\*\*\*/g, '<b><i>$1</i></b>')
      .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
      .replace(/\*(.+?)\*/g, '<i>$1</i>')
      .replace(/`([^`]+)`/g, '<code style="font-family:Courier New;background:#f0f0f0">$1</code>')
      .replace(/\[(.+?)\]\((.+?)\)/g, '<a href="$2">$1</a>');

  const lines = md.split('\n');
  const out: string[] = [];
  let inCode = false;
  let codeAcc: string[] = [];
  let inList = false;
  let orderedList = false;

  const closeList = () => {
    if (inList) {
      out.push(orderedList ? '</ol>' : '</ul>');
      inList = false;
    }
  };

  for (const line of lines) {
    if (line.startsWith('```')) {
      if (!inCode) { inCode = true; codeAcc = []; }
      else {
        inCode = false;
        closeList();
        out.push(`<pre style="background:#f0f0f0;padding:8pt;font-family:Courier New;font-size:9pt">${esc(codeAcc.join('\n'))}</pre>`);
      }
      continue;
    }
    if (inCode) { codeAcc.push(line); continue; }

    const h3 = line.match(/^### (.+)/);
    const h2 = line.match(/^## (.+)/);
    const h1 = line.match(/^# (.+)/);
    if (h1) { closeList(); out.push(`<h1 style="color:#1a1a1a">${inline(h1[1])}</h1>`); continue; }
    if (h2) { closeList(); out.push(`<h2 style="color:#1a1a1a">${inline(h2[1])}</h2>`); continue; }
    if (h3) { closeList(); out.push(`<h3 style="color:#1a1a1a">${inline(h3[1])}</h3>`); continue; }

    if (/^[-*+] /.test(line)) {
      if (!inList || orderedList) { closeList(); out.push('<ul>'); inList = true; orderedList = false; }
      out.push(`<li>${inline(line.slice(2))}</li>`);
      continue;
    }
    if (/^\d+\. /.test(line)) {
      if (!inList || !orderedList) { closeList(); out.push('<ol>'); inList = true; orderedList = true; }
      out.push(`<li>${inline(line.replace(/^\d+\. /, ''))}</li>`);
      continue;
    }

    closeList();
    if (line.trim() === '') { out.push('<p>&nbsp;</p>'); continue; }
    if (/^---+$/.test(line) || /^___+$/.test(line)) { out.push('<hr/>'); continue; }
    out.push(`<p style="margin:4pt 0">${inline(line)}</p>`);
  }
  closeList();
  return out.join('\n');
}

function downloadWord(name: string, content: string) {
  const baseName = name.replace(/\.[^.]+$/, '');
  const bodyHtml = isMarkdown(name) ? mdToWordHtml(content) : `<pre>${content.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}</pre>`;
  const html = `<!DOCTYPE html>
<html xmlns:o='urn:schemas-microsoft-com:office:office'
      xmlns:w='urn:schemas-microsoft-com:office:word'
      xmlns='http://www.w3.org/TR/REC-html40'>
<head><meta charset='utf-8'>
<!--[if gte mso 9]><xml>
 <w:WordDocument>
  <w:View>Print</w:View>
  <w:DoNotOptimizeForBrowser/>
 </w:WordDocument>
</xml><![endif]-->
<style>
body{font-family:'Times New Roman',serif;font-size:12pt;color:#1a1a1a;margin:0.12in}
div.content{width:100%}
p{margin:4pt 0}
</style>
</head><body><div class='content'>${bodyHtml}</div></body></html>`;
  const blob = new Blob([html], { type: 'application/msword' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `${baseName}.doc`;
  a.click();
}

function downloadRaw(file: JarvisFile) {
  const blob = new Blob([file.content], { type: 'text/plain' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = file.name;
  a.click();
}

function FileViewer({ file, onClose }: { file: JarvisFile; onClose: () => void }) {
  const md = isMarkdown(file.name);

  return (
    <div className={styles.overlay} onClick={onClose}>
      <div className={styles.modal} onClick={e => e.stopPropagation()}>
        <div className={styles.modalHeader}>
          <span className={styles.modalTitle}>{file.name}</span>
          <div className={styles.modalActions}>
            {md && (
              <button className={styles.btn} onClick={() => downloadWord(file.name, file.content)}>
                ↓ WORD
              </button>
            )}
            <button className={styles.btn} onClick={() => downloadRaw(file)}>
              ↓ {md ? 'MARKDOWN' : 'EXPORT'}
            </button>
            <button className={styles.btn} onClick={onClose}>✕ CLOSE</button>
          </div>
        </div>

        {md ? (
          <div className={styles.markdownContent}>
            <ReactMarkdown>{file.content}</ReactMarkdown>
          </div>
        ) : (
          <pre className={styles.modalContent}>{file.content}</pre>
        )}
      </div>
    </div>
  );
}

export default function FilePanel({ files, style }: Props) {
  const [selected, setSelected] = useState<JarvisFile | null>(null);

  return (
    <aside className={styles.panel} style={style}>
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

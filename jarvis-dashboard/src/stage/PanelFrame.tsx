import { ReactNode } from 'react';
import type { Panel } from './StageContext';

const KIND_ICON: Record<string, string> = {
  page: '◎', summary: '≣', file: '‹›', image: '▣', note: '✎', map: '◍', code: '⌘',
};

const KIND_NAME: Record<string, string> = {
  page: 'PAGE', summary: 'KEY POINTS', file: 'FILE', image: 'IMAGE', note: 'NOTE', map: 'GLOBE', code: 'CLAUDE CODE',
};

interface Props {
  panel: Panel;
  number: number;
  focused: boolean;
  marked: boolean;          // Jarvis is pointing at something in this panel
  onFocus: () => void;
  onClose: () => void;
  children: ReactNode;
}

// Chrome around every Stage panel: a drag handle with the panel's number (what you
// say to refer to it: "close panel 2"), its kind and title, and focus / close buttons.
export default function PanelFrame({ panel, number, focused, marked, onFocus, onClose, children }: Props) {
  return (
    <div className={`panel panel-${panel.kind} ${focused ? 'panelFocused' : ''} ${marked ? 'panelMarked' : ''}`}>
      <div className="panelHead">
        <div className="panelDrag" title="Drag to move — drop onto another panel to swap them">
          <span className="panelNum">{number}</span>
          <span className="panelKind"><span className="panelIcon">{KIND_ICON[panel.kind]}</span>{KIND_NAME[panel.kind]}</span>
          <span className="panelTitle" title={panel.title}>{panel.title}</span>
        </div>
        <div className="panelActions">
          <button className="panelBtn" title={focused ? 'Show everything' : 'Focus this panel'} onClick={onFocus}>
            {focused ? '⊞' : '⤢'}
          </button>
          <button className="panelBtn panelBtnClose" title="Close" onClick={onClose}>✕</button>
        </div>
      </div>
      <div className="panelBody">{children}</div>
    </div>
  );
}

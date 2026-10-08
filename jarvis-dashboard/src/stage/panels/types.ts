import type { Highlight, Panel } from '../StageContext';

export interface PanelProps {
  panel: Panel;
  /** The latest highlight Jarvis placed in this panel, if any. */
  highlight: Highlight | null;
  /** True while the Stage is hidden behind the dashboard. */
  hidden?: boolean;
  /** Other panels (a summary needs its source page's highlights). */
  all?: Panel[];
  highlights?: Highlight[];
}

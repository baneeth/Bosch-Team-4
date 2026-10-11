import React, { useEffect, useRef } from 'react';

const tone = (l) => {
  if (/INJECTED|ABORT|breach|NOT recovered|refus|failed|Error/i.test(l)) return 'bad';
  if (/COMPLETED|restored|Ready after|cleaned up|ok\b/i.test(l)) return 'good';
  if (/guard:|observing|measuring|pre-check|plan|adapter|planner|analyst/i.test(l)) return 'info';
  return '';
};

export default function Terminal({ lines, placeholder = 'No experiment running. Pick a scenario and inject.', height = 260, grow }) {
  const ref = useRef(null);
  useEffect(() => { if (ref.current) ref.current.scrollTop = ref.current.scrollHeight; }, [lines.length]);
  return (
    <div className={`terminal ${grow ? 'grow' : ''}`} ref={ref} style={{ height }}>
      {lines.length === 0 && <div className="line dim">{placeholder}</div>}
      {lines.map((l, i) => (
        <div key={i} className={`line ${tone(l)} ${/^\s+(pre|fault|recovery)\s+t\+/.test(l) ? 'prog' : ''}`}>{l || ' '}</div>
      ))}
    </div>
  );
}

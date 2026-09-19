import { useEffect, useId, useState } from 'react';
import { HelpCircle, X } from 'lucide-react';
import { HELP, type HelpKey } from '../help/helpContent';

/**
 * 見出しの横に置く「？ 使い方」ボタン。押すと使い方の吹き出しが出て、もう一度押すと閉じる。
 * 吹き出しは画面に固定で出すので、見出しの置き場所（狭いヘッダーなど）に左右されない。
 */
export default function HelpTip({ helpKey }: { helpKey: HelpKey }) {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const entry = HELP[helpKey];

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open]);

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-controls={panelId}
        title={open ? '使い方を閉じる' : 'この画面の使い方'}
        className={`inline-flex items-center gap-1 shrink-0 rounded-full px-2 py-0.5 text-xs font-bold border-2 transition-colors ${
          open
            ? 'bg-amber-400 border-amber-500 text-white'
            : 'bg-amber-50 border-amber-400 text-amber-700 hover:bg-amber-100'
        }`}
      >
        <HelpCircle size={16} />
        <span className="hidden sm:inline">使い方</span>
      </button>
      {open && (
        <div
          id={panelId}
          role="dialog"
          aria-label={entry.title}
          className="fixed z-[1000] left-1/2 -translate-x-1/2 top-16 w-[min(92vw,36rem)] max-h-[75vh] overflow-y-auto rounded-xl border-2 border-amber-300 bg-white shadow-2xl text-left font-normal"
        >
          <div className="sticky top-0 flex items-start justify-between gap-2 bg-amber-50 border-b border-amber-200 px-4 py-3">
            <div>
              <p className="text-base font-bold text-gray-800">{entry.title}</p>
              <p className="mt-0.5 text-sm text-gray-600">{entry.lead}</p>
            </div>
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="p-1 rounded hover:bg-amber-100 text-gray-500"
              title="閉じる"
            >
              <X size={18} />
            </button>
          </div>
          <div className="px-4 py-3 space-y-3">
            {entry.sections.map((section) => (
              <div key={section.heading}>
                <p className="text-sm font-bold text-amber-800">{section.heading}</p>
                <ul className="mt-1 space-y-1 list-disc pl-5 text-sm text-gray-700 leading-relaxed">
                  {section.points.map((point) => (
                    <li key={point}>{point}</li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </div>
      )}
    </>
  );
}

/** 画面の上に常に出しておく注意書き */
export function HelpNotice({ children }: { children: string }) {
  return (
    <div className="mb-4 flex items-start gap-2 rounded-lg border-2 border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900">
      <HelpCircle size={18} className="mt-0.5 shrink-0 text-amber-600" />
      <p className="leading-relaxed">{children}</p>
    </div>
  );
}

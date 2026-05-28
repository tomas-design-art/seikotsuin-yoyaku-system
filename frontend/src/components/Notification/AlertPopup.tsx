import { X } from 'lucide-react';

interface Toast {
  id: string;
  message: string;
  type: 'info' | 'warning' | 'error' | 'incoming';
  persistent: boolean;
}

interface AlertPopupProps {
  toasts: Toast[];
  onDismiss: (id: string) => void;
}

export default function AlertPopup({ toasts, onDismiss }: AlertPopupProps) {
  if (toasts.length === 0) return null;

  return (
    <div className="fixed top-4 right-4 z-50 space-y-2 max-w-sm">
      {toasts.map((toast) => {
        const bgColor =
          toast.type === 'error'
            ? 'bg-red-500'
            : toast.type === 'warning'
            ? 'bg-yellow-500'
            : toast.type === 'incoming'
            ? 'bg-green-600'
            : 'bg-blue-500';

        return (
          <div
            key={toast.id}
            className={`${bgColor} text-white px-4 py-3 rounded-lg shadow-lg flex items-start gap-2 animate-slide-in`}
          >
            <span className="flex-1 text-sm">{toast.message}</span>
            <button onClick={() => onDismiss(toast.id)} className="hover:opacity-75">
              <X size={16} />
            </button>
          </div>
        );
      })}
    </div>
  );
}

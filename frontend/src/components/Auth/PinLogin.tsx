import { useState, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../../hooks/useAuth';
import { staffLogin } from '../../api/client';

interface PinLoginProps {
    onSuccess?: () => void;
    title?: string;
    subtitle?: string;
    verifyOnly?: boolean;
}

export default function PinLogin({ onSuccess, title = '予約管理システム', subtitle = 'PINを入力してください', verifyOnly = false }: PinLoginProps) {
    const [pin, setPin] = useState('');
    const [error, setError] = useState('');
    const [loading, setLoading] = useState(false);
    const { staffLoginAction } = useAuth();
    const navigate = useNavigate();

    const handleDigit = useCallback((d: string) => {
        setError('');
        setPin((prev) => {
            if (prev.length >= 4) return prev;
            return prev + d;
        });
    }, []);

    const handleBackspace = useCallback(() => {
        setError('');
        setPin((prev) => prev.slice(0, -1));
    }, []);

    // Auto-submit when 4 digits entered
    useEffect(() => {
        if (pin.length === 4 && !loading) {
            setLoading(true);
            const authPromise = verifyOnly
                ? staffLogin(pin).then(() => true).catch(() => false)
                : staffLoginAction(pin);
            authPromise.then((ok) => {
                if (ok) {
                    if (onSuccess) onSuccess();
                    else navigate('/', { replace: true });
                } else {
                    setError('PINが正しくないか、一時的に制限されています');
                    setPin('');
                }
                setLoading(false);
            });
        }
    }, [pin, loading, staffLoginAction, navigate, onSuccess, verifyOnly]);

    // Keyboard input
    useEffect(() => {
        const handleKey = (e: KeyboardEvent) => {
            if (e.key >= '0' && e.key <= '9') handleDigit(e.key);
            else if (e.key === 'Backspace') handleBackspace();
        };
        window.addEventListener('keydown', handleKey);
        return () => window.removeEventListener('keydown', handleKey);
    }, [handleDigit, handleBackspace]);

    const dots = Array.from({ length: 4 }, (_, i) => (
        <div
            key={i}
            className={`w-4 h-4 rounded-full mx-2 transition-all ${i < pin.length ? 'bg-blue-600 scale-110' : 'bg-gray-300'
                }`}
        />
    ));

    const numpad = [
        ['1', '2', '3'],
        ['4', '5', '6'],
        ['7', '8', '9'],
        ['←', '0', '✓'],
    ];

    return (
        <div className="min-h-screen flex items-center justify-center bg-gradient-to-b from-blue-50 to-gray-100">
            <div className="bg-white rounded-2xl shadow-xl p-8 w-80">
                <div className="text-center mb-6">
                    <div className="text-4xl mb-2">🦴</div>
                    <h1 className="text-lg font-bold text-gray-800">{title}</h1>
                    <p className="text-sm text-gray-500 mt-1">{subtitle}</p>
                </div>

                {/* PIN dots */}
                <div className="flex justify-center mb-6">{dots}</div>

                {/* Error */}
                {error && (
                    <p className="text-center text-red-500 text-sm mb-4 animate-pulse">{error}</p>
                )}

                {/* Loading */}
                {loading && (
                    <p className="text-center text-blue-500 text-sm mb-4">認証中...</p>
                )}

                {/* Numpad */}
                <div className="grid grid-cols-3 gap-2">
                    {numpad.flat().map((key) => (
                        <button
                            key={key}
                            onClick={() => {
                                if (key === '←') handleBackspace();
                                else if (key === '✓') { /* auto-submit handles this */ }
                                else handleDigit(key);
                            }}
                            disabled={loading}
                            className={`h-14 rounded-xl text-xl font-semibold transition-all
                ${key === '←'
                                    ? 'bg-gray-200 text-gray-600 hover:bg-gray-300 active:bg-gray-400'
                                    : key === '✓'
                                        ? 'bg-blue-100 text-blue-600 hover:bg-blue-200 active:bg-blue-300'
                                        : 'bg-gray-100 text-gray-800 hover:bg-gray-200 active:bg-gray-300'}
                disabled:opacity-50`}
                        >
                            {key}
                        </button>
                    ))}
                </div>
            </div>
        </div>
    );
}

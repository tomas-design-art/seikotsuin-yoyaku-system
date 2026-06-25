import { useState, useEffect } from 'react';
import { X } from 'lucide-react';
import { useAuth } from '../../hooks/useAuth';

interface Props {
    isOpen: boolean;
    onClose: () => void;
    onSuccess: () => void;
}

export default function AdminLoginModal({ isOpen, onClose, onSuccess }: Props) {
    const [username, setUsername] = useState('');
    const [password, setPassword] = useState('');
    const [error, setError] = useState('');
    const [loading, setLoading] = useState(false);
    const { adminLoginAction } = useAuth();

    useEffect(() => {
        if (isOpen) {
            setUsername('');
            setPassword('');
            setError('');
        }
    }, [isOpen]);

    if (!isOpen) return null;

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        setError('');
        setLoading(true);
        const ok = await adminLoginAction(username, password);
        setLoading(false);
        if (ok) {
            onSuccess();
        } else {
            setError('IDまたはパスワードが正しくありません');
        }
    };

    return (
        <div className="fixed inset-0 bg-black bg-opacity-40 flex items-center justify-center z-50">
            <div className="bg-white rounded-lg shadow-2xl w-96 p-6">
                <div className="flex items-center justify-between mb-4">
                    <h2 className="text-lg font-bold text-gray-800">🔒 管理者ログイン</h2>
                    <button onClick={onClose} className="text-gray-400 hover:text-gray-600">
                        <X size={20} />
                    </button>
                </div>
                <p className="text-sm text-gray-500 mb-4">
                    この操作には管理者権限が必要です
                </p>
                <form onSubmit={handleSubmit} className="space-y-3">
                    <div>
                        <label htmlFor="admin-username" className="block text-sm font-medium text-gray-700 mb-1">管理者ID</label>
                        <input
                            id="admin-username"
                            name="username"
                            type="text"
                            value={username}
                            onChange={(e) => setUsername(e.target.value)}
                            className="w-full border rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-blue-500 focus:outline-none"
                            autoFocus
                            autoComplete="username"
                        />
                    </div>
                    <div>
                        <label htmlFor="admin-password" className="block text-sm font-medium text-gray-700 mb-1">パスワード</label>
                        <input
                            id="admin-password"
                            name="password"
                            type="password"
                            value={password}
                            onChange={(e) => setPassword(e.target.value)}
                            className="w-full border rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-blue-500 focus:outline-none"
                            autoComplete="current-password"
                        />
                    </div>
                    {error && <p className="text-red-500 text-sm">{error}</p>}
                    <button
                        type="submit"
                        disabled={loading || !username || !password}
                        className="w-full bg-blue-600 text-white py-2 rounded-lg text-sm font-medium hover:bg-blue-700 disabled:opacity-50 transition"
                    >
                        {loading ? '認証中...' : 'ログイン'}
                    </button>
                </form>
            </div>
        </div>
    );
}

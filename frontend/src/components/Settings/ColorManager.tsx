import HelpTip from '../HelpTip';
import { useState, useEffect } from 'react';
import { Plus, Pencil, Trash2, Check, X } from 'lucide-react';
import type { ReservationColor } from '../../types';
import { getReservationColors, createReservationColor, updateReservationColor, deleteReservationColor } from '../../api/client';
import { extractErrorMessage } from '../../utils/errorUtils';

const FIXED_WARNING_COLORS = [
    { name: '競合エラー', color_code: '#DC2626' },
    { name: 'キャンセル申請', color_code: '#9CA3AF', extra: 'opacity:0.7 / line-through / dashed' },
    { name: '変更申請', color_code: '#EAB308' },
    { name: '仮確保(HOLD)', color_code: '#8B5CF6' },
    { name: '仮予約', color_code: '#EAB308' },
];

export default function ColorManager() {
    const [colors, setColors] = useState<ReservationColor[]>([]);
    const [editingId, setEditingId] = useState<number | null>(null);
    const [editName, setEditName] = useState('');
    const [editCode, setEditCode] = useState('#3B82F6');
    const [showAdd, setShowAdd] = useState(false);
    const [newName, setNewName] = useState('');
    const [newCode, setNewCode] = useState('#3B82F6');
    const [error, setError] = useState<string | null>(null);

    const fetchColors = () => {
        getReservationColors().then((res) => setColors(res.data ?? [])).catch(() => setColors([]));
    };

    useEffect(() => { fetchColors(); }, []);

    const handleAdd = async () => {
        if (!newName.trim()) return;
        setError(null);
        try {
            await createReservationColor({ name: newName, color_code: newCode, display_order: colors.length + 1 });
            setShowAdd(false);
            setNewName('');
            setNewCode('#3B82F6');
            fetchColors();
        } catch (err) {
            setError(extractErrorMessage(err, '色の追加に失敗しました'));
        }
    };

    const handleUpdate = async (id: number) => {
        setError(null);
        try {
            await updateReservationColor(id, { name: editName, color_code: editCode });
            setEditingId(null);
            fetchColors();
        } catch (err) {
            setError(extractErrorMessage(err, '色の更新に失敗しました'));
        }
    };

    const handleDelete = async (id: number) => {
        if (!confirm('この色を削除しますか？使用中の予約はデフォルト色に変更されます。')) return;
        setError(null);
        try {
            await deleteReservationColor(id);
            fetchColors();
        } catch (err) {
            setError(extractErrorMessage(err, '色の削除に失敗しました'));
        }
    };

    const handleSetDefault = async (id: number) => {
        setError(null);
        try {
            await updateReservationColor(id, { is_default: true });
            fetchColors();
        } catch (err) {
            setError(extractErrorMessage(err, 'デフォルト設定に失敗しました'));
        }
    };

    const startEdit = (c: ReservationColor) => {
        setEditingId(c.id);
        setEditName(c.name);
        setEditCode(c.color_code);
    };

    return (
        <div className="max-w-2xl mx-auto p-6">
            <div className="flex items-center justify-between mb-6">
                <h2 className="text-xl font-bold flex items-center gap-2">予約色設定 <HelpTip helpKey="colors" /></h2>
                <button
                    onClick={() => setShowAdd(true)}
                    className="flex items-center gap-1 px-3 py-2 bg-blue-500 text-white rounded text-sm hover:bg-blue-600"
                >
                    <Plus size={16} /> 追加
                </button>
            </div>

            {error && (
                <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded text-red-700 text-sm">{error}</div>
            )}

            {/* Add form */}
            {showAdd && (
                <div className="mb-4 p-4 bg-blue-50 border border-blue-200 rounded">
                    <div className="flex items-center gap-3">
                        <input
                            type="color"
                            value={newCode}
                            onChange={(e) => setNewCode(e.target.value)}
                            className="w-10 h-10 rounded cursor-pointer border-0"
                        />
                        <input
                            type="text"
                            value={newName}
                            onChange={(e) => setNewName(e.target.value)}
                            placeholder="色の名前（例: 保険診療）"
                            className="flex-1 border rounded px-3 py-2 text-sm"
                        />
                        <input
                            type="text"
                            value={newCode}
                            onChange={(e) => setNewCode(e.target.value)}
                            className="w-24 border rounded px-3 py-2 text-sm font-mono"
                            maxLength={7}
                        />
                        <button onClick={handleAdd} className="p-2 bg-green-500 text-white rounded hover:bg-green-600">
                            <Check size={16} />
                        </button>
                        <button onClick={() => setShowAdd(false)} className="p-2 bg-gray-300 rounded hover:bg-gray-400">
                            <X size={16} />
                        </button>
                    </div>
                </div>
            )}

            {/* User-configured colors */}
            <div className="space-y-2 mb-8">
                {colors.map((c) => (
                    <div key={c.id} className="flex items-center gap-3 p-3 bg-white border rounded hover:shadow-sm">
                        {editingId === c.id ? (
                            <>
                                <input
                                    type="color"
                                    value={editCode}
                                    onChange={(e) => setEditCode(e.target.value)}
                                    className="w-8 h-8 rounded cursor-pointer border-0"
                                />
                                <input
                                    type="text"
                                    value={editName}
                                    onChange={(e) => setEditName(e.target.value)}
                                    className="flex-1 border rounded px-2 py-1 text-sm"
                                />
                                <input
                                    type="text"
                                    value={editCode}
                                    onChange={(e) => setEditCode(e.target.value)}
                                    className="w-24 border rounded px-2 py-1 text-sm font-mono"
                                    maxLength={7}
                                />
                                <button onClick={() => handleUpdate(c.id)} className="p-1 text-green-600 hover:bg-green-50 rounded">
                                    <Check size={16} />
                                </button>
                                <button onClick={() => setEditingId(null)} className="p-1 text-gray-500 hover:bg-gray-100 rounded">
                                    <X size={16} />
                                </button>
                            </>
                        ) : (
                            <>
                                <span className="w-6 h-6 rounded-full flex-shrink-0" style={{ backgroundColor: c.color_code }} />
                                <span className="flex-1 font-medium text-sm">{c.name}</span>
                                <span className="text-xs font-mono text-gray-500">{c.color_code}</span>
                                {c.is_default ? (
                                    <span className="px-2 py-0.5 bg-blue-100 text-blue-700 text-xs rounded-full">デフォルト</span>
                                ) : (
                                    <button
                                        onClick={() => handleSetDefault(c.id)}
                                        className="px-2 py-0.5 text-xs text-gray-500 border rounded hover:bg-gray-50"
                                    >
                                        デフォルトに設定
                                    </button>
                                )}
                                <button onClick={() => startEdit(c)} className="p-1 text-gray-500 hover:bg-gray-100 rounded">
                                    <Pencil size={14} />
                                </button>
                                {!c.is_default && (
                                    <button onClick={() => handleDelete(c.id)} className="p-1 text-red-400 hover:bg-red-50 rounded">
                                        <Trash2 size={14} />
                                    </button>
                                )}
                            </>
                        )}
                    </div>
                ))}
            </div>

            {/* Fixed warning colors */}
            <div>
                <h3 className="text-sm font-semibold text-gray-500 mb-3">以下は固定（変更不可）</h3>
                <div className="space-y-2">
                    {FIXED_WARNING_COLORS.map((fc) => (
                        <div key={fc.name} className="flex items-center gap-3 p-3 bg-gray-50 border rounded opacity-70">
                            <span
                                className="w-6 h-6 rounded-full flex-shrink-0"
                                style={{
                                    backgroundColor: fc.color_code,
                                    ...('extra' in fc ? { border: '1.5px dashed #6B7280' } : {}),
                                }}
                            />
                            <span className="flex-1 text-sm text-gray-600">{fc.name}</span>
                            <span className="text-xs font-mono text-gray-400">
                                {fc.color_code}{'extra' in fc ? ` + ${fc.extra}` : ''}
                            </span>
                        </div>
                    ))}
                </div>
            </div>
        </div>
    );
}

import HelpTip from './HelpTip';
import { useState, useRef } from 'react';
import { Upload, X, ChevronRight, ChevronLeft, Check, AlertTriangle, Download, UserCheck, RefreshCw, SkipForward } from 'lucide-react';
import type { ImportPreviewResponse, ImportExecuteResponse, ImportDuplicate, RowAction } from '../types';
import { importPreview, importExecute, apiBaseURL } from '../api/client';
import { extractErrorMessage } from '../utils/errorUtils';

interface Props {
    onClose: () => void;
    onComplete: () => void;
}

type Step = 'upload' | 'mapping' | 'preview' | 'resolve' | 'result';

const FIELD_LABELS_SPLIT: Record<string, string> = {
    last_name: '姓',
    middle_name: 'ミドルネーム',
    first_name: '名',
    reading: '読み方',
    phone: '電話番号',
    birth_date: '生年月日',
    email: 'メール',
    notes: '備考',
};

const FIELD_LABELS_FULL: Record<string, string> = {
    full_name: 'フルネーム',
    reading: '読み方',
    phone: '電話番号',
    birth_date: '生年月日',
    email: 'メール',
    notes: '備考',
};

export default function PatientImport({ onClose, onComplete }: Props) {
    const [step, setStep] = useState<Step>('upload');
    const [file, setFile] = useState<File | null>(null);
    const [previewData, setPreviewData] = useState<ImportPreviewResponse | null>(null);
    const [mapping, setMapping] = useState<Record<string, number>>({});
    const [mode, setMode] = useState<'split' | 'full_name'>('split');
    const [result, setResult] = useState<ImportExecuteResponse | null>(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const fileRef = useRef<HTMLInputElement>(null);
    const [pendingDuplicates, setPendingDuplicates] = useState<ImportDuplicate[]>([]);
    const [rowActions, setRowActions] = useState<Record<number, RowAction>>({});

    const fieldLabels = mode === 'split' ? FIELD_LABELS_SPLIT : FIELD_LABELS_FULL;
    const columns = previewData?.columns || [];

    // Step 1: ファイルアップロード
    const handleFileSelect = async (f: File) => {
        setFile(f);
        setError(null);
        setLoading(true);
        try {
            const res = await importPreview(f);
            setPreviewData(res.data);
            setMapping(res.data.suggested_mapping);
            setMode(res.data.suggested_mode);
            setStep('mapping');
        } catch (err) {
            setError(extractErrorMessage(err, 'ファイルの解析に失敗しました'));
        } finally {
            setLoading(false);
        }
    };

    // Step 3: 取込実行
    const handleExecute = async (actions?: RowAction[]) => {
        if (!file) return;
        setLoading(true);
        setError(null);
        try {
            const res = await importExecute(file, mode, mapping, actions);
            setResult(res.data);
            if ((res.data.duplicates ?? []).length > 0 && !actions) {
                // 初回実行で重複あり → 解決ステップへ
                setPendingDuplicates(res.data.duplicates);
                // デフォルト: 全行スキップ
                const defaults: Record<number, RowAction> = {};
                for (const d of (res.data.duplicates ?? [])) {
                    defaults[d.row] = { row: d.row, action: 'skip' };
                }
                setRowActions(defaults);
                setStep('resolve');
            } else {
                setStep('result');
            }
        } catch (err) {
            setError(extractErrorMessage(err, '取り込みに失敗しました'));
        } finally {
            setLoading(false);
        }
    };

    // Step 3b: 重複解決後に再実行
    const handleResolveExecute = async () => {
        const actions = Object.values(rowActions);
        await handleExecute(actions);
    };

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
            <div className="bg-white rounded-lg shadow-xl w-full max-w-4xl max-h-[90vh] flex flex-col">
                {/* ヘッダー */}
                <div className="flex items-center justify-between px-6 py-4 border-b">
                    <h2 className="text-lg font-bold flex items-center gap-2">患者一括取り込み <HelpTip helpKey="patientImport" /></h2>
                    <button onClick={onClose} className="p-1 hover:bg-gray-100 rounded"><X size={20} /></button>
                </div>

                {/* ステップインジケーター */}
                <div className="flex items-center gap-2 px-6 py-3 bg-gray-50 border-b text-sm">
                    {(['upload', 'mapping', 'preview', 'resolve', 'result'] as Step[]).map((s, i) => (
                        <div key={s} className="flex items-center gap-1">
                            {i > 0 && <ChevronRight size={14} className="text-gray-300" />}
                            <span className={`px-2 py-0.5 rounded ${step === s ? 'bg-blue-500 text-white' : 'text-gray-400'}`}>
                                {['1.アップロード', '2.マッピング確認', '3.プレビュー', '4.重複解決', '5.結果'][i]}
                            </span>
                        </div>
                    ))}
                </div>

                {/* コンテンツ */}
                <div className="flex-1 overflow-auto p-6">
                    {error && (
                        <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded text-red-700 text-sm">{error}</div>
                    )}

                    {/* Step 1: アップロード */}
                    {step === 'upload' && (
                        <div className="flex flex-col items-center justify-center py-12 space-y-4">
                            <Upload size={48} className="text-gray-300" />
                            <p className="text-gray-600">CSV または Excel (.xlsx) ファイルをアップロードしてください</p>
                            <input
                                ref={fileRef}
                                type="file"
                                accept=".csv,.xlsx"
                                className="hidden"
                                onChange={(e) => {
                                    const f = e.target.files?.[0];
                                    if (f) handleFileSelect(f);
                                }}
                            />
                            <button
                                onClick={() => fileRef.current?.click()}
                                disabled={loading}
                                className="px-6 py-2 bg-blue-500 text-white rounded hover:bg-blue-600 disabled:opacity-50"
                            >
                                {loading ? '解析中...' : 'ファイルを選択'}
                            </button>
                            {file && <p className="text-sm text-gray-500">{file.name}</p>}

                            <div className="mt-6 pt-4 border-t w-full max-w-md">
                                <p className="text-xs text-gray-400 mb-2 text-center">テンプレートをダウンロード</p>
                                <div className="flex justify-center gap-3">
                                    <a
                                        href={`${apiBaseURL}/patients/import/template/csv`}
                                        download
                                        className="flex items-center gap-1 px-3 py-1.5 text-xs border rounded hover:bg-gray-50 text-gray-600"
                                    >
                                        <Download size={12} /> CSV テンプレート
                                    </a>
                                    <a
                                        href={`${apiBaseURL}/patients/import/template/xlsx`}
                                        download
                                        className="flex items-center gap-1 px-3 py-1.5 text-xs border rounded hover:bg-gray-50 text-gray-600"
                                    >
                                        <Download size={12} /> Excel テンプレート
                                    </a>
                                </div>
                            </div>
                        </div>
                    )}

                    {/* Step 2: マッピング確認 */}
                    {step === 'mapping' && previewData && (
                        <div className="space-y-4">
                            <div className="flex items-center justify-between">
                                <p className="text-sm text-gray-600">
                                    ファイル: <span className="font-medium">{file?.name}</span>
                                    （{previewData.total_data_rows}行検出）
                                </p>
                            </div>

                            {/* モード選択 */}
                            <div className="p-3 bg-gray-50 rounded border">
                                <p className="text-sm font-medium mb-2">登録モード</p>
                                <div className="flex gap-4">
                                    <label className="flex items-center gap-2 text-sm cursor-pointer">
                                        <input type="radio" checked={mode === 'split'} onChange={() => setMode('split')} />
                                        通常モード（姓・名 分割）
                                    </label>
                                    <label className="flex items-center gap-2 text-sm cursor-pointer">
                                        <input type="radio" checked={mode === 'full_name'} onChange={() => setMode('full_name')} />
                                        フルネームモード
                                    </label>
                                </div>
                                {previewData.splittable_hint && previewData.suggested_mode === 'full_name' && (
                                    <div className="mt-2 p-2 bg-blue-50 border border-blue-200 rounded text-xs text-blue-700">
                                        氏名列のデータにスペース区切りが検出されました。
                                        通常モードに切り替えると姓/名を自動分割して取り込めます。
                                        {mode === 'full_name' && (
                                            <button
                                                onClick={() => setMode('split')}
                                                className="ml-2 underline font-medium hover:text-blue-900"
                                            >
                                                通常モードに切替
                                            </button>
                                        )}
                                    </div>
                                )}
                            </div>

                            {/* マッピング設定 */}
                            <div className="border rounded overflow-hidden">
                                <table className="w-full text-sm">
                                    <thead className="bg-gray-50 border-b">
                                        <tr>
                                            <th className="px-4 py-2 text-left w-40">項目</th>
                                            <th className="px-4 py-2 text-left">割当列</th>
                                            <th className="px-4 py-2 text-left w-48">サンプル値</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {Object.entries(fieldLabels).map(([field, label]) => {
                                            const isRequired = (mode === 'split' && (field === 'last_name' || field === 'first_name'))
                                                || (mode === 'full_name' && field === 'full_name');
                                            const selectedIdx = mapping[field];
                                            const sampleVal = selectedIdx !== undefined && previewData.preview_rows[0]
                                                ? (() => {
                                                    // preview_rows はオリジナル mapping で抽出済み
                                                    // selectedIdx が columns 内の何番目かで値を取る
                                                    // 簡略化: preview_rows のフィールドキーから取る
                                                    return previewData.preview_rows[0][field] || '';
                                                })()
                                                : '';
                                            return (
                                                <tr key={field} className="border-b last:border-b-0">
                                                    <td className="px-4 py-2 font-medium">
                                                        {label}
                                                        {isRequired && <span className="text-red-500 ml-1">*</span>}
                                                    </td>
                                                    <td className="px-4 py-2">
                                                        <select
                                                            value={selectedIdx ?? -1}
                                                            onChange={(e) => {
                                                                const val = parseInt(e.target.value);
                                                                setMapping((prev) => {
                                                                    const next = { ...prev };
                                                                    if (val === -1) {
                                                                        delete next[field];
                                                                    } else {
                                                                        next[field] = val;
                                                                    }
                                                                    return next;
                                                                });
                                                            }}
                                                            className="w-full border rounded px-2 py-1"
                                                        >
                                                            <option value={-1}>-- 未割当 --</option>
                                                            {columns.map((col, idx) => (
                                                                <option key={idx} value={idx}>
                                                                    {String.fromCharCode(65 + idx)}列: {col}
                                                                </option>
                                                            ))}
                                                        </select>
                                                    </td>
                                                    <td className="px-4 py-2 text-gray-500 text-xs truncate max-w-[12rem]">
                                                        {sampleVal}
                                                    </td>
                                                </tr>
                                            );
                                        })}
                                    </tbody>
                                </table>
                            </div>
                        </div>
                    )}

                    {/* Step 3: プレビュー */}
                    {step === 'preview' && previewData && (
                        <div className="space-y-4">
                            <p className="text-sm text-gray-600">
                                先頭{Math.min((previewData.preview_rows ?? []).length, 10)}行のプレビュー
                                （モード: {mode === 'split' ? '通常' : 'フルネーム'}）
                            </p>
                            <div className="border rounded overflow-auto">
                                <table className="w-full text-xs">
                                    <thead className="bg-gray-50 border-b">
                                        <tr>
                                            <th className="px-3 py-2 text-left">#</th>
                                            {Object.entries(fieldLabels).map(([field, label]) => (
                                                mapping[field] !== undefined ? (
                                                    <th key={field} className="px-3 py-2 text-left">{label}</th>
                                                ) : null
                                            ))}
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {(previewData.preview_rows ?? []).slice(0, 10).map((row, i) => (
                                            <tr key={i} className="border-b">
                                                <td className="px-3 py-1.5 text-gray-400">{i + 1}</td>
                                                {Object.keys(fieldLabels).map((field) => (
                                                    mapping[field] !== undefined ? (
                                                        <td key={field} className="px-3 py-1.5">{row[field] || ''}</td>
                                                    ) : null
                                                ))}
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            </div>
                            <p className="text-xs text-gray-400">
                                全 {previewData.total_data_rows ?? 0} 行を取り込みます。重複候補がある場合は確認画面が表示されます。
                            </p>
                        </div>
                    )}

                    {/* Step 4: 重複解決 */}
                    {step === 'resolve' && pendingDuplicates.length > 0 && (
                        <div className="space-y-4">
                            <div className="p-3 bg-yellow-50 border border-yellow-200 rounded">
                                <p className="text-sm font-medium flex items-center gap-1">
                                    <AlertTriangle size={14} className="text-yellow-600" />
                                    {pendingDuplicates.length}件の重複候補が見つかりました
                                </p>
                                <p className="text-xs text-gray-500 mt-1">
                                    各行について処理方法を選択してください。
                                    {result && result.created_count > 0 && (
                                        <span className="text-green-600 ml-1">（{result.created_count}件は登録済み）</span>
                                    )}
                                </p>
                            </div>

                            {pendingDuplicates.map((dup) => {
                                const action = rowActions[dup.row];
                                return (
                                    <div key={dup.row} className="border rounded overflow-hidden">
                                        {/* 取込データ */}
                                        <div className="bg-gray-50 px-4 py-2 border-b flex items-center justify-between">
                                            <span className="text-sm font-medium">行 {dup.row}: {dup.data.last_name || dup.data.full_name || ''} {dup.data.first_name || ''}</span>
                                            <span className={`text-xs px-2 py-0.5 rounded ${action?.action === 'use_existing' ? 'bg-blue-100 text-blue-700'
                                                : action?.action === 'update_existing' ? 'bg-green-100 text-green-700'
                                                    : 'bg-gray-100 text-gray-500'
                                                }`}>
                                                {action?.action === 'use_existing' ? '採用' : action?.action === 'update_existing' ? '更新' : 'スキップ'}
                                            </span>
                                        </div>

                                        {/* 候補一覧 */}
                                        <div className="p-3 space-y-2">
                                            <p className="text-xs text-gray-400 mb-1">既存の候補患者:</p>
                                            {(dup.candidates ?? []).map((c) => (
                                                <div key={c.id} className={`border rounded p-3 text-xs ${action?.patient_id === c.id ? 'border-blue-400 bg-blue-50' : ''}`}>
                                                    <div className="flex items-start justify-between mb-2">
                                                        <div className="space-y-0.5">
                                                            <p className="font-medium text-sm">{c.name} <span className="text-gray-400 font-normal">{c.patient_number || ''}</span></p>
                                                            <p className="text-gray-500">
                                                                {c.reading && <span className="mr-3">読み: {c.reading}</span>}
                                                                {c.phone && <span className="mr-3">TEL: {c.phone}</span>}
                                                                {c.birth_date && <span>生年月日: {c.birth_date}</span>}
                                                            </p>
                                                            <p className="text-yellow-600">一致: {c.reasons.join(', ')}</p>
                                                        </div>
                                                        <div className="flex gap-1 shrink-0 ml-2">
                                                            <button
                                                                onClick={() => setRowActions(prev => ({ ...prev, [dup.row]: { row: dup.row, action: 'use_existing', patient_id: c.id } }))}
                                                                className={`flex items-center gap-0.5 px-2 py-1 rounded text-xs border ${action?.action === 'use_existing' && action.patient_id === c.id
                                                                    ? 'bg-blue-500 text-white border-blue-500'
                                                                    : 'hover:bg-blue-50 border-blue-300 text-blue-600'
                                                                    }`}
                                                                title="この候補を採用（新規登録しない）"
                                                            >
                                                                <UserCheck size={12} /> 採用
                                                            </button>
                                                            <button
                                                                onClick={() => setRowActions(prev => ({ ...prev, [dup.row]: { row: dup.row, action: 'update_existing', patient_id: c.id } }))}
                                                                className={`flex items-center gap-0.5 px-2 py-1 rounded text-xs border ${action?.action === 'update_existing' && action.patient_id === c.id
                                                                    ? 'bg-green-500 text-white border-green-500'
                                                                    : 'hover:bg-green-50 border-green-300 text-green-600'
                                                                    }`}
                                                                title="この候補を更新（電話/メール/読み方/備考）"
                                                            >
                                                                <RefreshCw size={12} /> 更新
                                                            </button>
                                                        </div>
                                                    </div>
                                                </div>
                                            ))}
                                            <button
                                                onClick={() => setRowActions(prev => ({ ...prev, [dup.row]: { row: dup.row, action: 'skip' } }))}
                                                className={`flex items-center gap-1 px-2 py-1 rounded text-xs border w-full justify-center ${action?.action === 'skip'
                                                    ? 'bg-gray-200 text-gray-700 border-gray-300'
                                                    : 'hover:bg-gray-50 border-gray-200 text-gray-400'
                                                    }`}
                                            >
                                                <SkipForward size={12} /> この行をスキップ
                                            </button>
                                        </div>
                                    </div>
                                );
                            })}
                        </div>
                    )}

                    {/* Step 5: 結果 */}
                    {step === 'result' && result && (
                        <div className="space-y-4">
                            <div className="grid grid-cols-3 gap-3">
                                <div className="p-4 bg-blue-50 rounded border text-center">
                                    <p className="text-2xl font-bold text-blue-700">{result.total_rows}</p>
                                    <p className="text-xs text-gray-500">総行数</p>
                                </div>
                                <div className="p-4 bg-green-50 rounded border text-center">
                                    <p className="text-2xl font-bold text-green-700">{result.created_count}</p>
                                    <p className="text-xs text-gray-500">新規登録</p>
                                </div>
                                <div className="p-4 bg-gray-50 rounded border text-center">
                                    <p className="text-2xl font-bold text-gray-500">{result.skipped_count}</p>
                                    <p className="text-xs text-gray-500">空行スキップ</p>
                                </div>
                            </div>
                            {(result.adopted_count > 0 || result.updated_count > 0 || result.duplicate_count > 0 || result.error_count > 0) && (
                                <div className="grid grid-cols-2 gap-3">
                                    {result.adopted_count > 0 && (
                                        <div className="p-4 bg-blue-50 rounded border text-center">
                                            <p className="text-2xl font-bold text-blue-600">{result.adopted_count}</p>
                                            <p className="text-xs text-gray-500">既存採用</p>
                                        </div>
                                    )}
                                    {result.updated_count > 0 && (
                                        <div className="p-4 bg-teal-50 rounded border text-center">
                                            <p className="text-2xl font-bold text-teal-600">{result.updated_count}</p>
                                            <p className="text-xs text-gray-500">既存更新</p>
                                        </div>
                                    )}
                                    {result.duplicate_count > 0 && (
                                        <div className="p-4 bg-yellow-50 rounded border text-center">
                                            <p className="text-2xl font-bold text-yellow-700">{result.duplicate_count}</p>
                                            <p className="text-xs text-gray-500">重複スキップ</p>
                                        </div>
                                    )}
                                    {result.error_count > 0 && (
                                        <div className="p-4 bg-red-50 rounded border text-center">
                                            <p className="text-2xl font-bold text-red-700">{result.error_count}</p>
                                            <p className="text-xs text-gray-500">エラー</p>
                                        </div>
                                    )}
                                </div>
                            )}

                            {(result.duplicates ?? []).length > 0 && (
                                <div>
                                    <h3 className="text-sm font-medium mb-2 flex items-center gap-1">
                                        <AlertTriangle size={14} className="text-yellow-500" /> 未解決の重複（スキップ扱い）
                                    </h3>
                                    <div className="border rounded max-h-40 overflow-auto">
                                        <table className="w-full text-xs">
                                            <thead className="bg-yellow-50 border-b sticky top-0">
                                                <tr>
                                                    <th className="px-3 py-1.5 text-left">行</th>
                                                    <th className="px-3 py-1.5 text-left">取込データ</th>
                                                    <th className="px-3 py-1.5 text-left">既存候補</th>
                                                </tr>
                                            </thead>
                                            <tbody>
                                                {result.duplicates.map((d, i) => (
                                                    <tr key={i} className="border-b">
                                                        <td className="px-3 py-1.5">{d.row}</td>
                                                        <td className="px-3 py-1.5">{d.data?.last_name || d.data?.full_name || ''} {d.data?.first_name || ''}</td>
                                                        <td className="px-3 py-1.5">
                                                            {(d.candidates ?? []).map((c) => `${c.name} (${c.patient_number})`).join(', ')}
                                                        </td>
                                                    </tr>
                                                ))}
                                            </tbody>
                                        </table>
                                    </div>
                                </div>
                            )}

                            {(result.errors ?? []).length > 0 && (
                                <div>
                                    <h3 className="text-sm font-medium mb-2 text-red-600">エラー詳細</h3>
                                    <div className="border border-red-200 rounded max-h-40 overflow-auto">
                                        <table className="w-full text-xs">
                                            <thead className="bg-red-50 border-b sticky top-0">
                                                <tr>
                                                    <th className="px-3 py-1.5 text-left">行</th>
                                                    <th className="px-3 py-1.5 text-left">理由</th>
                                                </tr>
                                            </thead>
                                            <tbody>
                                                {(result.errors ?? []).map((e, i) => (
                                                    <tr key={i} className="border-b">
                                                        <td className="px-3 py-1.5">{e.row}</td>
                                                        <td className="px-3 py-1.5">{e.reason}</td>
                                                    </tr>
                                                ))}
                                            </tbody>
                                        </table>
                                    </div>
                                </div>
                            )}
                        </div>
                    )}
                </div>

                {/* フッター */}
                <div className="flex items-center justify-between px-6 py-4 border-t bg-gray-50">
                    <div>
                        {step !== 'upload' && step !== 'result' && (
                            <button
                                onClick={() => {
                                    if (step === 'mapping') setStep('upload');
                                    if (step === 'preview') setStep('mapping');
                                    if (step === 'resolve') setStep('preview');
                                }}
                                className="flex items-center gap-1 px-4 py-2 border rounded hover:bg-gray-100 text-sm"
                            >
                                <ChevronLeft size={14} /> 戻る
                            </button>
                        )}
                    </div>
                    <div className="flex gap-2">
                        {step === 'mapping' && (
                            <button
                                onClick={() => setStep('preview')}
                                className="flex items-center gap-1 px-4 py-2 bg-blue-500 text-white rounded hover:bg-blue-600 text-sm"
                            >
                                プレビュー <ChevronRight size={14} />
                            </button>
                        )}
                        {step === 'preview' && (
                            <button
                                onClick={() => handleExecute()}
                                disabled={loading}
                                className="flex items-center gap-1 px-4 py-2 bg-green-600 text-white rounded hover:bg-green-700 disabled:opacity-50 text-sm"
                            >
                                {loading ? '取込中...' : (<><Check size={14} /> 取込実行</>)}
                            </button>
                        )}
                        {step === 'resolve' && (
                            <button
                                onClick={handleResolveExecute}
                                disabled={loading}
                                className="flex items-center gap-1 px-4 py-2 bg-green-600 text-white rounded hover:bg-green-700 disabled:opacity-50 text-sm"
                            >
                                {loading ? '処理中...' : (<><Check size={14} /> 重複を処理して再実行</>)}
                            </button>
                        )}
                        {step === 'result' && (
                            <button
                                onClick={() => { onComplete(); onClose(); }}
                                className="px-4 py-2 bg-blue-500 text-white rounded hover:bg-blue-600 text-sm"
                            >
                                閉じる
                            </button>
                        )}
                    </div>
                </div>
            </div>
        </div>
    );
}

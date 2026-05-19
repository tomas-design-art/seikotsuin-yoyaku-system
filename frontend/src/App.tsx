import { useState, useCallback, useEffect, useMemo, useRef } from 'react';
import { BrowserRouter, Routes, Route, Link, Navigate, useLocation, useNavigate } from 'react-router-dom';
import { Calendar, Users, Settings, Stethoscope, Menu as MenuIcon, Volume2, VolumeX, Palette, Bot, CalendarDays, CheckCircle, Lock, Unlock, LogOut, X } from 'lucide-react';
import TimeTable from './components/TimeTable/TimeTable';
import ReservationForm from './components/ReservationForm/ReservationForm';
import ReservationDetail from './components/ReservationDetail';
import PractitionerManager from './components/Settings/PractitionerManager';
import MenuManager from './components/Settings/MenuManager';
import ColorManager from './components/Settings/ColorManager';
import ChatbotSettings from './components/Settings/ChatbotSettings';
import WeeklyScheduleManager from './components/Settings/WeeklyScheduleManager';
import PractitionerScheduleManager from './components/Settings/PractitionerScheduleManager';
import SystemSettings from './components/Settings/SystemSettings';
import AuditLogViewer from './components/Settings/AuditLogViewer';
import PatientList from './components/PatientList';
import NotificationBell from './components/Notification/NotificationBell';
import AlertPopup from './components/Notification/AlertPopup';
import NotificationPanel from './components/Notification/NotificationPanel';
import SeriesExtensionModal from './components/Notification/SeriesExtensionModal';
import HotPepperSync from './components/HotPepperSync';
import PublicReserve from './components/PublicReserve';
import AdminLoginModal from './components/Auth/AdminLoginModal';
import PinLogin from './components/Auth/PinLogin';
import { AuthProvider, useAuth } from './hooks/useAuth';
import { useSSE } from './hooks/useSSE';
import { useNotification } from './hooks/useNotification';
import { rescheduleReservation, getSeries, getPendingSeriesAlerts } from './api/client';
import { extractErrorMessage } from './utils/errorUtils';
import type { Reservation, SeriesResponse } from './types';

type PendingRescheduleTarget = {
  practitionerId: number;
  startMinutes: number;
  date: Date;
};

const OPERATOR_STORAGE_KEY = 'operator';
const OPERATOR_CANDIDATES = ['上田', '出口', '時田'];

function NavLink({ to, children, locked }: { to: string; children: React.ReactNode; locked?: boolean }) {
  const location = useLocation();
  const active = location.pathname === to || (to !== '/' && location.pathname.startsWith(to));
  return (
    <Link to={to} className={`px-3 py-2 rounded text-sm font-medium flex items-center gap-1 ${active ? 'bg-blue-100 text-blue-700' : 'text-gray-600 hover:bg-gray-100'}`}>
      {children}
      {locked && <Lock size={12} className="text-gray-400" />}
    </Link>
  );
}

function AppContent() {
  const { role, adminLogout } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const isAdmin = role === 'admin';
  const operatorName = localStorage.getItem(OPERATOR_STORAGE_KEY) ?? '';
  const appShellRef = useRef<HTMLDivElement>(null);

  const [showReservationForm, setShowReservationForm] = useState(false);
  const [showNotificationPanel, setShowNotificationPanel] = useState(false);
  const [selectedReservation, setSelectedReservation] = useState<Reservation | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [formInitialData, setFormInitialData] = useState<{
    practitionerId?: number;
    date?: Date;
    startMinutes?: number;
    endMinutes?: number;
    isSingleClick?: boolean;
  }>({});

  // Admin login modal state
  const [showAdminLogin, setShowAdminLogin] = useState(false);
  const [adminLoginTarget, setAdminLoginTarget] = useState<string | null>(null);

  // Reschedule mode state
  const [reschedulingReservation, setReschedulingReservation] = useState<Reservation | null>(null);
  const [pendingRescheduleTarget, setPendingRescheduleTarget] = useState<PendingRescheduleTarget | null>(null);
  const [isSubmittingReschedule, setIsSubmittingReschedule] = useState(false);
  const [rescheduleSuccess, setRescheduleSuccess] = useState<string | null>(null);
  const [rescheduleError, setRescheduleError] = useState<string | null>(null);
  const [rescheduleDurationOffset, setRescheduleDurationOffset] = useState(0);

  // Series extension modal state
  const [seriesExtensionTarget, setSeriesExtensionTarget] = useState<SeriesResponse | null>(null);
  const [isTimeTableFullscreen, setIsTimeTableFullscreen] = useState(false);

  const { toasts, unreadCount, audioInitialized, enableAudio, addToast, removeToast, clearUnread } = useNotification();

  const handleSSEEvent = useCallback((event: { event_type: string; data: Record<string, unknown> }) => {
    const msg = (event.data.message as string) || event.event_type;
    if (event.event_type === 'series_expiring') {
      // シリーズ延長通知 — モーダルを表示
      const seriesId = event.data.series_id as number | undefined;
      if (seriesId) {
        getSeries(seriesId).then((res) => {
          setSeriesExtensionTarget(res.data);
        }).catch(() => {
          addToast(msg, 'warning');
        });
      } else {
        addToast(msg, 'warning');
      }
    } else if (event.event_type === 'conflict_detected') {
      addToast(msg, 'error');
    } else if (event.event_type === 'hold_expired') {
      addToast(msg, 'warning');
    } else if (event.event_type === 'hotpepper_sync_reminder') {
      addToast(msg, 'warning');
    } else {
      addToast(msg, 'info');
    }
    setRefreshKey((k) => k + 1);
  }, [addToast]);

  useSSE(handleSSEEvent);

  // 画面起動時: SSE で受け取れなかった未対応のシリーズ延長アラートをキャッチアップ
  useEffect(() => {
    getPendingSeriesAlerts()
      .then((res) => {
        const alerts = res.data ?? [];
        if (alerts.length > 0) {
          setSeriesExtensionTarget(alerts[0]); // 最新1件をモーダル表示
        }
      })
      .catch(() => { /* ignore */ });
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const refresh = () => setRefreshKey((k) => k + 1);

  const handleSlotClick = (practitionerId: number, startMinutes: number, date: Date) => {
    setFormInitialData({ practitionerId, date, startMinutes, endMinutes: startMinutes + 30, isSingleClick: true });
    setShowReservationForm(true);
  };

  const handleDragSelect = (practitionerId: number, startMinutes: number, endMinutes: number, date: Date) => {
    setFormInitialData({ practitionerId, date, startMinutes, endMinutes, isSingleClick: false });
    setShowReservationForm(true);
  };

  // Start reschedule mode from ReservationDetail
  const handleStartReschedule = (reservation: Reservation) => {
    const currentStart = new Date(reservation.start_time);
    setSelectedReservation(null); // close detail
    setReschedulingReservation(reservation);
    setPendingRescheduleTarget({
      practitionerId: reservation.practitioner_id,
      startMinutes: currentStart.getHours() * 60 + currentStart.getMinutes(),
      date: currentStart,
    });
    setRescheduleError(null);
    setRescheduleDurationOffset(0);
  };

  // Handle slot click while in reschedule mode
  const handleRescheduleSlotClick = (practitionerId: number, startMinutes: number, date: Date) => {
    if (!reschedulingReservation) return;
    setRescheduleError(null);
    setPendingRescheduleTarget({
      practitionerId,
      startMinutes,
      date,
    });
  };

  const pendingRescheduleLabel = useMemo(() => {
    if (!pendingRescheduleTarget || !reschedulingReservation) return null;
    const baseDurationMin = (new Date(reschedulingReservation.end_time).getTime() - new Date(reschedulingReservation.start_time).getTime()) / 60000;
    const durationMin = baseDurationMin + rescheduleDurationOffset;

    const startH = Math.floor(pendingRescheduleTarget.startMinutes / 60);
    const startM = pendingRescheduleTarget.startMinutes % 60;
    const endMinutes = pendingRescheduleTarget.startMinutes + durationMin;
    const endH = Math.floor(endMinutes / 60);
    const endM = endMinutes % 60;
    const date = pendingRescheduleTarget.date;
    const displayDate = `${date.getMonth() + 1}/${date.getDate()}`;
    const startTimeStr = `${String(startH).padStart(2, '0')}:${String(startM).padStart(2, '0')}`;
    const endTimeStr = `${String(endH).padStart(2, '0')}:${String(endM).padStart(2, '0')}`;
    const offsetLabel = rescheduleDurationOffset !== 0
      ? `（時間${rescheduleDurationOffset > 0 ? '+' : ''}${rescheduleDurationOffset}分）`
      : '';
    return `${displayDate} ${startTimeStr}〜${endTimeStr}${offsetLabel}`;
  }, [pendingRescheduleTarget, reschedulingReservation, rescheduleDurationOffset]);

  const confirmPendingReschedule = useCallback(async () => {
    if (!reschedulingReservation || !pendingRescheduleTarget || isSubmittingReschedule) return;

    const r = reschedulingReservation;
    const baseDurationMin = (new Date(r.end_time).getTime() - new Date(r.start_time).getTime()) / 60000;
    const durationMin = baseDurationMin + rescheduleDurationOffset;

    const startH = Math.floor(pendingRescheduleTarget.startMinutes / 60);
    const startM = pendingRescheduleTarget.startMinutes % 60;
    const endMinutes = pendingRescheduleTarget.startMinutes + durationMin;
    const endH = Math.floor(endMinutes / 60);
    const endM = endMinutes % 60;
    const date = pendingRescheduleTarget.date;
    const dateStr = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
    const startTimeStr = `${String(startH).padStart(2, '0')}:${String(startM).padStart(2, '0')}`;
    const endTimeStr = `${String(endH).padStart(2, '0')}:${String(endM).padStart(2, '0')}`;

    setRescheduleError(null);
    setIsSubmittingReschedule(true);
    try {
      await rescheduleReservation(r.id, {
        new_start_time: `${dateStr}T${startTimeStr}:00+09:00`,
        new_end_time: `${dateStr}T${endTimeStr}:00+09:00`,
        new_practitioner_id: pendingRescheduleTarget.practitionerId !== r.practitioner_id ? pendingRescheduleTarget.practitionerId : undefined,
      });
      setReschedulingReservation(null);
      setPendingRescheduleTarget(null);
      setRescheduleDurationOffset(0);
      setRescheduleSuccess('予約を変更しました');
      refresh();
      setTimeout(() => setRescheduleSuccess(null), 2000);
    } catch (err: unknown) {
      setRescheduleError(extractErrorMessage(err, '予約変更に失敗しました'));
    } finally {
      setIsSubmittingReschedule(false);
    }
  }, [reschedulingReservation, pendingRescheduleTarget, isSubmittingReschedule, rescheduleDurationOffset]);

  const cancelReschedule = () => {
    setReschedulingReservation(null);
    setPendingRescheduleTarget(null);
    setRescheduleError(null);
    setIsSubmittingReschedule(false);
    setRescheduleDurationOffset(0);
  };

  const exitTimeTableFullscreen = useCallback(async () => {
    setIsTimeTableFullscreen(false);
    if (document.fullscreenElement) {
      try {
        await document.exitFullscreen();
      } catch {
        // Browser may already be leaving fullscreen via Esc.
      }
    }
  }, []);

  const toggleTimeTableFullscreen = useCallback(async () => {
    if (isTimeTableFullscreen || document.fullscreenElement) {
      await exitTimeTableFullscreen();
      return;
    }

    setIsTimeTableFullscreen(true);
    try {
      await appShellRef.current?.requestFullscreen();
    } catch {
      // Keep the in-app fullscreen layout even if the browser denies fullscreen.
    }
  }, [exitTimeTableFullscreen, isTimeTableFullscreen]);

  useEffect(() => {
    const handleFullscreenChange = () => {
      if (!document.fullscreenElement) {
        setIsTimeTableFullscreen(false);
      }
    };
    document.addEventListener('fullscreenchange', handleFullscreenChange);
    return () => document.removeEventListener('fullscreenchange', handleFullscreenChange);
  }, []);

  useEffect(() => {
    if (location.pathname !== '/timetable' && isTimeTableFullscreen) {
      void exitTimeTableFullscreen();
    }
  }, [location.pathname, isTimeTableFullscreen, exitTimeTableFullscreen]);

  const showAppHeader = !(location.pathname === '/timetable' && isTimeTableFullscreen);

  const handleOperatorSwitch = () => {
    localStorage.removeItem(OPERATOR_STORAGE_KEY);
    adminLogout();
    navigate('/');
  };

  return (
    <div
      ref={appShellRef}
      className={`flex flex-col h-screen bg-gray-50 ${isTimeTableFullscreen ? 'fixed inset-0 z-[9999] overflow-hidden' : ''}`}
    >
      {/* Header */}
      {showAppHeader && (
        <header className="bg-white shadow-sm border-b z-20">
          <div className="flex items-center justify-between px-4 py-2">
            <div className="flex items-center gap-4">
              <h1 className="text-lg font-bold text-gray-900">🦴 予約管理</h1>
              <nav className="flex items-center gap-1">
                <NavLink to="/timetable"><Calendar size={16} className="inline mr-1" />タイムテーブル</NavLink>
                <NavLink to="/patients" locked={role !== 'staff' && role !== 'admin'}><Users size={16} className="inline mr-1" />患者</NavLink>
                <AdminNavLink to="/settings/practitioners" isAdmin={isAdmin} onRequireAdmin={setAdminLoginTarget}>
                  <Stethoscope size={16} className="inline mr-1" />施術者
                </AdminNavLink>
                <AdminNavLink to="/settings/menus" isAdmin={isAdmin} onRequireAdmin={setAdminLoginTarget}>
                  <MenuIcon size={16} className="inline mr-1" />メニュー
                </AdminNavLink>
                <AdminNavLink to="/settings/colors" isAdmin={isAdmin} onRequireAdmin={setAdminLoginTarget}>
                  <Palette size={16} className="inline mr-1" />色設定
                </AdminNavLink>
                <AdminNavLink to="/settings/chatbot" isAdmin={isAdmin} onRequireAdmin={setAdminLoginTarget}>
                  <Bot size={16} className="inline mr-1" />チャットボット
                </AdminNavLink>
                <AdminNavLink to="/settings/schedule" isAdmin={isAdmin} onRequireAdmin={setAdminLoginTarget}>
                  <CalendarDays size={16} className="inline mr-1" />院営業スケジュール
                </AdminNavLink>
                <NavLink to="/settings/practitioner-schedules"><CalendarDays size={16} className="inline mr-1" />職員勤務スケジュール</NavLink>
                <AdminNavLink to="/settings/audit-logs" isAdmin={isAdmin} onRequireAdmin={setAdminLoginTarget}>
                  <Settings size={16} className="inline mr-1" />監査ログ
                </AdminNavLink>
                <AdminNavLink to="/settings" isAdmin={isAdmin} onRequireAdmin={setAdminLoginTarget}>
                  <Settings size={16} className="inline mr-1" />設定
                </AdminNavLink>
                <NavLink to="/hotpepper">🔥 HP同期</NavLink>
              </nav>
            </div>
            <div className="flex items-center gap-2">
              <div className="flex items-center gap-1 text-sm text-gray-700 bg-gray-100 px-2 py-1 rounded">
                <Users size={14} />
                <span>{operatorName || '未選択'}</span>
              </div>
              {isAdmin && (
                <div className="flex items-center gap-1 text-sm text-green-700 bg-green-50 px-2 py-1 rounded">
                  <Unlock size={14} />
                  <span>管理者</span>
                </div>
              )}
              <button
                onClick={enableAudio}
                className={`p-2 rounded-full ${audioInitialized ? 'text-green-500' : 'text-gray-400 hover:text-gray-600'}`}
                title={audioInitialized ? '通知音ON' : 'クリックして通知音を有効化'}
              >
                {audioInitialized ? <Volume2 size={18} /> : <VolumeX size={18} />}
              </button>
              <NotificationBell
                unreadCount={unreadCount}
                onClick={() => { setShowNotificationPanel(!showNotificationPanel); clearUnread(); }}
              />
              <button
                onClick={handleOperatorSwitch}
                className="p-2 rounded-full text-gray-400 hover:text-red-500"
                title="操作者を切り替え"
              >
                <LogOut size={18} />
              </button>
            </div>
          </div>
        </header>
      )}

      {/* Main */}
      <main className={`flex-1 bg-gray-50 ${isTimeTableFullscreen ? 'overflow-hidden' : 'overflow-auto'}`}>
        <Routes>
          <Route path="/timetable" element={
            <TimeTable
              onSlotClick={handleSlotClick}
              onDragSelect={handleDragSelect}
              onReservationClick={setSelectedReservation}
              refreshKey={refreshKey}
              reschedulingReservation={reschedulingReservation}
              onRescheduleSlotClick={handleRescheduleSlotClick}
              onCancelReschedule={cancelReschedule}
              pendingRescheduleTarget={pendingRescheduleTarget}
              pendingRescheduleLabel={pendingRescheduleLabel}
              onConfirmReschedule={confirmPendingReschedule}
              canConfirmReschedule={!!pendingRescheduleTarget && !isSubmittingReschedule}
              isConfirmingReschedule={isSubmittingReschedule}
              rescheduleDurationOffset={rescheduleDurationOffset}
              onRescheduleDurationChange={(delta) => setRescheduleDurationOffset(prev => prev + delta)}
              isFullscreenMode={isTimeTableFullscreen}
              onToggleFullscreen={toggleTimeTableFullscreen}
              fullscreenRightControls={isTimeTableFullscreen ? (
                <>
                  <NotificationBell
                    unreadCount={unreadCount}
                    onClick={() => { setShowNotificationPanel(!showNotificationPanel); clearUnread(); }}
                  />
                  <button
                    onClick={() => { void exitTimeTableFullscreen(); }}
                    className="p-1.5 text-gray-500 hover:text-gray-700 hover:bg-gray-100 rounded"
                    title="全画面表示を終了"
                  >
                    <X size={16} />
                  </button>
                </>
              ) : null}
            />
          } />
          <Route path="/patients" element={<PatientAccessGate />} />
          <Route path="/settings/practitioners" element={<PractitionerManager />} />
          <Route path="/settings/menus" element={<MenuManager />} />
          <Route path="/settings/colors" element={<ColorManager />} />
          <Route path="/settings/chatbot" element={<ChatbotSettings />} />
          <Route path="/settings/schedule" element={<WeeklyScheduleManager />} />
          <Route path="/settings/practitioner-schedules" element={<PractitionerScheduleManager />} />
          <Route path="/settings/audit-logs" element={<AuditLogViewer />} />
          <Route path="/settings" element={<SystemSettings />} />
          <Route path="/hotpepper" element={<HotPepperSync />} />
          <Route path="/reserve" element={<PublicReserve />} />
          <Route path="*" element={<Navigate to="/timetable" replace />} />
        </Routes>
      </main>

      {location.pathname === '/timetable' && isTimeTableFullscreen && (
        <button
          type="button"
          onClick={() => { void exitTimeTableFullscreen(); }}
          className="fixed top-2 left-1/2 z-[10000] flex h-8 w-8 -translate-x-1/2 items-center justify-center rounded-full bg-white/90 text-gray-600 shadow border border-gray-200 hover:bg-white hover:text-gray-900"
          title="全画面表示を終了"
        >
          <X size={16} />
        </button>
      )}

      {/* Modals */}
      <ReservationForm
        isOpen={showReservationForm}
        onClose={() => setShowReservationForm(false)}
        onSuccess={refresh}
        initialData={formInitialData}
      />

      {selectedReservation && (
        <ReservationDetail
          reservation={selectedReservation}
          onClose={() => setSelectedReservation(null)}
          onUpdate={refresh}
          onStartReschedule={handleStartReschedule}
        />
      )}

      {showNotificationPanel && (
        <NotificationPanel onClose={() => setShowNotificationPanel(false)} />
      )}

      {/* Reschedule error */}
      {rescheduleError && (
        <div className="fixed top-4 left-1/2 -translate-x-1/2 z-[70] bg-red-500 text-white px-6 py-3 rounded-lg shadow-2xl flex items-center gap-2 cursor-pointer"
          onClick={() => setRescheduleError(null)}
        >
          <span className="font-medium">{rescheduleError}</span>
        </div>
      )}

      {/* Reschedule success popup */}
      {rescheduleSuccess && (
        <div className="fixed inset-0 flex items-center justify-center z-[70] pointer-events-none">
          <div className="bg-green-500 text-white px-6 py-3 rounded-lg shadow-2xl flex items-center gap-2 animate-bounce">
            <CheckCircle size={20} />
            <span className="font-medium">{rescheduleSuccess}</span>
          </div>
        </div>
      )}

      {/* Toast notifications */}
      <AlertPopup toasts={toasts} onDismiss={removeToast} />

      {/* Series extension modal */}
      {seriesExtensionTarget && (
        <SeriesExtensionModal
          series={seriesExtensionTarget}
          onClose={() => setSeriesExtensionTarget(null)}
          onAction={refresh}
        />
      )}

      {/* Admin login modal */}
      <AdminLoginModal
        isOpen={showAdminLogin || adminLoginTarget !== null}
        onClose={() => { setShowAdminLogin(false); setAdminLoginTarget(null); }}
        onSuccess={() => {
          setShowAdminLogin(false);
          if (adminLoginTarget) {
            navigate(adminLoginTarget);
          }
          setAdminLoginTarget(null);
        }}
      />
    </div>
  );
}

function PatientAccessGate() {
  const { role, loading } = useAuth();
  const [unlocked, setUnlocked] = useState(false);

  if (loading) {
    return <div className="p-6 text-sm text-gray-500">認証状態を確認しています...</div>;
  }

  if (!unlocked) {
    return (
      <div className="min-h-full flex items-center justify-center p-6">
        <PinLogin
          title="患者情報ロック"
          subtitle="患者ページを開くにはスタッフPINを入力してください"
          verifyOnly={role === 'admin'}
          onSuccess={() => setUnlocked(true)}
        />
      </div>
    );
  }

  return <PatientList />;
}

function AdminNavLink({ to, isAdmin, onRequireAdmin, children }: {
  to: string;
  isAdmin: boolean;
  onRequireAdmin: (path: string) => void;
  children: React.ReactNode;
}) {
  const location = useLocation();
  const active = location.pathname === to || (to !== '/' && location.pathname.startsWith(to));

  const handleClick = (e: React.MouseEvent) => {
    if (!isAdmin) {
      e.preventDefault();
      onRequireAdmin(to);
    }
  };

  return (
    <Link
      to={isAdmin ? to : '#'}
      onClick={handleClick}
      className={`px-3 py-2 rounded text-sm font-medium flex items-center gap-1 ${active ? 'bg-blue-100 text-blue-700' : 'text-gray-600 hover:bg-gray-100'}`}
    >
      {children}
      {!isAdmin && <Lock size={12} className="text-gray-400" />}
    </Link>
  );
}

function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <AuthGate />
      </AuthProvider>
    </BrowserRouter>
  );
}

function UserSelectPage() {
  const navigate = useNavigate();

  const handleSelect = (name: string) => {
    localStorage.setItem(OPERATOR_STORAGE_KEY, name);
    navigate('/timetable');
  };

  return (
    <div className="min-h-screen bg-gray-50 flex items-center justify-center px-4">
      <div className="w-full max-w-xl bg-white rounded-2xl shadow-sm border p-6 md:p-10">
        <h1 className="text-2xl md:text-3xl font-bold text-center text-gray-900 mb-2">操作者を選択してください</h1>
        <p className="text-center text-gray-500 mb-8">選択後に予約管理画面へ進みます</p>
        <div className="flex flex-col gap-4">
          {OPERATOR_CANDIDATES.map((name) => (
            <button
              key={name}
              onClick={() => handleSelect(name)}
              className="w-full h-20 rounded-xl bg-green-700 hover:bg-green-800 active:scale-[0.99] text-white text-2xl font-semibold transition"
            >
              {name}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

function AuthGate() {
  const location = useLocation();
  if (location.pathname.startsWith('/reserve')) return <PublicReserve />;
  if (location.pathname === '/') return <UserSelectPage />;
  const operator = localStorage.getItem(OPERATOR_STORAGE_KEY);
  if (!operator) return <Navigate to="/" replace />;
  return <AppContent />;
}

export default App

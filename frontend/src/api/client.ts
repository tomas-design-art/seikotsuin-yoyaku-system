import axios from 'axios';
import type {
  Practitioner,
  Patient,
  PatientPageResponse,
  CandidateResponse,
  ImportPreviewResponse,
  ImportExecuteResponse,
  RowAction,
  Menu,
  Reservation,
  ReservationCreate,
  Setting,
  Notification,
  ReservationColor,
  ChatResponse,
  WeeklySchedule,
  PractitionerSchedule,
  ScheduleOverride,
  PractitionerDayStatus,
  AffectedReservation,
  DateOverride,
  BusinessHoursDay,
  UnavailableTime,
  BulkReservationCreate,
  BulkReservationResult,
  SeriesResponse,
  SeriesExtendRequest,
  SeriesModifyRequest,
  SeriesBulkEditRequest,
  WebReserveRequest,
  WebReserveSuccess,
  WebReserveConflict,
  AuditLog,
} from '../types';

// @ts-ignore
const baseURL = import.meta.env.VITE_API_URL || '/api';
export const apiBaseURL = baseURL;

const api = axios.create({
  baseURL: baseURL,
  headers: { 'Content-Type': 'application/json' },
});

const OPERATOR_STORAGE_KEY = 'operator';

// Auth interceptor
api.interceptors.request.use((config) => {
  const token = localStorage.getItem('auth_token');
  const operator = localStorage.getItem(OPERATOR_STORAGE_KEY)?.trim();
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  if (operator) {
    // X-Operator は非ASCII（日本語）を含む可能性があるため URL エンコードして送る
    // （XHR の setRequestHeader は ISO-8859-1 しか許可しない）
    try {
      config.headers['X-Operator'] = encodeURIComponent(operator);
    } catch {
      // 失敗しても致命的にしない
    }
  }
  return config;
});

// 401 response interceptor — admin token 期限切れ時に staff token で自動リトライ
api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const original = error.config;
    if (
      error.response?.status === 401 &&
      !original._retry
    ) {
      const currentToken = localStorage.getItem('auth_token');
      const staffToken = localStorage.getItem('staff_token');
      // staff token が存在し、現在のトークンと異なる場合にフォールバック
      if (staffToken && staffToken !== currentToken) {
        original._retry = true;
        localStorage.removeItem('admin_token');
        localStorage.setItem('auth_token', staffToken);
        original.headers.Authorization = `Bearer ${staffToken}`;
        // useAuth に role 降格を通知
        window.dispatchEvent(new CustomEvent('auth:admin-expired'));
        return api(original);
      }
    }
    return Promise.reject(error);
  },
);

// ---- Practitioners ----
export const getPractitioners = () => api.get<Practitioner[]>('/practitioners/');
export const createPractitioner = (data: Partial<Practitioner>) =>
  api.post<Practitioner>('/practitioners/', data);
export const updatePractitioner = (id: number, data: Partial<Practitioner>) =>
  api.put<Practitioner>(`/practitioners/${id}`, data);
export const deletePractitioner = (id: number) =>
  api.delete<Practitioner>(`/practitioners/${id}`);
export const purgePractitioner = (id: number) =>
  api.post(`/practitioners/${id}/purge`);

// ---- Patients ----
export const getPatients = (params?: {
  page?: number;
  per_page?: number;
  sort_by?: string;
  sort_order?: string;
  include_inactive?: boolean;
}) => api.get<PatientPageResponse>('/patients/', { params });
export const getPatient = (id: number) => api.get<Patient>(`/patients/${id}`);
export const searchPatients = (q: string) =>
  api.get<Patient[]>('/patients/search', { params: { q } });
export const searchPatientsWithInactive = (q: string, includeInactive: boolean) =>
  api.get<Patient[]>('/patients/search', { params: { q, include_inactive: includeInactive } });
export const createPatient = (data: Partial<Patient>) =>
  api.post<Patient>('/patients/', data);
export const updatePatient = (id: number, data: Partial<Patient>) =>
  api.put<Patient>(`/patients/${id}`, data);
export const findCandidates = (data: {
  registration_mode?: string;
  last_name?: string;
  first_name?: string;
  full_name?: string;
  reading?: string;
  phone?: string;
  birth_date?: string;
}) => api.post<CandidateResponse[]>('/patients/candidates', data);
export const deactivatePatient = (id: number) =>
  api.post<Patient>(`/patients/${id}/deactivate`);
export const reactivatePatient = (id: number) =>
  api.post<Patient>(`/patients/${id}/reactivate`);
export const purgePatient = (id: number, reason: string) =>
  api.post(`/patients/${id}/purge`, { reason });
export const importPreview = (file: File) => {
  const fd = new FormData();
  fd.append('file', file);
  return api.post<ImportPreviewResponse>('/patients/import/preview', fd, {
    headers: { 'Content-Type': 'multipart/form-data' },
  });
};
export const importExecute = (file: File, mode: string, mapping: Record<string, number>, rowActions?: RowAction[]) => {
  const fd = new FormData();
  fd.append('file', file);
  fd.append('mode', mode);
  fd.append('mapping_json', JSON.stringify(mapping));
  if (rowActions && rowActions.length > 0) {
    fd.append('row_actions_json', JSON.stringify(rowActions));
  }
  return api.post<ImportExecuteResponse>('/patients/import/execute', fd, {
    headers: { 'Content-Type': 'multipart/form-data' },
  });
};

// ---- Menus ----
export const getMenus = () => api.get<Menu[]>('/menus/');
export const createMenu = (data: Partial<Menu>) => api.post<Menu>('/menus/', data);
export const updateMenu = (id: number, data: Partial<Menu>) =>
  api.put<Menu>(`/menus/${id}`, data);
export const deleteMenu = (id: number) => api.delete<Menu>(`/menus/${id}`);
export const purgeMenu = (id: number) => api.post(`/menus/${id}/purge`);
export const reorderMenus = (items: { id: number; display_order: number }[]) =>
  api.put<Menu[]>('/menus/reorder', items);

// ---- Reservations ----
export const getReservations = (params: {
  start_date?: string;
  end_date?: string;
  practitioner_id?: number;
}) => api.get<Reservation[]>('/reservations/', { params });

export const getReservation = (id: number) =>
  api.get<Reservation>(`/reservations/${id}`);

export const createReservation = (data: ReservationCreate) =>
  api.post<Reservation>('/reservations/', data);

export const bulkCreateReservations = (data: BulkReservationCreate) =>
  api.post<BulkReservationResult>('/reservations/bulk', data);

export const updateReservation = (id: number, data: Partial<Reservation>) =>
  api.put<Reservation>(`/reservations/${id}`, data);

export const confirmReservation = (id: number) =>
  api.post<Reservation>(`/reservations/${id}/confirm`);

export const rejectReservation = (id: number) =>
  api.post<Reservation>(`/reservations/${id}/reject`);

export const cancelRequest = (id: number) =>
  api.post<Reservation>(`/reservations/${id}/cancel-request`);

export const cancelApprove = (id: number) =>
  api.post<Reservation>(`/reservations/${id}/cancel-approve`);

export const changeRequest = (
  id: number,
  data: { new_start_time: string; new_end_time: string; new_practitioner_id?: number }
) => api.post(`/reservations/${id}/change-request`, data);

export const changeApprove = (id: number) =>
  api.post(`/reservations/${id}/change-approve`);

export const rescheduleReservation = (
  id: number,
  data: { new_start_time: string; new_end_time: string; new_practitioner_id?: number }
) => api.post(`/reservations/${id}/reschedule`, data);

export const getConflicts = () => api.get<Reservation[]>('/reservations/conflicts/');

// ---- Reservation Series ----
export const getActiveSeries = () =>
  api.get<SeriesResponse[]>('/reservations/series');

export const getSeries = (seriesId: number) =>
  api.get<SeriesResponse>(`/reservations/series/${seriesId}`);

export const getPendingSeriesAlerts = () =>
  api.get<SeriesResponse[]>('/reservations/series/pending-alerts');

export const extendSeries = (seriesId: number, data: SeriesExtendRequest) =>
  api.post<BulkReservationResult>(`/reservations/series/${seriesId}/extend`, data);

export const modifySeries = (seriesId: number, data: SeriesModifyRequest) =>
  api.post(`/reservations/series/${seriesId}/modify`, data);

export const cancelRemainingSeries = (seriesId: number) =>
  api.post(`/reservations/series/${seriesId}/cancel-remaining`);

export const declineSeriesExtension = (seriesId: number) =>
  api.post(`/reservations/series/${seriesId}/decline-extension`);

export const dismissSeriesAlert = (seriesId: number) =>
  api.post(`/reservations/series/${seriesId}/dismiss-alert`);

export const cancelSeriesFrom = (seriesId: number, reservationId: number) =>
  api.post(`/reservations/series/${seriesId}/cancel-from/${reservationId}`);

export const editSeriesFrom = (seriesId: number, reservationId: number, data: SeriesBulkEditRequest) =>
  api.post(`/reservations/series/${seriesId}/edit-from/${reservationId}`, data);

export const getSeriesReservations = (seriesId: number) =>
  api.get<Reservation[]>(`/reservations/series/${seriesId}/reservations`);

// ---- Settings ----
export const getSettings = () => api.get<Setting[]>('/settings/');
export const updateSetting = (key: string, value: string) =>
  api.put<Setting>(`/settings/${key}`, { value });

// ---- Weekly Schedules ----
export const getWeeklySchedules = () =>
  api.get<WeeklySchedule[]>('/weekly-schedules/');
export const updateWeeklySchedule = (
  dayOfWeek: number,
  data: { is_open: boolean; open_time: string; close_time: string }
) => api.put<WeeklySchedule>(`/weekly-schedules/${dayOfWeek}`, data);

// ---- Date Overrides (特別休診日・特別営業日) ----
export const getDateOverrides = () =>
  api.get<DateOverride[]>('/date-overrides/');
export const createDateOverride = (data: {
  date: string;
  is_open: boolean;
  open_time?: string;
  close_time?: string;
  label?: string;
}) => api.post<DateOverride>('/date-overrides/', data);
export const updateDateOverride = (id: number, data: {
  is_open: boolean;
  open_time?: string;
  close_time?: string;
  label?: string;
}) => api.put<DateOverride>(`/date-overrides/${id}`, data);
export const deleteDateOverride = (id: number) =>
  api.delete(`/date-overrides/${id}`);

// ---- Business Hours (解決済み営業時間) ----
export const getBusinessHoursRange = (params: {
  start_date: string;
  end_date: string;
}) => api.get<BusinessHoursDay[]>('/business-hours/range', { params });

// ---- Practitioner Schedules ----
export const getPractitionerDefaults = (practitionerId: number) =>
  api.get<PractitionerSchedule[]>(`/practitioner-schedules/${practitionerId}/defaults`);
export const updatePractitionerDefaults = (
  practitionerId: number,
  data: { schedules: Array<{ day_of_week: number; is_working: boolean; start_time: string; end_time: string }> }
) => api.put<PractitionerSchedule[]>(`/practitioner-schedules/${practitionerId}/defaults`, data);
export const getScheduleOverrides = (params?: {
  practitioner_id?: number;
  start_date?: string;
  end_date?: string;
}) => api.get<ScheduleOverride[]>('/practitioner-schedules/overrides', { params });
export const createScheduleOverride = (data: {
  practitioner_id: number;
  date: string;
  is_working: boolean;
  reason?: string;
}) => api.post<ScheduleOverride>('/practitioner-schedules/overrides', data);
export const deleteScheduleOverride = (id: number) =>
  api.delete(`/practitioner-schedules/overrides/${id}`);
export const getScheduleStatus = (params: {
  practitioner_ids: string;
  start_date: string;
  end_date: string;
}) => api.get<PractitionerDayStatus[]>('/practitioner-schedules/status', { params });
export const getAffectedReservations = (practitionerId: number, targetDate: string) =>
  api.get<AffectedReservation[]>('/practitioner-schedules/overrides/affected-reservations', {
    params: { practitioner_id: practitionerId, target_date: targetDate },
  });
export const transferReservation = (reservationId: number, newPractitionerId: number) =>
  api.post<Reservation>(`/practitioner-schedules/reservations/${reservationId}/transfer`, {
    new_practitioner_id: newPractitionerId,
  });

// ---- Practitioner Unavailable Times (時間帯休み) ----
export const getUnavailableTimes = (params?: {
  practitioner_id?: number;
  start_date?: string;
  end_date?: string;
}) => api.get<UnavailableTime[]>('/practitioner-schedules/unavailable-times', { params });
export const createUnavailableTime = (data: {
  practitioner_id: number;
  date: string;
  start_time: string;
  end_time: string;
  reason?: string;
}) => api.post<UnavailableTime>('/practitioner-schedules/unavailable-times', data);
export const deleteUnavailableTime = (id: number) =>
  api.delete(`/practitioner-schedules/unavailable-times/${id}`);

// ---- 休暇かぶり予約アラート ----
export type ScheduleConflictAlert = {
  kind: 'override' | 'unavailable_time';
  source_id: number;
  practitioner_id: number;
  practitioner_name: string;
  date: string;
  reservation_id: number;
  patient_name: string;
  start_time: string;
  end_time: string;
  reason: string | null;
  message: string;
};
export const getScheduleConflictAlerts = () =>
  api.get<ScheduleConflictAlert[]>('/practitioner-schedules/conflict-alerts');

// ---- Notifications ----
export const getNotifications = () => api.get<Notification[]>('/notifications/');
export const markNotificationRead = (id: number) =>
  api.put<Notification>(`/notifications/${id}/read`);
export const deleteNotification = (id: number) =>
  api.delete(`/notifications/${id}`);
export const deleteAllNotifications = () =>
  api.delete('/notifications/');
export const getAuditLogs = (limit = 200) =>
  api.get<AuditLog[]>('/audit-logs/', { params: { limit } });

// ---- Auth ----
export const staffLogin = (pin: string) =>
  api.post<{ token: string; role: string }>('/auth/staff-login', { pin });
export const adminLogin = (username: string, password: string) =>
  api.post<{ token: string; role: string }>('/auth/admin-login', { username, password });
export const authMe = () =>
  api.get<{ authenticated: boolean; role: string | null }>('/auth/me');
export const changePassword = (new_password: string) =>
  api.put('/auth/change-password', { new_password });
// legacy compat
export const login = (password: string) =>
  api.post<{ token: string }>('/auth/login', { password });

// ---- Reservation Colors ----
export const getReservationColors = () =>
  api.get<ReservationColor[]>('/reservation-colors/');
export const createReservationColor = (data: Partial<ReservationColor>) =>
  api.post<ReservationColor>('/reservation-colors/', data);
export const updateReservationColor = (id: number, data: Partial<ReservationColor>) =>
  api.put<ReservationColor>(`/reservation-colors/${id}`, data);
export const deleteReservationColor = (id: number) =>
  api.delete(`/reservation-colors/${id}`);

// ---- HotPepper Sync ----
export const getPendingSync = () => api.get<Reservation[]>('/hotpepper/pending-sync/');
export const markSynced = (id: number) =>
  api.post(`/hotpepper/${id}/mark-synced`);
export const parseHotpepperEmail = (emailBody: string) =>
  api.post('/hotpepper/parse-email', { email_body: emailBody });

// ---- Chatbot ----
export const createChatSession = () =>
  api.post<{ session_id: string; messages: Array<{ role: string; content: string }>; status: string }>('/chatbot/session/');
export const sendChatMessage = (sessionId: string, message: string) =>
  api.post<ChatResponse>('/chatbot/message', { session_id: sessionId, message });
export const getChatSession = (sessionId: string) =>
  api.get(`/chatbot/session/${sessionId}`);

// ---- Public Web Reserve ----
export const submitWebReserve = (data: WebReserveRequest) =>
  api.post<WebReserveSuccess | WebReserveConflict>('/web_reserve', data);

// ---- Data Reset ----
export const resetOperationalData = () =>
  api.post<{ status: string; deleted_reservations: number; deleted_patients: number }>(
    '/settings/reset-operational-data'
  );

export default api;

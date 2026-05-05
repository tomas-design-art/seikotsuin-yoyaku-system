// Types matching the backend API responses

export interface Practitioner {
  id: number;
  name: string;
  role: string;
  daily_report_code: string | null;
  is_active: boolean;
  is_visible: boolean;
  display_order: number;
  created_at: string;
  updated_at: string;
}

export interface Patient {
  id: number;
  name: string;
  registration_mode: string;
  last_name: string | null;
  middle_name: string | null;
  first_name: string | null;
  last_name_kana: string | null;
  first_name_kana: string | null;
  reading: string | null;
  birth_date: string | null;
  patient_number: string | null;
  phone: string | null;
  email: string | null;
  line_id: string | null;
  notes: string | null;
  is_active: boolean;
  default_menu_id: number | null;
  default_duration: number | null;
  preferred_practitioner_id: number | null;
  created_at: string;
  updated_at: string;
}

export interface CandidateResponse {
  patient: Patient;
  match_reasons: string[];
}

export interface PatientPageResponse {
  items: Patient[];
  total: number;
  page: number;
  per_page: number;
}

// ── 一括取り込み ──
export interface ImportPreviewResponse {
  columns: string[];
  suggested_mapping: Record<string, number>;
  suggested_mode: 'split' | 'full_name';
  preview_rows: Record<string, string>[];
  total_data_rows: number;
  splittable_hint: boolean;
}

export interface ImportDuplicate {
  row: number;
  data: Record<string, string>;
  candidates: { id: number; name: string; patient_number: string | null; phone: string | null; reading: string | null; birth_date: string | null; reasons: string[] }[];
}

export interface RowAction {
  row: number;
  action: 'use_existing' | 'update_existing' | 'skip';
  patient_id?: number;
}

export interface ImportExecuteResponse {
  total_rows: number;
  created_count: number;
  skipped_count: number;
  duplicate_count: number;
  adopted_count: number;
  updated_count: number;
  error_count: number;
  duplicates: ImportDuplicate[];
  errors: { row: number; reason: string }[];
}

export interface PatientBrief {
  id: number;
  name: string;
  patient_number: string | null;
}

export interface MenuPriceTier {
  id?: number;
  duration_minutes: number;
  price: number | null;
  display_order: number;
}

export interface Menu {
  id: number;
  name: string;
  duration_minutes: number;
  is_duration_variable: boolean;
  max_duration_minutes: number | null;
  price: number | null;
  color_id: number | null;
  color: ColorBrief | null;
  is_active: boolean;
  display_order: number;
  price_tiers: MenuPriceTier[];
  created_at: string;
  updated_at: string;
}

export interface MenuBrief {
  id: number;
  name: string;
  duration_minutes: number;
}

export type ReservationStatus =
  | 'PENDING'
  | 'HOLD'
  | 'CONFIRMED'
  | 'CHANGE_REQUESTED'
  | 'CANCEL_REQUESTED'
  | 'CANCELLED'
  | 'REJECTED'
  | 'EXPIRED';

export type Channel = 'PHONE' | 'WALK_IN' | 'LINE' | 'HOTPEPPER' | 'CHATBOT';

export interface ReservationColor {
  id: number;
  name: string;
  color_code: string;
  display_order: number;
  is_default: boolean;
  created_at: string;
  updated_at: string;
}

export interface ColorBrief {
  id: number;
  name: string;
  color_code: string;
}

export interface SeriesInfoBrief {
  id: number;
  frequency: string;
  total_created: number;
  remaining_count: number;
  is_active: boolean;
}

export interface Reservation {
  id: number;
  patient: PatientBrief | null;
  practitioner_id: number;
  practitioner_name: string | null;
  menu: MenuBrief | null;
  color: ColorBrief | null;
  color_id: number | null;
  start_time: string;
  end_time: string;
  status: ReservationStatus;
  channel: Channel;
  source_ref: string | null;
  notes: string | null;
  conflict_note: string | null;
  hotpepper_synced: boolean;
  hold_expires_at: string | null;
  series_id: number | null;
  series_info: SeriesInfoBrief | null;
  created_at: string;
  updated_at: string;
}

export interface ReservationCreate {
  patient_id?: number | null;
  practitioner_id: number;
  menu_id?: number | null;
  color_id?: number | null;
  start_time: string;
  end_time: string;
  channel: Channel;
  notes?: string;
  source_ref?: string;
}

export interface Setting {
  id: number;
  key: string;
  value: string;
  updated_at: string;
}

export interface Notification {
  id: number;
  reservation_id: number | null;
  event_type: string;
  message: string;
  is_read: boolean;
  created_at: string;
}

export interface AuditLog {
  id: number;
  timestamp: string;
  operator: string;
  action: string;
  target_id: number | null;
  detail: Record<string, unknown> | null;
}

// --- Bulk Reservation ---

export interface BulkReservationCreate {
  patient_id?: number | null;
  practitioner_id: number;
  menu_id?: number | null;
  color_id?: number | null;
  start_time: string; // "HH:MM"
  duration_minutes: number;
  channel: Channel;
  notes?: string;
  frequency: 'weekly' | 'biweekly' | 'monthly';
  start_date: string; // "YYYY-MM-DD"
  end_date?: string | null;
  count?: number | null;
}

export interface BulkReservationResult {
  total_requested: number;
  created_count: number;
  skipped: { date: string; reason: string }[];
  series_id?: number;
}

// --- シリーズ管理 ---

export interface SeriesResponse {
  id: number;
  patient_id: number | null;
  patient_name: string | null;
  practitioner_id: number;
  practitioner_name: string | null;
  menu_id: number | null;
  menu_name: string | null;
  start_time: string;
  duration_minutes: number;
  frequency: string;
  channel: string;
  remaining_count: number;
  total_created: number;
  is_active: boolean;
  created_at: string;
}

export interface SeriesExtendRequest {
  count: number;
}

export interface SeriesModifyRequest {
  practitioner_id?: number;
  menu_id?: number;
  color_id?: number;
  start_time?: string;
  duration_minutes?: number;
  frequency?: string;
  count?: number;
  cancel_remaining?: boolean;
}

export interface SeriesBulkEditRequest {
  practitioner_id?: number;
  menu_id?: number;
  color_id?: number;
  start_time?: string;
  duration_minutes?: number;
  notes?: string;
}

export interface ConflictingReservation {
  id: number;
  patient_name: string | null;
  start_time: string;
  end_time: string;
  status: string;
}

export interface ConflictResponse {
  detail: string;
  conflicting_reservations: ConflictingReservation[];
}

// Warning colors (fixed, cannot be overridden)
export const WARNING_COLORS: Record<string, string> = {
  conflict: '#EF4444',
  CANCEL_REQUESTED: '#FCA5A5',
  CHANGE_REQUESTED: '#FB923C',
  HOLD: '#8B5CF6',
  PENDING: '#F59E0B',
};

// Status color mapping (fallback when no user color is set)
export const STATUS_COLORS: Record<ReservationStatus, string> = {
  CONFIRMED: '#3B82F6',
  PENDING: '#F59E0B',
  HOLD: '#8B5CF6',
  CANCEL_REQUESTED: '#FCA5A5',
  CHANGE_REQUESTED: '#FB923C',
  CANCELLED: '#9CA3AF',
  REJECTED: '#EF4444',
  EXPIRED: '#6B7280',
};

// Channel icons
export const CHANNEL_ICONS: Record<Channel, string> = {
  PHONE: '📞',
  WALK_IN: '🏥',
  LINE: '💬',
  HOTPEPPER: '🔥',
  CHATBOT: '🤖',
};

// Channel labels (Japanese)
export const CHANNEL_LABELS: Record<Channel, string> = {
  PHONE: '電話',
  WALK_IN: '直接来院',
  LINE: 'LINE',
  HOTPEPPER: 'ホットペッパー',
  CHATBOT: 'チャットボット',
};

// Chat types
export interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
}

export interface ChatSession {
  id: string;
  messages: ChatMessage[];
  reservation_id: number | null;
  status: 'active' | 'completed' | 'expired';
  created_at: string;
  updated_at: string;
}

export interface ChatResponse {
  session_id: string;
  response: string;
  actions: Array<{
    type: string;
    options?: string[];
  }>;
  reservation_created: {
    id: number;
    date: string;
    start_time: string;
    end_time: string;
    menu: string;
    patient_name: string;
  } | null;
}

export interface WebReserveRequest {
  name: string;
  phone: string;
  menu_id: number;
  desired_datetime: string;
  duration?: number;
}

export interface WebReserveSuccess {
  status: 'success';
  reservation_id: number;
}

export interface WebReserveConflict {
  status: 'conflict';
  alternatives: string[];
}

export interface WeeklySchedule {
  id: number;
  day_of_week: number;
  is_open: boolean;
  open_time: string;
  close_time: string;
  updated_at: string;
}

// --- Practitioner Schedule ---

export interface PractitionerSchedule {
  id: number;
  practitioner_id: number;
  day_of_week: number;
  is_working: boolean;
  start_time: string;
  end_time: string;
}

export interface ScheduleOverride {
  id: number;
  practitioner_id: number;
  date: string;
  is_working: boolean;
  reason: string | null;
  created_at: string;
}

export interface PractitionerDayStatus {
  practitioner_id: number;
  date: string;
  is_working: boolean;
  start_time: string | null;
  end_time: string | null;
  reason: string | null;
  source: 'override' | 'default' | 'fallback' | 'clinic' | 'weekly' | 'holiday' | 'holiday_schedule' | 'holiday_default';
  unavailable_times?: { id: number; start_time: string; end_time: string; reason: string | null }[];
}

export interface TransferCandidate {
  practitioner_id: number;
  practitioner_name: string;
  is_available: boolean;
}

export interface AffectedReservation {
  reservation_id: number;
  patient_name: string | null;
  start_time: string;
  end_time: string;
  menu_name: string | null;
  transfer_candidates: TransferCandidate[];
}

// --- Date Override (特別休診日・特別営業日) ---

export interface DateOverride {
  id: number;
  date: string;
  is_open: boolean;
  open_time: string | null;
  close_time: string | null;
  label: string | null;
  updated_at: string;
}

// --- Business Hours (解決済み日別営業時間) ---

export interface BusinessHoursDay {
  date: string;
  is_open: boolean;
  open_time: string | null;
  close_time: string | null;
  source: 'override' | 'holiday' | 'weekly' | 'fallback';
  label: string | null;
}

// --- Practitioner Unavailable Time (時間帯休み) ---

export interface UnavailableTime {
  id: number;
  practitioner_id: number;
  date: string;
  start_time: string;
  end_time: string;
  reason: string | null;
  created_at: string;
}

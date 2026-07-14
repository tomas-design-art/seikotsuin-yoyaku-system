/**
 * 通知音再生ユーティリティ
 */

let audioContext: AudioContext | null = null;

export async function initAudio(): Promise<void> {
  if (!audioContext) {
    audioContext = new AudioContext();
  }
  if (audioContext.state === 'suspended') {
    await audioContext.resume();
  }
}

async function ensureRunning(): Promise<boolean> {
  if (!audioContext) {
    await initAudio();
  }
  // suspended のまま resume が間に合っていないケースを再度チェック
  if (audioContext && audioContext.state === 'suspended') {
    await audioContext.resume();
  }
  return audioContext?.state === 'running';
}

async function playTone(frequency: number, duration: number, volume = 0.3, type: OscillatorType = 'sine'): Promise<void> {
  const ready = await ensureRunning();
  if (!ready || !audioContext) return;

  const oscillator = audioContext.createOscillator();
  const gainNode = audioContext.createGain();

  oscillator.connect(gainNode);
  gainNode.connect(audioContext.destination);

  oscillator.frequency.value = frequency;
  oscillator.type = type;
  gainNode.gain.value = volume;

  // Fade out
  gainNode.gain.exponentialRampToValueAtTime(0.001, audioContext.currentTime + duration);

  oscillator.start(audioContext.currentTime);
  oscillator.stop(audioContext.currentTime + duration);
}

export async function playNotificationSound(): Promise<void> {
  await playTone(880, 0.3);
  setTimeout(() => playTone(1100, 0.2), 200);
}

export async function playAlertSound(): Promise<void> {
  for (let i = 0; i < 3; i++) {
    setTimeout(() => playTone(1200, 0.15, 0.5), i * 250);
    setTimeout(() => playTone(800, 0.15, 0.5), i * 250 + 125);
  }
}

export async function playWarningSound(): Promise<void> {
  await playTone(600, 0.5, 0.4);
}

/** 自動予約着信音（旧・汎用フォールバック）: メール受信音風の3音チャイム */
export async function playIncomingReservationSound(): Promise<void> {
  await playSoundPattern('bright_ascend');
}

// ────────────────────────────────────────────────────────────
// 通知音パターン registry
// スタッフが「設定」画面からチャネルごとに選んでテスト再生できるようにする。
// いずれも「聞き逃さない」ことを優先し、1.3〜2.5秒程度の長めの複数音パターンにしている。
// ────────────────────────────────────────────────────────────

export type SoundPatternId =
  | 'school_chime'
  | 'interphone_double'
  | 'bright_ascend'
  | 'triple_bell'
  | 'soft_notify';

export const SOUND_PATTERNS: { id: SoundPatternId; label: string; description: string }[] = [
  {
    id: 'school_chime',
    label: '学校チャイム風（キーンコーンカーンコーン）',
    description: '4音のゆったりした下降チャイム。約2.2秒、離れた席でも聞き取りやすい。',
  },
  {
    id: 'interphone_double',
    label: 'インターホン風2連打（タラン！タラン！）',
    description: '高音の2連打を2セット。約1.4秒、はっきりした輪郭の音。',
  },
  {
    id: 'bright_ascend',
    label: '明るい上昇音（テ・レ・レ・レン♪）',
    description: '4音の上昇＋伸ばし。約1.4秒、爽やかで柔らかい印象。',
  },
  {
    id: 'triple_bell',
    label: 'トリプルベル（コーン・コーン・コーーン）',
    description: 'ベルを3回、最後は長め。約1.7秒。',
  },
  {
    id: 'soft_notify',
    label: '控えめ通知音（短め）',
    description: '短い2音のみ。約0.5秒、目立たせすぎたくない場合向け。',
  },
];

/** チャネル別デフォルトパターン（初期設定値・バックエンド設定が未取得の場合のフォールバック） */
export const DEFAULT_CHANNEL_SOUND_PATTERNS: Record<'hotpepper' | 'line' | 'web', SoundPatternId> = {
  hotpepper: 'school_chime',
  line: 'triple_bell',
  web: 'bright_ascend',
};

interface ToneStep {
  freq: number;
  duration: number;
  delay: number; // ms from sequence start
  volume?: number;
  type?: OscillatorType;
}

function scheduleSequence(steps: ToneStep[]): void {
  steps.forEach((step) => {
    setTimeout(() => {
      void playTone(step.freq, step.duration, step.volume ?? 0.45, step.type ?? 'sine');
    }, step.delay);
  });
}

/** パターンIDを指定して通知音を再生する（設定画面のテスト再生・実際の通知の両方から利用） */
export async function playSoundPattern(id: SoundPatternId): Promise<void> {
  const ready = await ensureRunning();
  if (!ready) return;

  switch (id) {
    case 'school_chime':
      // キンコンカンコン（Westminster風）。ゆったり長めで聞き逃し防止。
      scheduleSequence([
        { freq: 784, duration: 0.5, delay: 0 },
        { freq: 659, duration: 0.5, delay: 450 },
        { freq: 523, duration: 0.5, delay: 900 },
        { freq: 392, duration: 0.9, delay: 1350 },
      ]);
      break;
    case 'interphone_double':
      scheduleSequence([
        { freq: 1100, duration: 0.18, delay: 0, type: 'triangle' },
        { freq: 1375, duration: 0.3, delay: 120, type: 'triangle' },
        { freq: 1100, duration: 0.18, delay: 700, type: 'triangle' },
        { freq: 1375, duration: 0.35, delay: 820, type: 'triangle' },
      ]);
      break;
    case 'bright_ascend':
      scheduleSequence([
        { freq: 660, duration: 0.2, delay: 0 },
        { freq: 880, duration: 0.2, delay: 160 },
        { freq: 1100, duration: 0.2, delay: 320 },
        { freq: 1320, duration: 0.2, delay: 480 },
        { freq: 1650, duration: 0.5, delay: 640 },
      ]);
      break;
    case 'triple_bell':
      scheduleSequence([
        { freq: 1046, duration: 0.35, delay: 0 },
        { freq: 1046, duration: 0.35, delay: 500 },
        { freq: 1318, duration: 0.7, delay: 1000 },
      ]);
      break;
    case 'soft_notify':
      scheduleSequence([
        { freq: 880, duration: 0.3, delay: 0 },
        { freq: 1100, duration: 0.3, delay: 220 },
      ]);
      break;
  }
}


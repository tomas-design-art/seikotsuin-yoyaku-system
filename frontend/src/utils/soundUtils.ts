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

/** 自動予約着信音（ホットペッパー・ホームページ予約）: メール受信音風の3音チャイム */
export async function playIncomingReservationSound(): Promise<void> {
  await playTone(660, 0.3, 0.45);
  setTimeout(() => playTone(880, 0.3, 0.45), 180);
  setTimeout(() => playTone(1100, 0.55, 0.45), 360);
}

/** ホットペッパー予約着信音: 三角波を使って輪郭をはっきりさせた、高音のインターホン風2連打（タラン！ タラン！） */
export async function playHotpepperReservationSound(): Promise<void> {
  // 1回目：タラン！
  await playTone(1100, 0.15, 0.45, 'triangle');
  setTimeout(() => playTone(1375, 0.25, 0.45, 'triangle'), 100);

  // 2回目：タラン！（400ms 後）
  setTimeout(() => {
    playTone(1100, 0.15, 0.45, 'triangle');
    setTimeout(() => playTone(1375, 0.25, 0.45, 'triangle'), 100);
  }, 400);
}

/** ホームページ/チャットボット予約着信音: 明るい和音風の上昇4連音（テ・レ・レ・レン♪） */
export async function playWebReservationSound(): Promise<void> {
  await playTone(880, 0.15, 0.4, 'sine');
  setTimeout(() => playTone(1100, 0.15, 0.4, 'sine'), 120);
  setTimeout(() => playTone(1320, 0.15, 0.4, 'sine'), 240);
  setTimeout(() => playTone(1650, 0.35, 0.4, 'sine'), 360);
}

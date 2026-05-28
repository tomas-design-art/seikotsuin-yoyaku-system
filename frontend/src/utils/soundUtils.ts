/**
 * 通知音再生ユーティリティ
 */

let audioContext: AudioContext | null = null;

export function initAudio(): void {
  if (!audioContext) {
    audioContext = new AudioContext();
  }
  if (audioContext.state === 'suspended') {
    audioContext.resume();
  }
}

async function playTone(frequency: number, duration: number, volume = 0.3): Promise<void> {
  if (!audioContext) {
    initAudio();
  }
  if (!audioContext) return;

  const oscillator = audioContext.createOscillator();
  const gainNode = audioContext.createGain();

  oscillator.connect(gainNode);
  gainNode.connect(audioContext.destination);

  oscillator.frequency.value = frequency;
  oscillator.type = 'sine';
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
export function playIncomingReservationSound(): void {
  playTone(660, 0.3, 0.45);
  setTimeout(() => playTone(880, 0.3, 0.45), 180);
  setTimeout(() => playTone(1100, 0.55, 0.45), 360);
}

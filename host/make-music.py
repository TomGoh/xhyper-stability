#!/usr/bin/env python3
"""make-music.py OUT.wav [seconds=180]: a generated test track for the soak phase's music option.

A plain melody so the audio path runs like real playback (not a constant tone): a C - Am - F - G progression as
eighth-note arpeggios at 100 bpm over held bass notes, soft attack and release on every note, 44.1 kHz stereo 16-bit
PCM (WAV, since no MP3 encoder is needed on the host). Python 3 stdlib only.
"""
import array, math, sys, wave

RATE = 44100
CHORDS = [(48, (60, 64, 67, 72)), (45, (57, 60, 64, 69)), (41, (53, 57, 60, 65)), (43, (55, 59, 62, 67))]


def hz(midi):
    return 440.0 * 2 ** ((midi - 69) / 12)


def note(buf, start, length, freq, amp):
    a, r = int(0.01 * RATE), int(0.08 * RATE)
    for i in range(length):
        env = min(1.0, i / a) * min(1.0, (length - i) / r)
        t = i / RATE
        v = math.sin(2 * math.pi * freq * t) + 0.3 * math.sin(4 * math.pi * freq * t)
        buf[start + i] += amp * env * v


def main():
    out = sys.argv[1]
    seconds = int(sys.argv[2]) if len(sys.argv) > 2 else 180
    beat = 60 / 100
    eighth = int(beat / 2 * RATE)
    bar = 8 * eighth
    n = seconds * RATE
    buf = [0.0] * (n + bar)
    pos, k = 0, 0
    while pos < n:
        bass, arp = CHORDS[k % len(CHORDS)]
        note(buf, pos, bar, hz(bass), 0.18)
        for j in range(8):
            note(buf, pos + j * eighth, eighth, hz(arp[(j if j < 4 else 7 - j) % 4] + (12 if j == 4 else 0)), 0.12)
        pos += bar
        k += 1
    pcm = array.array('h')
    for i in range(n):
        v = int(max(-1.0, min(1.0, buf[i])) * 32767 * 0.6)
        pcm.append(v); pcm.append(v)
    with wave.open(out, 'wb') as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
    print(f'{out}: {seconds} s, {RATE} Hz stereo 16-bit')


if __name__ == '__main__':
    main()

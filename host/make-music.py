#!/usr/bin/env python3
"""make-music.py OUT.wav [seconds=180]: a generated test track for the soak phase's music option.

A plain melody so the audio path runs like real playback (not a constant tone): a C - Am - F - G progression as
eighth-note arpeggios at 100 bpm over held bass notes, soft attack and release on every note, 44.1 kHz stereo 16-bit
PCM (WAV, since no MP3 encoder is needed on the host). Python 3 stdlib only.
"""
# How it is run: python3 host/make-music.py OUT.wav [seconds]; it writes OUT.wav and prints
# `OUT.wav: <seconds> s, 44100 Hz stereo 16-bit`. The same arguments always give the same file.
#
# Structure:
#   Constants             RATE, CHORDS
#   Synthesis             hz, note: a MIDI note's frequency; one enveloped note added into the sample buffer
#   Track                 main: the chord progression, mixed down to 16-bit stereo and written as WAV
import array, math, sys, wave

# ==== Constants =======================================================================================================

# samples per second
RATE = 44100
# the progression, one chord per bar: (bass MIDI note, the four arpeggio MIDI notes), i.e. C, Am, F, G
CHORDS = [(48, (60, 64, 67, 72)), (45, (57, 60, 64, 69)), (41, (53, 57, 60, 65)), (43, (55, 59, 62, 67))]


# ==== Synthesis =======================================================================================================

def hz(midi):
    """Frequency in Hz of a MIDI note number (69 = A4 = 440 Hz, equal temperament)."""
    return 440.0 * 2 ** ((midi - 69) / 12)


def note(buf, start, length, freq, amp):
    """Adds one note to buf (float samples) at sample `start` for `length` samples: the fundamental plus 0.3 of the
    second harmonic, scaled by amp, with a 10 ms linear attack and an 80 ms linear release."""
    # a, r: attack and release lengths in samples
    a, r = int(0.01 * RATE), int(0.08 * RATE)
    for i in range(length):
        env = min(1.0, i / a) * min(1.0, (length - i) / r)
        t = i / RATE
        v = math.sin(2 * math.pi * freq * t) + 0.3 * math.sin(4 * math.pi * freq * t)
        buf[start + i] += amp * env * v


# ==== Track ===========================================================================================================

def main():
    """Writes the track to sys.argv[1], sys.argv[2] seconds long (default 180)."""
    out = sys.argv[1]
    seconds = int(sys.argv[2]) if len(sys.argv) > 2 else 180
    beat = 60 / 100  # seconds per beat at 100 bpm
    eighth = int(beat / 2 * RATE)
    bar = 8 * eighth
    n = seconds * RATE
    # one bar of headroom: the last bar's notes may run past the end of the track
    buf = [0.0] * (n + bar)
    # pos: the sample where the current bar starts; k: the bar number (which chord)
    pos, k = 0, 0
    while pos < n:
        bass, arp = CHORDS[k % len(CHORDS)]
        # the bass note held for the whole bar
        note(buf, pos, bar, hz(bass), 0.18)
        # eight eighth notes up and down the chord: notes 0 1 2 3 3 2 1 0, the fifth one an octave higher
        for j in range(8):
            note(buf, pos + j * eighth, eighth, hz(arp[(j if j < 4 else 7 - j) % 4] + (12 if j == 4 else 0)), 0.12)
        pos += bar
        k += 1
    # mix-down: clipped to [-1, 1], scaled to 0.6 of the 16-bit range, the same sample on both channels
    pcm = array.array('h')
    for i in range(n):
        v = int(max(-1.0, min(1.0, buf[i])) * 32767 * 0.6)
        pcm.append(v)
        pcm.append(v)
    with wave.open(out, 'wb') as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
    print(f'{out}: {seconds} s, {RATE} Hz stereo 16-bit')


if __name__ == '__main__':
    main()

#!/system/bin/sh
# klogwatch.sh <file> <regex>: follows the kernel log (`dmesg -w`, which prints the buffer first) into <file>, keeping
# only the records whose first line matches the extended regex <regex>, with their continuation lines (a record of
# several lines, such as an IRQ-off report's stack, prints its later lines without a time stamp). The kernel ring buffer
# can wrap during a long measurement; this copy cannot. Run as root, detached
# (`setsid nohup sh klogwatch.sh <file> <regex> > /dev/null 2>&1 < /dev/null &`); <file>.pid holds this script's pid,
# then the filter's: `pkill -P <first pid>; kill <both pids>` stops it.

# ---- arguments -------------------------------------------------------------------------------------------------------
OUT=${1:?file}    # the file the matching records are appended to
REGEX=${2:?regex} # extended regex a record's first line must match

# ---- follow the kernel log -------------------------------------------------------------------------------------------
echo $$ > "$OUT.pid"
# awk: a line with a time stamp ("[") starts a record and decides whether it is kept; lines without one (continuation
# lines) follow that decision. fflush: every kept line reaches the file at once.
dmesg -w 2>/dev/null | awk -v r="$REGEX" '/^\[/ { keep = ($0 ~ r) } keep { print; fflush() }' >> "$OUT" &
echo $! >> "$OUT.pid"
wait

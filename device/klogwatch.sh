#!/system/bin/sh
# klogwatch.sh <file> <regex>: follows the kernel log (`dmesg -w`, which prints the buffer first) into <file>, keeping
# only the records whose first line matches the extended regex <regex>, with their continuation lines (a record of
# several lines, such as an IRQ-off report's stack, prints its later lines without a time stamp). The kernel ring buffer
# can wrap during a long measurement; this copy cannot. Run as root, detached
# (`setsid nohup sh klogwatch.sh <file> <regex> > /dev/null 2>&1 < /dev/null &`); <file>.pid holds this script's pid,
# then the filter's: `pkill -P <first pid>; kill <both pids>` stops it.
F=${1:?file}; R=${2:?regex}
echo $$ > "$F.pid"
dmesg -w 2>/dev/null | awk -v r="$R" '/^\[/ { keep = ($0 ~ r) } keep { print; fflush() }' >> "$F" &
echo $! >> "$F.pid"
wait

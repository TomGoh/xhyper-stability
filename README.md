# xhyper-stability：虚拟化层下 Android 设备的稳定性与压力测试

对运行在 XHyper 之下的 Android 设备（当前是 T11206 平板）做长时间的稳定性与压力测试，并且从主机一侧判断故障属于哪一层。

一般的 Android 稳定性测试假定故障会在 Android 里留下痕迹（崩溃记录、无响应、墓碑文件、内核日志）。虚拟化层出错时，最常见的结果是整机复位或冻住，Android 来不及记录。所以本仓库的检测从下往上做：设备还能不能连上，开机号有没有变，引导程序和 pstore 记下了什么，然后才看 Android 里的记录。

## 目录

| 路径 | 内容 |
|---|---|
| `host/stab.py` | 主机端调度：按计划依次执行各阶段，定期做健康快照，记录事件，写出总结。只用 Python 3 标准库 |
| `device/health.sh` | 设备端只读快照（以 root 运行）：开机号、开机原因、睡眠计数、频率、温度、pstore、内核异常行、崩溃缓冲区、无响应 |
| `plans/*.json` | 测试计划：`smoke.json`（每个阶段跑一次，约 20 分钟），`overnight.json`（约 12 小时） |
| `third_party/apks.lock` | 测试会安装的 APK 清单（文件名、包名、版本、sha256）；APK 本身不进仓库（许可、体积、内部应用），运行时从 `STAB_APK_DIR` 目录取，哈希对上才装 |
| `third_party/` | 公开压力工具的固定版本和静态 aarch64 构建脚本（stress-ng、stressapptest、rt-tests 的 cyclictest），以及 Fastbot 的拉取脚本；编出的程序不入库 |

## 用法

```bash
python3 host/stab.py run -s <序列号> --plan plans/smoke.json --out <输出目录> [--expect-boot-sha <boot_a 哈希前缀>]
python3 host/stab.py report <输出目录>      # 重新生成 summary.md
```

- `--expect-boot-sha`：开始时核对设备上 `boot_a` 的哈希。发版测试一定要带，保证测的就是要发的镜像。
- 输出目录里有 `events.jsonl`、`log.txt`、`summary.md`、`identity.txt`、每次快照（`health/`）、压力工具输出、意外重启时取回的 pstore（`pstore-NNNN/`）。工作区的记录放在 `docs/artifacts/<记录>/` 下。
- 远端 adb（例如经隧道连到别人电脑上的设备）：设置 `ADB_SERVER_SOCKET`。
- 中途按 Ctrl-C 或者收到 SIGTERM 也会写出 summary.md，并停掉 monkey。

## 阶段

| 类型 | 做什么 | 主要压到虚拟化层的哪部分 |
|---|---|---|
| `reboot_loop` | 反复冷启动，核对开机原因、开机号、pstore | 启动路径、各核上线 |
| `soak` | 限定应用的 monkey 随机操作；每隔一段时间跑一次 stress-ng 定向压力；定期灭屏亮屏；可选放音乐（`music`：用 VIEW 意图在音乐应用的曲目页打开媒体库里的指定文件，按键设音量，定时检查、停了就重放，阶段结束前暂停） | 核间中断（`--affinity --switch --futex --membarrier --tlb-shootdown`）、定时器（`--timer --hrtimers --cyclic`）、内存（`--vm`）、各类设备中断 |
| `suspend` | 用 `rtcwake -m mem` 强制系统睡眠，由闹钟唤醒，核对睡眠成功和失败计数 | 系统睡眠、各核掉电和上电、唤醒中断 |
| `memtest` | stressapptest 在高负载下检查内存内容 | 第二阶段映射、缓存维护；出错时报数据不一致 |
| `latency` | cyclictest 测空闲和有负载时的唤醒延迟 | 定时器注入、唤醒路径 |
| `standby` | 灭屏后在 `hours` 小时内反复强制睡眠（每次 `suspend_s` 秒），记下每次到 adb 重新连上的实际时长和唤醒原因；中位数不到请求时长一半时记 `STANDBY_SHORT`（警告：插着 USB 供电时，厂商充电管理每次睡眠都安排几秒后的唤醒闹钟，测不到长待机）。这时每次循环约 60 秒，`hours: 2` 约 115 次循环、230 次健康快照；睡眠被拒（adb 没断开）的那次不计入时长统计 | 长时间深度睡眠后的恢复 |
| `el2_stress` | `device/el2stress.sh` 按退出类型逐项施压，每项后记 EL2 占比（平行日志）和串口上的热点路径计数：`ipi`（membarrier、TLB 击落、hackbench）、`timer`（高频 POSIX 定时器）、`idle`（大量短睡眠）、`traffic`（Wi-Fi ping 洪泛、fsync 写盘、USB 大流量）、`affinity`（轮换忙碌中断的路由）、`hotplug`（1–7 号核反复上下线）、`radio`（Wi-Fi 与蓝牙关开）、`copy`（写盘、丢页缓存、读回比 md5）、`pmu`（满负载下 10 kHz 全系统采样，关掉内核限流，先停监视器）、`all`（同时） | 中断注入与就地应答、核间中断陷入、分发器访问、电源调用、PMU 溢出路径 |
| `monitor_soak` | （可选）启用 EL2 监视器后加压，检查各核 EL2 周期单调递增 | 监视器与电源括号 |

## EL2 平行日志

计划里有 `el2_monitor` 时（`{"artifacts": "${XHYPER_MONITOR_ARTIFACTS}", "interval_s": 1}`，环境变量指向监视器的 `.ko` 目录）：开始时把监视器的内核模块和 `device/el2log.sh` 推到设备上；每次开机后（计划内的重启和意外重启都算）按依赖顺序加载模块、写 1 启动监视器、启动设备端采样循环。循环每秒把 `/proc/xcore_stats`（每个核的 EL2 周期数）连同开机时间、墙上时间和各簇的当前频率追加到 `/data/local/tmp/stab/el2/<开机号>.log`；它在设备上独立运行，睡眠和 adb 断开都不影响。主机在每次健康检查时把日志拉回 `<输出>/el2/` 并增量检查：

- `EL2_COUNT_DOWN`：某个核的计数倒退（监视器丢了累计周期）；
- `EL2_MISSING`：某次采样缺了某个核（驱动锁存了生命周期错误，或该核离线）；
- `EL2_HIGH`：某个核的 EL2 占比连续 5 次采样超过 50%。

每类前 20 次写进事件，其余只计数。结束时 `host/el2_report.py` 写出 `el2-summary.md`（每个阶段、每个核 EL2 占比的平均值、99 分位数、最大值，以及全部异常）和 `el2-per-second.csv`，按事件日志里的时间与各阶段对齐：样本归到包含它的最内层区间，即 `el2_stress` 的各个步骤；计划里重复的步骤记为 `<步骤>-r2`、`-r3`……，各占一行。占比是估计值：周期增量 ÷（时间增量 × 采样时刻的频率）。

开着监视器时，被测系统多了一个驱动、一个 PMU 使用者，每次深度空闲还多几次超级调用。出了问题，要去掉 `el2_monitor` 再跑一次，确认监视器有没有参与。

## 串口

`--serial-log <serial.txt>`：指向另一个进程正在写的串口抓取文件（每行带主机时间，例如工作区 `tools/perf/t11206-serial-capture.sh` 的输出；T11206 的 Linux 控制台和虚拟化层控制台共用这条 921600 的串口）。每次健康快照会往设备内核日志写一行 `STAB <阶段> <序号> <标签>`，这行也会出现在串口上，用来对齐两条时间线。每次健康检查扫描新增的串口输出：

- `SERIAL_FATAL`（错误）：`XHYPER PANIC`、`XHYPER_PANIC_HOLD`（虚拟化层的 panic 处理）、`MANAGER_FATAL`、`XHYPER_*ERROR`、`Kernel panic`，以及引导程序记录的上次复位原因（`exception_reboot_reason`、`exception_panic_reason`、`wdt fiq`）；
- `SERIAL_STALE`（警告）：两次检查之间串口没有任何输出（抓取停了，或者转接头、飞线断开）。

主机还按内核时间统计串口每 0.1 秒输出的字节数（只统计当前这次开机，保留最近 2 小时），给关抢占、关中断报告定级用（见下节）。

出现意外重启、失联、致命标记时，把事发前约 10 分钟到当前的串口内容另存为 `serial-NNN-<原因>.txt`。

## 故障分类

- **错误（任何一个都判 FAIL）**：`UNEXPECTED_REBOOT`（开机号在计划外改变，附开机原因和 pstore）、`DEVICE_GONE`（`lost_timeout_s` 内连不上，要人去看；设备在线但健康快照一直失败也算）、`BOOT_FAIL`、`RESUME_TIMEOUT`、`SUSPEND_FAIL`（睡眠失败计数增加）、`KERNEL_BAD`（新的 oops、锁死、RCU 停顿、看门狗、`WARNING: CPU` 行）、`STRESS_FAIL`、`MEMTEST_FAIL`、`IDENTITY_MISMATCH`、`MONITOR_FAIL`。
- 重启和唤醒后比较设备清单（PCI、USB、声卡、网络接口、输入设备），少了任何一项记 `DEVICE_MISSING`（错误）；dropbox 里崩溃、无响应、看门狗记录增加记警告。
- **警告**：`DEVICE_LOST`（暂时失联又回来了）、`PROBE_TIMEOUT`（健康快照超时，但 60 秒内 adb 就答复了，开机号没变：满负载下快照本身太慢；随即重拍一次，重拍也失败就记 `DEVICE_GONE` 并中止）、`KERNEL_LOG_CAPPED`（两次快照之间值得看的内核行超过 2000 行，只读了最新的 2000 行）、`HEALTH_DEGRADED`（设备上 `/data` 满了或不可写、或没有 awk，`health.sh` 退回每次只读最后 200 行内核行；每次开机报一次）、应用崩溃、无响应、墓碑文件、`SUSPEND_NOT_ENTERED`（睡眠在进入前被放弃）、`SUSPEND_ABORTED`（闹钟 2 秒内到期，或者冻结各核时有具名设备的唤醒中断待处理；按设备名判，不按中断号）、`SKIP`（工具缺失，或者某个压力项在这台设备上不能运行）。
- 厂商的关抢占、关中断报告（`preemptoff_warn`、`irqsoff_warn`，`D` 是时长）：100 毫秒以下记警告；100 毫秒及以上记 `KERNEL_BAD`，以下三种情况除外，降为警告并写明原因：串口同步打印所致（`[F - 0.2 秒, F + D]` 内打上的串口字节数不少于这段时间线路容量的一半，需要 `--serial-log`）；关抢占的区间落在另一个核的关中断区间里（跨核调用在等那个核，消息写明等的是哪次关中断，那次关中断自己照常定级）；与 `WARNING: CPU` 的打印重叠。
- 内核日志里的问题按内核时间归到发生时所在的阶段或步骤：每个阶段开始时往内核日志写一行 `STAB PHASE_START <阶段>` 并读回它的内核时间；由后面阶段的快照收集到的，事件的阶段写发生时的那个，消息末尾注明 `(collected in <收集时的阶段>)`。
- 同一次开机里，健康快照只读上次快照之后的内核行（`health.sh` 的第 2、3 个参数是上次的 `dmesg_last` 和开机号）。关抢占、关中断报告按（级别、报告类型、数字换成 N 的进程名、原因类别）分组，每个阶段每组前 3 条逐条记，其余每次快照每组记一条“N more like this”，事件带 `count`；`summary.md` 和 `compare.py` 按 `count` 累加，警告按类别各列前 20 条。
- 出错后先在原厂镜像上用同一个计划复现：两边都有的是平台或厂商的问题，只在虚拟化层下出现的才归我们。

## 注意

- 平板用 `su 0` 取得 root，不用 `adb root`。
- 插着 USB 时强制睡眠也能睡下去，但睡眠期间 adb 会断开。所以 `rtcwake` 在设备上脱离 adb 独立运行，主机这边等它断开、再等它重新连上（2026-10-13 在 T11206 上实测：`PM: suspend entry (deep)`，由闹钟中断唤醒，睡眠成功计数加 1）。
- monkey 只在 `packages` 列出的应用里操作，系统按键比例为 0（不会按电源键、音量键）。即便如此，它仍可能拉下通知栏、切换开关，所以不要把设置应用放进列表。
- stress-ng 的 `--cpu-online` 会反复让核心下线、上线，stress-ng 自己把它归为可能挂死机器的一类，要加 `--pathological` 才会运行。先在原厂镜像上试，再放进计划。
- 测试期间不要开 EL2 监视器（它占用 PMU），除非计划里有 `monitor_soak`。

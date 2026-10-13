# xhyper-stability：虚拟化层下 Android 设备的稳定性与压力测试

对运行在 XHyper 之下的 Android 设备（当前是 T11206 平板）做长时间的稳定性与压力测试，并且从主机一侧判断故障属于哪一层。

一般的 Android 稳定性测试假定故障会在 Android 里留下痕迹（崩溃记录、无响应、墓碑文件、内核日志）。虚拟化层出错时，最常见的结果是整机复位或冻住，Android 来不及记录。所以本仓库的检测从下往上做：设备还能不能连上，开机号有没有变，引导程序和 pstore 记下了什么，然后才看 Android 里的记录。

## 目录

| 路径 | 内容 |
|---|---|
| `host/stab.py` | 主机端调度：按计划依次执行各阶段，定期做健康快照，记录事件，写出总结。只用 Python 3 标准库 |
| `device/health.sh` | 设备端只读快照（以 root 运行）：开机号、开机原因、睡眠计数、频率、温度、pstore、内核异常行、崩溃缓冲区、无响应 |
| `plans/*.json` | 测试计划：`smoke.json`（每个阶段跑一次，约 20 分钟），`overnight.json`（约 12 小时） |
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
| `soak` | 限定应用的 monkey 随机操作；每隔一段时间跑一次 stress-ng 定向压力；定期灭屏亮屏 | 核间中断（`--affinity --switch --futex --membarrier --tlb-shootdown`）、定时器（`--timer --hrtimers --cyclic`）、内存（`--vm`）、各类设备中断 |
| `suspend` | 用 `rtcwake -m mem` 强制系统睡眠，由闹钟唤醒，核对睡眠成功和失败计数 | 系统睡眠、各核掉电和上电、唤醒中断 |
| `memtest` | stressapptest 在高负载下检查内存内容 | 第二阶段映射、缓存维护；出错时报数据不一致 |
| `latency` | cyclictest 测空闲和有负载时的唤醒延迟 | 定时器注入、唤醒路径 |
| `standby` | 长时间灭屏睡眠，定时唤醒检查 | 长时间深度睡眠后的恢复 |
| `monitor_soak` | （可选）启用 EL2 监视器后加压，检查各核 EL2 周期单调递增 | 监视器与电源括号 |

## EL2 平行日志

计划里有 `el2_monitor` 时（`{"artifacts": "${XHYPER_MONITOR_ARTIFACTS}", "interval_s": 1}`，环境变量指向监视器的 `.ko` 目录）：开始时把监视器的内核模块和 `device/el2log.sh` 推到设备上；每次开机后（计划内的重启和意外重启都算）按依赖顺序加载模块、写 1 启动监视器、启动设备端采样循环。循环每秒把 `/proc/xcore_stats`（每个核的 EL2 周期数）连同开机时间、墙上时间和各簇的当前频率追加到 `/data/local/tmp/stab/el2/<开机号>.log`；它在设备上独立运行，睡眠和 adb 断开都不影响。主机在每次健康检查时把日志拉回 `<输出>/el2/` 并增量检查：

- `EL2_COUNT_DOWN`：某个核的计数倒退（监视器丢了累计周期）；
- `EL2_MISSING`：某次采样缺了某个核（驱动锁存了生命周期错误，或该核离线）；
- `EL2_HIGH`：某个核的 EL2 占比连续 5 次采样超过 50%。

每类前 20 次写进事件，其余只计数。结束时 `host/el2_report.py` 写出 `el2-summary.md`（每个阶段、每个核 EL2 占比的平均值、99 分位数、最大值，以及全部异常）和 `el2-per-second.csv`，按事件日志里的时间与各阶段对齐。占比是估计值：周期增量 ÷（时间增量 × 采样时刻的频率）。

开着监视器时，被测系统多了一个驱动、一个 PMU 使用者，每次深度空闲还多几次超级调用。出了问题，要去掉 `el2_monitor` 再跑一次，确认监视器有没有参与。

## 故障分类

- **错误（任何一个都判 FAIL）**：`UNEXPECTED_REBOOT`（开机号在计划外改变，附开机原因和 pstore）、`DEVICE_GONE`（`lost_timeout_s` 内连不上，要人去看）、`BOOT_FAIL`、`RESUME_TIMEOUT`、`SUSPEND_FAIL`（睡眠失败计数增加）、`KERNEL_BAD`（新的 oops、锁死、RCU 停顿、看门狗、`WARNING: CPU` 行）、`STRESS_FAIL`、`MEMTEST_FAIL`、`IDENTITY_MISMATCH`、`MONITOR_FAIL`。
- **警告**：`DEVICE_LOST`（暂时失联又回来了）、应用崩溃、无响应、墓碑文件、`SUSPEND_NOT_ENTERED`（睡眠在进入前被放弃）、`SKIP`（工具缺失，或者某个压力项在这台设备上不能运行）。
- 出错后先在原厂镜像上用同一个计划复现：两边都有的是平台或厂商的问题，只在虚拟化层下出现的才归我们。

## 注意

- 平板用 `su 0` 取得 root，不用 `adb root`。
- 插着 USB 时强制睡眠也能睡下去，但睡眠期间 adb 会断开。所以 `rtcwake` 在设备上脱离 adb 独立运行，主机这边等它断开、再等它重新连上（2026-10-13 在 T11206 上实测：`PM: suspend entry (deep)`，由闹钟中断唤醒，睡眠成功计数加 1）。
- monkey 只在 `packages` 列出的应用里操作，系统按键比例为 0（不会按电源键、音量键）。即便如此，它仍可能拉下通知栏、切换开关，所以不要把设置应用放进列表。
- stress-ng 的 `--cpu-online` 会反复让核心下线、上线，stress-ng 自己把它归为可能挂死机器的一类，要加 `--pathological` 才会运行。先在原厂镜像上试，再放进计划。
- 测试期间不要开 EL2 监视器（它占用 PMU），除非计划里有 `monitor_soak`。

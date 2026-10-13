# 第三方压力测试工具

交叉编译为静态 aarch64 二进制，推送到平板的 `/data/local/tmp/stress/` 运行。版本固定在 `versions.lock`（名称、地址、标签、完整提交）。

## 工具用途
- stress-ng V0.22.01：CPU、内存、IO、调度、系统调用等数百种压力项（`stress-ng --stressors` 的列表见 `logs/stress-ng-stressors.txt`）。
- stressapptest v1.0.11：内存和缓存一致性压力，结尾必须是 `Status: PASS`。
- rt-tests v2.11：`cyclictest` 测调度延迟，另有 `hackbench`（调度压力）和 `pi_stress`（优先级继承）。
- Fastbot（可选）：随机 UI 测试，只下载预编译文件，不在本阶段运行。

## 重新编译
1. 在一台 x86_64 Linux 主机上编译（需要 `aarch64-linux-gnu-gcc`、`aarch64-linux-gnu-g++`、autoconf、automake、libtool、git；下载源码需要代理时先设置 `http_proxy`/`https_proxy`）。
2. 把 `versions.lock` 和 `build-aarch64-static.sh` 放到同一目录，运行 `./build-aarch64-static.sh > build.log 2>&1`（约 3 分钟，可以放后台跑）。
3. stressapptest 需要 `aarch64-linux-gnu-g++`；numactl 需要 autoconf、automake、libtool。
4. rt-tests 2.11 无条件包含 `numa.h`，所以脚本先从 numactl v2.0.19 构建静态 `libnuma.a` 再链接，没有改任何源码。libcpupower 未启用，所以 cyclictest 没有 `--deepest-idle-state`。
5. 产物在 `out/aarch64/`（二进制、`SHA256SUMS`、`BUILDINFO.txt`），拷回运行 `host/stab.py` 的机器上本仓库的 `third_party/out/aarch64/`。`out/` 与 `src/` 被 git 忽略。
6. Fastbot：`./fetch-fastbot.sh` 下载固定提交的预编译文件到 `out/fastbot/`，并生成 `SHA256SUMS`。启动命令（README 原文）：
   `adb shell CLASSPATH=/sdcard/monkeyq.jar:/sdcard/framework.jar:/sdcard/fastbot-thirdpart.jar exec app_process /system/bin com.android.commands.monkey.Monkey -p <包名> --agent reuseq --running-minutes <分钟> --throttle <毫秒> -v -v`，需先把 jar 推到 `/sdcard/`，`libs/*` 推到 `/data/local/tmp/`。

## 板上冒烟结果（2026-10-13，平板内核 5.15.178-android13，每项不超过 10 秒）
- `stress-ng --version`：`stress-ng, version 0.22.01 (gcc 15.2.0, aarch64 Linux ...)`。
- `stress-ng --cpu 1 --timeout 3s --metrics-brief`：passed 1，failed 0，`successful run completed in 3.03 secs`。
- `stressapptest -s 5 -M 64`：`Status: PASS - please verify no corrected errors`，0 个硬件事件，0 个错误。
- `cyclictest -m -p 80 -t 1 -l 1000 -q`（`su 0`）：`T: 0 ( 4725) P:80 I:1000 C: 1000 Min: 39 Act: 132 Avg: 145 Max: 1073`。
  注意：rt-tests 2.11 已删除 `-n` 选项（默认就是 clock_nanosleep），带 `-n` 会报 invalid option。运行时会打印 `ERROR: shm_open No such file or directory`，不影响结果。
- `hackbench --help`、`pi_stress --help` 均能运行。


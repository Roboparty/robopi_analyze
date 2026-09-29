# robopi-analyze

RoboPi HPM、BMS、电源和通信故障分析工具包，面向 Ubuntu 24.04 ARM64 RoboPi
系统。包内包含七维日志采集、USB-CAN 抓包、CAN ASC、HPM 串口和推理进程分析工具。

## 服务职责

- `hpm-log-capture.service` 是常驻 HPM 日志守护进程，持续读取 `/dev/ttyS4`。
- `hpm-sreset.service` 启动时先执行一次 HPM 正常启动复位，之后作为常驻监视器，
  找不到 HPM 时执行复位。
- `usbcan-capture.service` 安装后默认启用，系统下次启动时自动开始七维采集。
- `usbcan-debug-snapshot` 用于机器人即将下电或故障发生时保存完整现场。

安装包后 HPM 服务自动启动。需要采集故障现场时执行：

```bash
sudo systemctl start usbcan-capture.service
```

查看状态：

```bash
sudo robopi-comm-status
```

结束采集：

```bash
sudo systemctl stop usbcan-capture.service
```

停止七维采集不会停止 HPM 日志守护进程。

## 七个维度

1. BMS/电源：周期记录 `bms.service` 状态。
2. CAN 状态：记录四路 CAN 的状态、错误计数和收发统计。
3. 内核日志：记录动态 `dmesg`，用于定位驱动和 USB 事件。
4. HPM 串口：记录 `/dev/ttyS4` 的 HPM 固件日志，默认 921600 8N1。
5. USB-CAN：记录 `usbmon` 原始 USB URB PCAP。
6. CAN ASC：采集 `can0` 到 `can3` 并转换为 Vector ASC 格式。
7. 推理输出：记录 `inference_session` screen 会话文本。

没有对应设备、工具或 screen 会话时，对应文件保持为空，其他维度继续运行。

## 七维采集

命令入口：

```bash
sudo robopi-seven-capture /home/robo/robopi-logs/seven-$(date +%Y%m%d-%H%M%S)
```

也可以使用 systemd 入口：

```bash
sudo systemctl start usbcan-capture.service
```

默认 screen 会话名为 `inference_session`，临时指定其他会话：

```bash
sudo env INFERENCE_SCREEN_SESSION=my_inference \
  robopi-seven-capture /home/robo/robopi-logs/seven-$(date +%Y%m%d-%H%M%S)
```

按 `Ctrl-C` 结束命令入口的采集。会话目录包含：

```text
manifest.json
bms-status.txt
can-details.jsonl
dmesg-live.txt
hpm-uart-live.txt
usbcan.pcap*
can.log
can.asc
inference-session.txt
```

## USB-CAN 配置

循环 PCAP 默认写入 `/run/usbcan/usbcan.pcap*`，配置文件为
`/etc/default/usbcan-capture`：

```text
USBMON_IFACE=auto
CAN_INTERFACE=can3
CAPTURE_DIR=/run/usbcan
FILE_SIZE_MB=64
FILE_COUNT=8
```

默认保留 8 个、每个约 64 MB 的文件，达到数量后覆盖最旧文件。自动识别 USB Bus
异常时可以查看：

```bash
lsusb
lsusb -t
sudo tcpdump -D | grep usbmon
```

HPM 日志实时查看：

```bash
sudo journalctl -fu hpm-log-capture.service
```

## 紧急快照

故障出现或机器人即将下电时执行：

```bash
sudo usbcan-debug-snapshot
sync
```

默认生成以下内容：

```text
/home/robo/usbcan-snapshots/YYYYMMDD-HHMMSS/
/home/robo/usbcan-snapshots/YYYYMMDD-HHMMSS.zip
/home/robo/usbcan-snapshots/YYYYMMDD-HHMMSS.zip.sha256
```

快照会暂停七维采集服务，复制 PCAP 和当前状态后恢复服务；HPM 守护进程始终保持
运行。自定义输出目录：

```bash
sudo env USBCAN_SNAPSHOT_DIR=/mnt/logs usbcan-debug-snapshot
```

校验压缩包：

```bash
cd /home/robo/usbcan-snapshots
sha256sum -c YYYYMMDD-HHMMSS.zip.sha256
unzip -l YYYYMMDD-HHMMSS.zip | head
```

## 分析与导出

分析七维快照，会生成 `timeline.csv` 和 `summary.json`：

```bash
robopi-seven-analyze /home/robo/usbcan-snapshots/YYYYMMDD-HHMMSS
```

导出 ZIP 和 SHA-256：

```bash
robopi-seven-export /home/robo/usbcan-snapshots/YYYYMMDD-HHMMSS
```

只分析 USB-CAN PCAP：

```bash
analyze-ethercan-pcap /home/robo/usbcan-snapshots/YYYYMMDD-HHMMSS --top 50
```

第一次分析建议不要指定 Bus、Device 或 Endpoint，先根据报告确认设备位置，再按需
添加 `--bus`、`--device` 或 `--endpoint` 过滤条件。

## 文件索引

### 七个维度

| 文件 | 维度 | 输出文件 |
|---|---|---|
| `dimensions/01_bms_status.sh` | BMS/电源状态 | `bms-status.txt` |
| `dimensions/02_can_details.sh` | CAN 状态和统计 | `can-details.jsonl` |
| `dimensions/03_kernel_dmesg.sh` | 内核动态日志 | `dmesg-live.txt` |
| `dimensions/04_hpm_uart.sh` | HPM `ttyS4` 日志 | `hpm-uart-live.txt` |
| `dimensions/05_usb_pcap.sh` | USB-CAN 原始抓包 | `usbcan.pcap*` |
| `dimensions/06_can_asc.sh` | 四路 CAN ASC 日志 | `can.asc` |
| `dimensions/07_inference_screen.sh` | inference screen 输出 | `inference-session.txt` |

### 采集和分析

| 文件 | 作用 |
|---|---|
| `capture/capture_seven_dimensions.sh` | 七维同步采集编排 |
| `capture/capture_usbcan_ring.sh` | USB-CAN 循环抓包实现 |
| `capture/capture_hpm_uart.sh` | HPM 原始串口读取实现 |
| `bin/save_usbcan_snapshot.sh` | 保存七维故障快照、ZIP 和 SHA-256 |
| `bin/export_seven_dimensions.sh` | 分析、打包七维会话 |
| `bin/restart_can_interfaces.sh` | 重启四路 CAN，不修改 CAN 配置 |
| `bin/show_communication_dashboard.py` | 在线通信状态面板 |
| `analysis/analyze_seven_dimensions.py` | 合并七维日志时间线 |
| `analysis/analyze_usbcan_urb.py` | 分析 USB URB 状态和错误 |
| `analysis/analyze_usbcan_timeline.py` | 按时间窗口统计 USB 流量 |
| `analysis/analyze_usbcan_echo.py` | 检查 CAN TX echo context |
| `analysis/compare_usbcan_captures.py` | 对比 USB-CAN 抓包 |
| `analysis/extract_usbcan_error_windows.py` | 截取 USB 错误时间窗口 |
| `analysis/check_usbcan_snapshot.sh` | 检查快照文件完整性 |

### HPM 工具

| 文件 | 作用 |
|---|---|
| `bin/flash_hpm.sh` | 擦除并烧录 HPM 固件 |
| `bin/hpmtool.py` | HPM USB BootROM 工具 |
| `etc/firmware/` | 随包提供的 HPM/EtherCANFD 固件 |

安装后的稳定命令包括 `robopi-seven-capture`、`robopi-seven-analyze`、
`robopi-seven-export`、`robopi-comm-status`、`usbcan-capture`、
`usbcan-debug-snapshot`、`hpm-log-capture`、`robopi-can-capture` 和
`robopi-can-restart`。HPM 自动复位由 `hpm-sreset.service` 在后台处理。

源码目录职责：

```text
bin/         可直接执行的运维、烧录和导出命令
capture/     七维采集流程脚本
dimensions/  七个独立日志采集维度
analysis/    离线分析程序
tests/       测试脚本、样例抓包和分析结果
README.md     项目说明和使用文档
etc/         默认配置、systemd 单元和固件
```

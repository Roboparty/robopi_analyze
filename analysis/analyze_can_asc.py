#!/usr/bin/env python3
# Copyright (C) 2026 wentywenty
# SPDX-License-Identifier: GPL-3.0
"""Analyze periodicity, jitter, gaps, bus load and node presence in a Vector ASC log.

Reads a session's can.asc and reports, per (channel, frame id):

  - period and robust jitter: intervals beyond 2x the median period are
    excluded from the stddev and counted separately, so one long stop does
    not drown the real sub-millisecond jitter of the running segments;
  - bus-idle events: windows where a whole channel goes silent. When every
    id stops at the same instant the transmitter was stopped (robot powered
    down); those frames were never sent, not lost;
  - single-id gap anomalies that occur while other ids keep transmitting —
    those are the ones that suggest real frame loss;
  - estimated bus load and per-id presence.

Ids whose average rate is below --min-rate are event-like (e.g. Device_Cmd
0x7FF sent in short config bursts); median/std statistics on burst spacing
are meaningless, so they are listed separately instead of polluting the
periodicity tables.

Accepts either the can.asc file itself or a session directory.
"""

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path

import can


def resolve_input(arg):
    path = Path(arg)
    if path.is_dir():
        asc = path / "can.asc"
        if not asc.is_file():
            raise SystemExit(f"analyze-can-asc: {path} is a directory with no can.asc")
        return asc
    return path


def collect(path, channel_filter):
    """Stream the ASC once: {(channel, id): [timestamps...]} plus global range."""
    series = defaultdict(list)
    first = last = None
    total = 0
    for msg in can.ASCReader(str(path)):
        if channel_filter is not None and msg.channel != channel_filter:
            continue
        total += 1
        series[(msg.channel, msg.arbitration_id)].append(msg.timestamp)
        if first is None:
            first = msg.timestamp
        last = msg.timestamp
    return series, first, last, total


def frame_bit_time(msg, bitrate, data_bitrate):
    """Rough on-wire time of one frame in seconds; bit stuffing not included."""
    if msg.is_fd:
        arbitration = 70 if msg.is_extended_id else 50
        data = 8 * len(msg.data) + 28
        return arbitration / bitrate + data / data_bitrate
    overhead = 67 if msg.is_extended_id else 47
    return (overhead + 8 * len(msg.data)) / bitrate


def bus_load(path, channel_filter, bitrate, data_bitrate, duration):
    used = defaultdict(float)
    for msg in can.ASCReader(str(path)):
        if channel_filter is not None and msg.channel != channel_filter:
            continue
        used[msg.channel] += frame_bit_time(msg, bitrate, data_bitrate)
    return {channel: seconds / duration for channel, seconds in used.items()}


def timing_stats(timestamps):
    if len(timestamps) < 2:
        return None
    gaps = [b - a for a, b in zip(timestamps, timestamps[1:])]
    median = statistics.median(gaps)
    if median <= 0:
        return None
    # Robust jitter: intervals beyond 2x the median period are outliers
    # (stops, stalls). Excluding them keeps one long silence from
    # dominating the stddev; they are accounted in the idle/gap sections.
    normal = [g for g in gaps if g <= 2 * median]
    return {
        "frames": len(timestamps),
        "median": median,
        "period_ms": median * 1000,
        "jitter_ms": (statistics.stdev(normal) if len(normal) > 1 else 0.0) * 1000,
        "min_ms": min(normal) * 1000,
        "max_ms": max(normal) * 1000,
        "outliers": len(gaps) - len(normal),
        "gaps": gaps,
    }


def merged_channel_gaps(series, min_gap):
    """Gaps of at least min_gap seconds in each channel's merged timestamp
    stream (all ids of the channel collapsed together)."""
    per_channel = defaultdict(list)
    for (channel, _), stamps in series.items():
        per_channel[channel].extend(stamps)
    windows = {}
    for channel, stamps in per_channel.items():
        stamps.sort()
        windows[channel] = [(a, b) for a, b in zip(stamps, stamps[1:])
                            if b - a >= min_gap]
    return windows


def channel_idle_events(series, min_idle):
    """Idle windows per channel, merged across channels when overlapping.

    A window is a gap of at least min_idle seconds in the merged timestamp
    stream of one channel. Windows from different channels that overlap are
    merged into a single event so "all channels silent together" (i.e. the
    transmitter stopped, nothing was lost) is visible at a glance.
    """
    raw = []
    for channel, wins in merged_channel_gaps(series, min_idle).items():
        for a, b in wins:
            raw.append((a, b, {channel}))
    raw.sort()
    events = []
    for a, b, chans in raw:
        if events and a <= events[-1][1]:
            ea, eb, ec = events[-1]
            events[-1] = (ea, max(eb, b), ec | chans)
        else:
            events.append((a, b, set(chans)))
    return events


def mostly_inside(events, a, b, fraction=0.5):
    """True when at least `fraction` of [a, b] overlaps one of the windows.

    An id's gap around a channel-wide halt is typically a few ms wider than
    the halt itself (that id happened to stop slightly earlier and resume
    slightly later than the channel as a whole), so strict containment
    would miss it; overlap-based attribution does not.
    """
    span = b - a
    if span <= 0:
        return False
    for event in events:
        overlap = min(b, event[1]) - max(a, event[0])
        if overlap > 0 and overlap / span >= fraction:
            return True
    return False


def render_table(headers, rows):
    """Print a padded table; column width follows the widest cell, so huge
    values can no longer glue columns together."""
    cells = [[str(h) for h in headers]]
    for row in rows:
        cells.append([str(c) for c in row])
    widths = [max(len(row[i]) for row in cells) for i in range(len(headers))]
    for row in cells:
        print("  " + "  ".join(row[i].ljust(widths[i])
                               for i in range(len(headers))).rstrip())


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("asc", help="can.asc 文件,或会话目录(自动取其中的 can.asc)")
    parser.add_argument("--channel", type=int, help="只分析指定通道(0 = can0)")
    parser.add_argument("--min-frames", type=int, default=20,
                        help="参与周期分析的最少帧数(默认 20)")
    parser.add_argument("--min-rate", type=float, default=1.0,
                        help="平均速率低于此值(帧/秒)的 ID 不参与周期分析(默认 1.0)")
    parser.add_argument("--gap-ratio", type=float, default=1.5,
                        help="间隔超过周期的多少倍记为异常(默认 1.5)")
    parser.add_argument("--idle-secs", type=float, default=1.0,
                        help="整通道静默至少这么多秒记为一次总线静默(默认 1.0)")
    parser.add_argument("--stall-secs", type=float, default=0.005,
                        help="整通道同步停顿的归因门槛,秒(默认 0.005)")
    parser.add_argument("--top", type=int, default=10, help="最多列出的条目数(默认 10)")
    parser.add_argument("--bitrate", type=float, default=1_000_000,
                        help="标称波特率,用于负载率估算(默认 1000000)")
    parser.add_argument("--data-bitrate", type=float, default=5_000_000,
                        help="CAN FD 数据段波特率(默认 5000000)")
    parser.add_argument("--json", type=Path, help="把完整结果写成 JSON")
    args = parser.parse_args()

    asc = resolve_input(args.asc)
    if not asc.is_file():
        parser.error(f"ASC file not found: {asc}")
    if asc.stat().st_size == 0:
        parser.error(
            f"{asc} is empty (0 bytes) — the capture recorded no CAN traffic.\n"
            "  can0-can3 only carry frames while the robot's control loop is running,\n"
            "  so an empty can.asc means the buses were idle, not that ASC conversion failed.\n"
            "  Check before recording with:\n"
            "    ip -br link show type can\n"
            "    cat /sys/class/net/can0/statistics/rx_packets   # 隔一秒再看一次")

    series, first, last, total = collect(asc, args.channel)
    if not total:
        parser.error("no frames matched the given filters (check --channel)")
    duration = last - first
    active_channels = sorted({channel for channel, _ in series})

    print("=== 概览 ===")
    print(f"文件:          {asc}")
    print(f"总帧数:        {total}")
    print(f"时间跨度:      {duration:.3f} 秒")
    print(f"平均速率:      {total / duration:.0f} 帧/秒")
    print(f"唯一 (通道,ID): {len(series)}")

    per_channel = defaultdict(int)
    for (channel, _), stamps in series.items():
        per_channel[channel] += len(stamps)
    print("\n=== 按通道 ===")
    render_table(("通道", "帧数", "速率(帧/秒)", "占比"),
                 [(f"can{ch}", per_channel[ch], f"{per_channel[ch] / duration:.0f}",
                   f"{per_channel[ch] / total:.1%}") for ch in sorted(per_channel)])

    # Split ids into periodic (analyzed) and sporadic (listed only).
    periodic = []
    sporadic = []
    for (channel, arb_id), stamps in series.items():
        rate = len(stamps) / duration
        stats = timing_stats(stamps)
        if stats and stats["frames"] >= args.min_frames and rate >= args.min_rate:
            periodic.append((channel, arb_id, stamps, stats))
        else:
            sporadic.append((channel, arb_id, len(stamps), rate))

    # Merged cross-channel events at the stall threshold; the >= idle-secs
    # subset is reported as bus idle, the shorter ones as scheduler-beat
    # stalls. Both are attributed per id by overlap, not per-id counting.
    all_events = channel_idle_events(series, args.stall_secs)
    idle_events = [e for e in all_events if e[1] - e[0] >= args.idle_secs]
    micro_stalls = [e for e in all_events if e[1] - e[0] < args.idle_secs]

    # Attribute each oversized gap three ways: a bus-idle window (>= idle-secs,
    # transmitter stopped), a channel-synchronized halt (scheduler beat, not
    # per-node loss), or a genuine single-id anomaly (other ids kept
    # transmitting).
    anomalies = []
    idle_explained = 0
    for channel, arb_id, stamps, stats in periodic:
        for index, gap in enumerate(stats["gaps"]):
            if gap <= stats["median"] * args.gap_ratio:
                continue
            start, end = stamps[index], stamps[index + 1]
            if mostly_inside(idle_events, start, end):
                idle_explained += 1
            elif mostly_inside(all_events, start, end):
                continue
            else:
                anomalies.append({"channel": channel, "id": arb_id, "at": start,
                                  "gap_ms": gap * 1000, "period_ms": stats["period_ms"],
                                  "missed": max(0, round(gap / stats["median"]) - 1)})
    anomalies.sort(key=lambda item: item["gap_ms"], reverse=True)

    print(f"\n=== 周期性 / 抖动 (稳健; 帧数 >= {args.min_frames} 且速率 >= {args.min_rate} 帧/秒) ===")
    rows = []
    for channel, arb_id, _, stats in periodic:
        ratio = stats["jitter_ms"] / stats["period_ms"] if stats["period_ms"] else 0
        rows.append((f"can{channel}", f"0x{arb_id:X}", stats["frames"],
                     f"{stats['period_ms']:.3f}", f"{stats['jitter_ms']:.4f}",
                     f"{stats['min_ms']:.3f}", f"{stats['max_ms']:.3f}",
                     stats["outliers"], f"{ratio:.2%}"))
    rows.sort(key=lambda r: -float(r[8].rstrip("%")))
    render_table(("通道", "帧ID", "帧数", "周期(ms)", "抖动std", "最小", "最大",
                  "离群间隔", "抖动率"), rows)
    print("  抖动率 = std / 周期,越小越稳;离群间隔 = 超过 2 倍周期被剔除的间隔数(见静默/异常分析)")

    if sporadic:
        print("\n=== 低频/非周期报文 (不适用周期分析) ===")
        render_table(("通道", "帧ID", "帧数", "平均速率(帧/秒)"),
                     sorted((f"can{ch}", f"0x{a:X}", n, f"{r:.3f}")
                            for ch, a, n, r in sporadic))
        print("  突发/事件型报文(如配置指令),无固定周期,周期和抖动统计对它们没有意义")

    print(f"\n=== 总线静默 (整通道 >= {args.idle_secs}s 无任何帧) ===")
    if idle_events:
        idle_events.sort(key=lambda e: e[1] - e[0], reverse=True)
        rows = []
        for start, end, chans in idle_events[:args.top]:
            note = "全部通道同步" if len(chans) == len(active_channels) else \
                   f"{len(chans)}/{len(active_channels)} 通道"
            rows.append((f"{start - first:.3f}", f"{end - first:.3f}",
                         f"{end - start:.1f}", note))
        render_table(("起(s)", "止(s)", "时长(s)", "范围"), rows)
        print("  各 ID 同步停止 = 发送方停机(机器人下电/暂停),不计为丢帧")
    else:
        print("  未发现总线静默")

    print("\n=== 异常间隔 (疑似丢帧:该 ID 断流期间其他 ID 仍在发送) ===")
    if anomalies:
        render_table(("通道", "帧ID", "时刻(s)", "间隔(ms)", "正常(ms)", "疑似丢帧"),
                     [(f"can{a['channel']}", f"0x{a['id']:X}", f"{a['at'] - first:.3f}",
                       f"{a['gap_ms']:.3f}", f"{a['period_ms']:.3f}", a["missed"])
                      for a in anomalies[:args.top]])
    else:
        print("  未发现")
    if idle_explained:
        print(f"  另有 {idle_explained} 处大间隔落在总线静默窗口内,归因为发送方停机")
    if micro_stalls:
        durations = [e[1] - e[0] for e in micro_stalls]
        print(f"  另有 {len(micro_stalls)} 次整通道同步停顿(所有 ID 同时断流,"
              f"中位 {statistics.median(durations) * 1000:.1f} ms / "
              f"最大 {max(durations) * 1000:.1f} ms)——"
              f"指向发送方/调度节拍,而非个别节点丢帧")

    print("\n=== 总线负载率 (估算) ===")
    print(f"标称速率 {args.bitrate / 1e6:.3f} Mbps / 数据速率 {args.data_bitrate / 1e6:.3f} Mbps")
    loads = bus_load(asc, args.channel, args.bitrate, args.data_bitrate, duration)
    render_table(("通道", "占用时间(s)", "负载率"),
                 [(f"can{ch}", f"{load * duration:.3f}", f"{load:.2%}")
                  for ch, load in sorted(loads.items())])
    print("  不含位填充,实际负载会略高于此值")

    print("\n=== 节点在线区间 ===")
    node_rows = []
    for (channel, arb_id), stamps in sorted(series.items()):
        stats = timing_stats(stamps)
        tolerance = max(3 * stats["median"], 1.0) if stats else 1.0
        first_off = stamps[0] - first
        last_off = stamps[-1] - first
        notes = []
        if first_off > tolerance:
            notes.append(f"中途出现(+{first_off:.3f}s)")
        if duration - last_off > tolerance:
            notes.append(f"提前消失(-{duration - last_off:.3f}s)")
        node_rows.append((f"can{channel}", f"0x{arb_id:X}", len(stamps),
                          f"{first_off:.3f}", f"{last_off:.3f}",
                          " / ".join(notes) if notes else "全程在线"))
    render_table(("通道", "帧ID", "帧数", "首次(s)", "最后(s)", "状态"), node_rows)

    if args.json:
        report = {
            "file": str(asc), "frames": total, "duration_s": duration,
            "timing": [{"channel": ch, "id": f"0x{a:X}", **{k: stats[k] for k in
                        ("frames", "period_ms", "jitter_ms", "min_ms", "max_ms", "outliers")}}
                       for ch, a, _, stats in periodic],
            "sporadic": [{"channel": ch, "id": f"0x{a:X}", "frames": n, "rate_hz": round(r, 4)}
                         for ch, a, n, r in sporadic],
            "idle_events": [{"start_s": s - first, "end_s": e - first,
                             "duration_s": round(e - s, 3), "channels": sorted(c)}
                            for s, e, c in idle_events],
            "gap_anomalies": [{"channel": a["channel"], "id": f"0x{a['id']:X}",
                               "at_s": a["at"] - first, "gap_ms": round(a["gap_ms"], 3),
                               "period_ms": round(a["period_ms"], 3), "missed": a["missed"]}
                              for a in anomalies],
            "channel_stalls": {"count": len(micro_stalls),
                               "median_ms": round(statistics.median([e[1] - e[0] for e in micro_stalls]) * 1000, 2) if micro_stalls else None,
                               "max_ms": round(max(e[1] - e[0] for e in micro_stalls) * 1000, 2) if micro_stalls else None},
            "bus_load": {f"can{ch}": load for ch, load in loads.items()},
        }
        args.json.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
        print(f"\nJSON 报告: {args.json}")


if __name__ == "__main__":
    main()

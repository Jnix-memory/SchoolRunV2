#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FIT 文件生成器（V2 改进版）
以 data/standard.fit 为母版拟合"胶囊形"跑道（直道 + 半圆），按目标距离绕圈生成
拟真 GPS 轨迹；叠加"整体递减 + 每公里随机"的速度曲线，并按慢跑动力学人设写入
逐点/会话运动学数据（步频半值、功率、触地时间），兼容 Keep / Garmin Connect 导入。

用法: python generate_fit.py <个人编号> <日期> <开始时间> <运动总时长> [距离公里]
示例: python generate_fit.py 021 20260414 1315 2103 3.0
说明:
  * 未给距离时按"三公里为主"随机取 3.00~3.29 km；
  * 时间戳按 FIT 纪元约定写入，FileId 必须为文件首条消息；
  * 跑步 cadence 按 Garmin 真实文件惯例存"半值"(步频÷2)，App 显示时 ×2；
  * 方法学与常量参考 SchoolRunV1 FIT_GENERATOR_TECH_REPORT（胶囊跑道/速度曲线/动力学人设）。
"""

import os
import sys
import math
import random
from datetime import datetime

from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.file_id_message import FileIdMessage, FileType, Manufacturer
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.messages.session_message import SessionMessage
from fit_tool.profile.messages.lap_message import LapMessage
from fit_tool.profile.messages.activity_message import ActivityMessage
from fit_tool.profile.profile_type import Sport, SubSport

# 本地胶囊拟合模块（tools/ 目录）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import capsule_track as ct

# 母版文件：data/standard.fit（与 SchoolRunV1 的 1.fit 同源，含逐圈 Lap 消息）
MASTER_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           'data', 'standard.fit')

# ---- 轨迹采样参数（报告 final_constants）----
REC_SPACING_M = 3.0        # 记录点间距（米）
JITTER_RATIO = 0.25        # 横向抖动触发率
JITTER_AMP_M = 10.0        # 横向波动峰值 ≤10m（低频平滑 + 峰值归一）
TRACK_SHIFT_EAST_M = 10.0  # 生成轨迹整体往正东平移(米)，修正与实地跑道的定位偏差

# ---- 速度模型 ----
TREND_DECAY = 0.2          # 线性档(5km+): 整体递减：末端速度 = 首端 × 80%
PER_KM_AMP = 0.05          # 线性档每公里随机幅度 ±5%
PACE_SMOOTH_M = 200.0      # 线性档配速平滑相关长度
# 3km 档（<4km）："快跑段 + 中段慢走(3~4km/h)"强变化剖面（capsule_track 的 walk 模式）
STEP_DROP_3K = 0.45        # 兜底/速度上限估算用的降幅参考

# ---- 慢跑动力学人设（报告 FEAT-07 / final_constants）----
AVG_SPM = 158.0            # 平均步频（步/分）
CAD_HALF_AVG = 79          # 会话平均步频"半值"（=158÷2，Keep 显示时 ×2）
AVG_POWER_W = 160.0        # 平均功率（W）
AVG_STANCE_MS = 247.0      # 平均触地时间（ms）


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def generate_fit(user_id, date, start_time, duration, output_path=None, distance=None):
    """
    生成带真实轨迹的 FIT 文件。

    Args:
        user_id:    个人编号（3 位数字字符串）
        date:       日期，格式 YYYYMMDD
        start_time: 开始时间，格式 HHMM（24 小时制）
        duration:   运动总时长，格式 MMSS
        output_path: 输出 .fit 路径；默认 data/<user_id>.fit
        distance:   可选，距离（公里）。None 时随机取 3.00~3.29 km（三公里为主）。
    """
    try:
        # ---- 解析输入 ----
        year, month, day = int(date[:4]), int(date[4:6]), int(date[6:8])
        hour, minute = int(start_time[:2]), int(start_time[2:4])
        duration_min, duration_sec = int(duration[:2]), int(duration[2:4])
        total_duration = duration_min * 60 + duration_sec
        if total_duration <= 0:
            print("错误: 运动时长必须大于0")
            return False

        # ---- 距离（米）：用户指定优先；否则 3.00~3.29 km（三公里为主）----
        if distance is not None:
            distance_m = float(distance) * 1000.0
            if distance_m <= 0:
                print("错误: 距离必须大于0")
                return False
        else:
            distance_m = random.uniform(3.00, 3.29) * 1000.0

        # ---- 1) 从母版拟合胶囊跑道 ----
        capsule = ct.fit_capsule_from_master(MASTER_PATH)
        if capsule is None:
            print("错误: 无法从母版拟合跑道（%s 缺失或无效）" % MASTER_PATH)
            return False
        print("跑道拟合: 半直道=%.2fm 半径=%.2fm 单圈=%.2fm 残差=%.2fm%s" % (
            capsule['a'], capsule['r'], capsule['L0'], capsule['residual'],
            (' [降级:%s]' % capsule['reason']) if not capsule['ok'] else ''))

        # ---- 2) 绕圈采样 + 横向低频抖动 + 终点出跑道 + 长度标定 ----
        out_m, out_dist, _out_lap, scale = ct.generate_track_loop(
            capsule, distance_m, REC_SPACING_M,
            jitter_ratio=JITTER_RATIO, jitter_amp_m=JITTER_AMP_M)
        n_pts = len(out_m)
        if n_pts < 2:
            print("错误: 采样点数不足")
            return False
        geo = ct.to_geo_points(capsule, out_m)
        # 轨迹整体往正东平移约 10m（在经纬度边界换算，仅经度变化，形状/长度/速度不变）
        if TRACK_SHIFT_EAST_M:
            cos_lat = math.cos(math.radians(capsule['center_lat']))
            dlon = TRACK_SHIFT_EAST_M / (111320.0 * cos_lat)
            geo = [(lat, lon + dlon) for (lat, lon) in geo]

        # ---- 开始时间（本地时间原样写入，App 按 FIT 纪元回显墙钟一致）----
        start_ms = int(datetime(year, month, day, hour, minute, 0).timestamp() * 1000)
        dur_ms = int(total_duration * 1000)

        # ---- 3) 速度曲线：时间-距离映射（总时长精确保持）----
        # 3km 档(<4km)：km1快~km2略快~km3大掉速(参考5'45/5'33/9'33)+偶发慢走，强离散；
        # 5km及以上线性平缓递减
        if distance_m < 4000.0:
            pace_dg, pace_qg = ct.build_pace_curve(
                distance_m, trend_mode='disp3', duration_s=total_duration)
            trend_decay_ref = 0.5               # 仅用于瞬时速度上限估算
        else:
            pace_dg, pace_qg = ct.build_pace_curve(
                distance_m, amp=PER_KM_AMP, smooth_m=PACE_SMOOTH_M,
                trend_decay=TREND_DECAY, duration_s=total_duration)
            trend_decay_ref = TREND_DECAY
        q_at = lambda d: ct.pace_time_fraction(pace_dg, pace_qg, d)

        # 逐点瞬时速度(m/s)：由距离-时间曲线中心差分（±3 点窗口）推得，带物理上限
        head_v = distance_m / total_duration / (1.0 - trend_decay_ref / 2.0)  # 首端理论速度
        v_cap = head_v * 1.15
        spd = []
        for i in range(n_pts):
            im = max(0, i - 3)
            ip = min(n_pts - 1, i + 3)
            dd = out_dist[ip] - out_dist[im]
            dq = max(q_at(out_dist[ip]) - q_at(out_dist[im]), 1e-9)
            spd.append(min(dd / dq / total_duration, v_cap))
        avg_v = sum(spd) / n_pts

        # ---- 4) 组装 FIT 消息 ----
        builder = FitFileBuilder()

        # FileId 必须是文件首条消息（Keep/Garmin SDK 解析必需）
        file_id = FileIdMessage()
        file_id.type = FileType.ACTIVITY
        file_id.manufacturer = Manufacturer.GARMIN
        file_id.time_created = start_ms
        builder.add(file_id)

        # 记录点：时间按速度曲线分配（慢处密、快处疏），时间严格单调
        for i in range(n_pts):
            record = RecordMessage()
            t_ms = start_ms + int(round(dur_ms * q_at(out_dist[i])))
            record.timestamp = t_ms
            record.position_lat = geo[i][0]
            record.position_long = geo[i][1]
            record.distance = out_dist[i]
            # 逐点运动学：速度快时步频/功率略升、触地略降
            v = spd[i]
            k = v / avg_v if avg_v > 0 else 1.0
            record.enhanced_speed = v
            cad_half = (AVG_SPM + 10.0 * (k - 1.0) + random.uniform(-2.0, 2.0)) / 2.0
            cad_half = _clamp(cad_half, 72.0, 86.0)
            record.cadence = int(round(cad_half))          # 半值存储（×2 显示）
            pwr = AVG_POWER_W * ((0.8 + 0.2 * _clamp(k, 0.6, 1.5)) ** 3) \
                * (1.0 + 0.08 * random.uniform(-1.0, 1.0))
            record.power = int(round(_clamp(pwr, 100.0, 300.0)))
            st = 39000.0 / (cad_half * 2.0) + random.uniform(-18.0, 18.0)
            record.stance_time = _clamp(st, 200.0, 290.0)
            builder.add(record)

        # 圈消息：每整圈距离 = L0*scale；零头圈为剩余距离
        L0s = capsule['L0'] * scale
        lap_dists = []
        n_full = int(distance_m // capsule['L0'])
        rem = distance_m - n_full * capsule['L0']
        for _ in range(n_full):
            lap_dists.append(L0s)
        if rem > 1.0:
            lap_dists.append(rem * scale)
        lap_edges = []
        cum = 0.0
        for ld in lap_dists:
            cum += ld
            lap_edges.append(cum)
        for k, ld in enumerate(lap_dists):
            lap_msg = LapMessage()
            prev_d = 0.0 if k == 0 else lap_edges[k - 1]
            t_start = start_ms + int(round(dur_ms * q_at(prev_d)))
            t_end = start_ms + int(round(dur_ms * q_at(lap_edges[k])))
            lap_msg.start_time = t_start
            lap_msg.timestamp = t_end
            lap_msg.total_elapsed_time = (t_end - t_start) / 1000.0
            lap_msg.total_distance = ld
            lap_msg.sport = Sport.RUNNING
            lap_msg.sub_sport = SubSport.GENERIC
            builder.add(lap_msg)

        # 会话消息
        session_msg = SessionMessage()
        session_msg.timestamp = start_ms + dur_ms
        session_msg.start_time = start_ms
        session_msg.total_elapsed_time = total_duration
        session_msg.total_distance = distance_m
        session_msg.sport = Sport.RUNNING
        session_msg.sub_sport = SubSport.GENERIC
        session_msg.num_laps = len(lap_dists)
        session_msg.total_calories = int(distance_m / 1000.0 * 70.0)
        session_msg.avg_running_cadence = CAD_HALF_AVG   # 半值（×2 = 158 步/分）
        session_msg.avg_power = int(round(AVG_POWER_W))
        session_msg.avg_stance_time = int(round(AVG_STANCE_MS))
        if hasattr(session_msg, 'avg_speed'):
            session_msg.avg_speed = avg_v
        if hasattr(session_msg, 'max_speed'):
            session_msg.max_speed = max(spd)
        builder.add(session_msg)

        # 活动消息
        activity_msg = ActivityMessage()
        activity_msg.timestamp = start_ms + dur_ms
        activity_msg.total_timer_time = total_duration
        activity_msg.num_sessions = 1
        activity_msg.type = 0  # Manual
        builder.add(activity_msg)

        # ---- 5) 构建并保存 ----
        if output_path is None:
            output_path = 'data/%s.fit' % user_id
        os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
        builder.build().to_file(output_path)

        # 最终折线实测总长（出口标定锚点）
        total_meas = sum(math.hypot(out_m[i][0] - out_m[i - 1][0],
                                    out_m[i][1] - out_m[i - 1][1]) for i in range(1, n_pts))
        print("成功生成: %s  目标=%.2fm 实测折线=%.2fm 误差=%.2f%% 点数=%d 圈数=%d" % (
            output_path, distance_m, total_meas,
            (total_meas - distance_m) / distance_m * 100.0, n_pts, len(lap_dists)))
        return True

    except Exception as e:
        print("生成失败: %s" % e)
        import traceback
        traceback.print_exc()
        return False


if __name__ == '__main__':
    if len(sys.argv) not in (5, 6):
        print("用法: python generate_fit.py <个人编号> <日期> <开始时间> <运动总时长> [距离公里]")
        print("示例: python generate_fit.py 021 20260414 1315 2103 3.0")
        sys.exit(1)
    uid = sys.argv[1]
    date = sys.argv[2]
    st = sys.argv[3]
    dur = sys.argv[4]
    dist = float(sys.argv[5]) if len(sys.argv) == 6 else None
    generate_fit(uid, date, st, dur, distance=dist)

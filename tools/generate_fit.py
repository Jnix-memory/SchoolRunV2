#!/usr/bin/env python3
"""
FIT 文件生成器
从 standard.fit 模板读取 GPS 轨迹，生成兼容 Keep 的运动数据

参考标准：《国民体质测定标准》（国家体育总局）
"""

import os
import random
from datetime import datetime

from fit_tool.fit_file import FitFile
from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.file_id_message import FileIdMessage, FileType, Manufacturer
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.messages.session_message import SessionMessage
from fit_tool.profile.messages.lap_message import LapMessage
from fit_tool.profile.messages.activity_message import ActivityMessage
from fit_tool.profile.messages.event_message import EventMessage, Event, EventType
from fit_tool.profile.messages.device_info_message import DeviceInfoMessage
from fit_tool.profile.profile_type import Sport, SubSport

TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'standard.fit')


def _load_template_points():
    fit = FitFile.from_file(TEMPLATE_PATH)
    points = []
    for r in fit.records:
        m = r.message
        if hasattr(m, 'position_lat') and m.position_lat is not None \
           and hasattr(m, 'position_long') and m.position_long is not None:
            points.append((m.position_lat, m.position_long))
    return points


TEMPLATE_POINTS = _load_template_points()


def _jitter(val, pct=0.03):
    return val * random.uniform(1 - pct, 1 + pct)


def generate_fit(user_id, date, start_time, duration, output_path=None):
    try:
        year = int(date[:4])
        month = int(date[4:6])
        day = int(date[6:8])
        hour = int(start_time[:2])
        minute = int(start_time[2:4])
        total_duration = int(duration[:2]) * 60 + int(duration[2:4])

        total_distance = random.uniform(2.95, 3.20) * 1000
        start_ts = int(datetime(year, month, day, hour, minute, 0).timestamp() * 1000)
        end_ts = start_ts + total_duration * 1000
        num_points = len(TEMPLATE_POINTS)

        base_cadence = random.randint(165, 175)
        base_step_len = random.uniform(0.95, 1.15)
        avg_speed = total_distance / total_duration

        builder = FitFileBuilder()

        # ==================== FileIdMessage ====================
        fid = FileIdMessage()
        fid.type = FileType.ACTIVITY
        fid.manufacturer = Manufacturer.GARMIN
        fid.product = 3843
        fid.serial_number = 1234567890
        fid.time_created = start_ts
        fid.number = 0
        fid.product_name = 'Forerunner 245'
        builder.add(fid)

        # ==================== DeviceInfoMessage ====================
        dev = DeviceInfoMessage()
        dev.timestamp = start_ts
        dev.device_index = 0
        dev.manufacturer = Manufacturer.GARMIN
        dev.product = 3843
        dev.product_name = 'Forerunner 245'
        dev.software_version = 7.2
        dev.hardware_version = 1.0
        dev.source_type = 1
        builder.add(dev)

        # ==================== EventMessage: START ====================
        evt_start = EventMessage()
        evt_start.timestamp = start_ts
        evt_start.event = Event.TIMER
        evt_start.event_type = EventType.START
        builder.add(evt_start)

        # ==================== RecordMessage ====================
        for i in range(num_points):
            frac = i / num_points
            elapsed_sec = frac * total_duration

            record = RecordMessage()
            record.timestamp = start_ts + int(frac * total_duration * 1000)
            record.position_lat = TEMPLATE_POINTS[i][0]
            record.position_long = TEMPLATE_POINTS[i][1]
            record.distance = total_distance * frac

            c = base_cadence + random.randint(-5, 5)
            record.cadence = c
            record.fractional_cadence = random.uniform(0, 0.99)

            speed_jitter = avg_speed * random.uniform(0.97, 1.03)
            record.speed = speed_jitter
            record.enhanced_speed = speed_jitter

            record.total_cycles = int(_jitter(c / 60 * elapsed_sec))
            record.step_length = speed_jitter / (c / 60) * 100 if c > 0 else 0

            builder.add(record)

        # ==================== LapMessage ====================
        LAP_DIST = 400.0
        total_laps = max(1, int(total_distance / LAP_DIST))
        lap_cumulative_cycles = 0

        for n in range(total_laps):
            lap_elapsed = total_duration / total_laps

            lap = LapMessage()
            lap.start_time = start_ts + int(n * lap_elapsed * 1000)
            lap.timestamp = start_ts + int((n + 1) * lap_elapsed * 1000)
            lap.total_elapsed_time = lap_elapsed
            lap.total_timer_time = lap_elapsed
            lap.total_distance = LAP_DIST
            lap.sport = Sport.RUNNING
            lap.sub_sport = SubSport.GENERIC

            lap_cad = base_cadence + random.randint(-3, 3)
            lap.avg_cadence = lap_cad
            lap.max_cadence = lap_cad + random.randint(5, 12)
            lap.avg_fractional_cadence = random.uniform(0, 0.99)

            lap_steps = int(_jitter(lap_cad / 60 * lap_elapsed))
            lap.total_cycles = lap_steps
            lap_cumulative_cycles += lap_steps

            lap.avg_step_length = avg_speed / (lap_cad / 60) * 100 if lap_cad > 0 else 0

            builder.add(lap)

        # ==================== SessionMessage ====================
        session = SessionMessage()
        session.timestamp = end_ts
        session.start_time = start_ts
        session.total_elapsed_time = total_duration
        session.total_timer_time = total_duration
        session.total_distance = total_distance
        session.sport = Sport.RUNNING
        session.sub_sport = SubSport.GENERIC
        session.num_laps = total_laps
        session.total_calories = int(total_distance / 1000 * 70)

        session.avg_speed = avg_speed
        session.enhanced_avg_speed = avg_speed

        session.avg_cadence = base_cadence
        session.max_cadence = base_cadence + random.randint(8, 15)
        session.avg_fractional_cadence = random.uniform(0, 0.99)
        session.max_fractional_cadence = random.uniform(0, 0.99)
        session.avg_running_cadence = base_cadence

        session.total_cycles = lap_cumulative_cycles

        session.avg_step_length = base_step_len * 100
        session.avg_power = random.randint(220, 280)
        session.avg_stance_time = random.randint(210, 250)

        builder.add(session)

        # ==================== EventMessage: STOP ====================
        evt_stop = EventMessage()
        evt_stop.timestamp = end_ts
        evt_stop.event = Event.TIMER
        evt_stop.event_type = EventType.STOP_ALL
        builder.add(evt_stop)

        # ==================== ActivityMessage ====================
        activity = ActivityMessage()
        activity.timestamp = end_ts
        activity.total_timer_time = total_duration
        activity.num_sessions = 1
        activity.type = 0
        builder.add(activity)

        # 保存
        if output_path is None:
            output_path = f'data/{user_id}.fit'
        os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
        builder.build().to_file(output_path)
        print(f"生成成功: {output_path}")
        return True

    except Exception as e:
        print(f"生成失败: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == '__main__':
    import sys
    if len(sys.argv) != 5:
        print("用法: python generate_fit.py <编号> <日期> <时间> <时长>")
        sys.exit(1)
    generate_fit(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4])

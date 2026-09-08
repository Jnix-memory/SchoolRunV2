#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
capsule_track.py
基于母版 GPS（data/standard.fit，与 SchoolRunV1 的 1.fit 同源）自动拟合"胶囊形"跑道
（两条直线段 + 两端两个半圆），并按目标距离绕圈采样、在切线垂直方向加入低频横向抖动，
用于生成拟真的跑步 FIT 轨迹。

胶囊参数定义（以米制局部坐标为准）：
    a  : 半直道长（直线段总长 = 2a）
    r  : 半圆半径
    周长 L0 = 2*(2a) + 2*pi*r = 4a + 2*pi*r

本模块移植自 SchoolRunV1 改进版（对应 FIT_GENERATOR_TECH_REPORT 中的胶囊跑道方案）：
    PCA 拟合朝向/外包络、等弧长绕圈采样、低频平滑横向抖动（峰值归一）、
    起点锁定、终点出跑道（外侧法向 10~30m 外移）、出口双重长度标定、
    速度曲线（整体递减趋势 + 每公里随机、总时长保持）。

坐标约定：模块内部一律使用以跑道中心为原点的米制偏移坐标，仅在 to_geo_points 边界
换算回经纬度（单位混用是本类项目最隐蔽的 bug 源，见技术报告 INV-01/FIX-05）。
"""

import math
import random

# ---- 经纬度 -> 米制局部坐标（以中心为原点）----

EARTH_M_PER_DEG_LAT = 110574.0  # 米/纬度（约）


def geo_to_meters(center_lat, center_lon, lats, lons):
    """把经纬度列表转为以 (center_lat, center_lon) 为原点的米制坐标。
    返回 [(x_east, y_north), ...]。x 向东、y 向北。"""
    cos_lat = math.cos(math.radians(center_lat))
    mx = 111320.0 * cos_lat  # 米/经度
    pts = []
    for lat, lon in zip(lats, lons):
        x = (lon - center_lon) * mx
        y = (lat - center_lat) * EARTH_M_PER_DEG_LAT
        pts.append((x, y))
    return pts


def meters_to_geo(center_lat, center_lon, pts):
    """米制坐标 -> 经纬度。"""
    cos_lat = math.cos(math.radians(center_lat))
    mx = 111320.0 * cos_lat
    out = []
    for x, y in pts:
        lon = center_lon + x / mx
        lat = center_lat + y / EARTH_M_PER_DEG_LAT
        out.append((lat, lon))
    return out


def polyline_len(pts):
    """米制折线总长。"""
    return sum(math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1])
               for i in range(1, len(pts)))


def _pct(sorted_vals, q):
    """线性百分位（sorted_vals 已升序）。"""
    if not sorted_vals:
        return 0.0
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_vals[lo]
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


# ---- PCA（2x2 解析特征分解）----

def pca_2d(pts):
    """pts: [(x,y),...] 米制。返回 (e1, e2, lam1, lam2)：
    e1 为最大方差方向（单位向量），e2 为其垂直方向。"""
    n = len(pts)
    cx = sum(p[0] for p in pts) / n
    cy = sum(p[1] for p in pts) / n
    sxx = sum((p[0] - cx) ** 2 for p in pts) / n
    syy = sum((p[1] - cy) ** 2 for p in pts) / n
    sxy = sum((p[0] - cx) * (p[1] - cy) for p in pts) / n
    tr = sxx + syy
    det = sxx * syy - sxy * sxy
    disc = math.sqrt(max(0.0, tr * tr - 4 * det))
    lam1 = (tr + disc) / 2.0
    lam2 = (tr - disc) / 2.0
    if abs(sxy) > 1e-12:
        v = (sxy, lam1 - sxx)
    elif sxx >= syy:
        v = (1.0, 0.0)
    else:
        v = (0.0, 1.0)
    nv = math.hypot(v[0], v[1]) or 1.0
    e1 = (v[0] / nv, v[1] / nv)
    e2 = (-e1[1], e1[0])
    return e1, e2, lam1, lam2


# ---- 胶囊几何 ----

def capsule_lap_len(a, r):
    return 4.0 * a + 2.0 * math.pi * r


def capsule_point(a, r, s, L0):
    """按弧长 s in [0, L0) 取胶囊上一点（米制局部坐标，u 沿直道方向，v 垂直）。
    行走方向约定：顶直道从左到右 -> 右半圆(顺时针转下) -> 底直道从右到左
    -> 左半圆(逆时针转上) -> 回到起点。周期为 L0。"""
    s = s % L0
    half_pi_r = math.pi * r
    if s < 2.0 * a:                 # 顶直道
        return (-a + s, r)
    s -= 2.0 * a
    if s < half_pi_r:               # 右半圆：p=(a + r cos fi, r sin fi), fi 从 pi/2 递减到 -pi/2
        fi = math.pi / 2.0 - s / r
        return (a + r * math.cos(fi), r * math.sin(fi))
    s -= half_pi_r
    if s < 2.0 * a:                 # 底直道
        return (a - s, -r)
    s -= 2.0 * a
    fi2 = -math.pi / 2.0 + s / r    # 左半圆：p=(-a - r cos fi2, r sin fi2), fi2 从 -pi/2 到 +pi/2
    return (-a - r * math.cos(fi2), r * math.sin(fi2))


def fit_capsule_from_points(pts_m):
    """对一圈的米制点拟合胶囊外包络。
    返回 dict: a, r, e1, e2, U, W, L0, ok, reason
    a 为半直道长；r 为半圆半径；U/W 为沿主轴的实测外包络长/宽。"""
    e1, e2, _, _ = pca_2d(pts_m)
    us = [p[0] * e1[0] + p[1] * e1[1] for p in pts_m]
    vs = [p[0] * e2[0] + p[1] * e2[1] for p in pts_m]
    us_s, vs_s = sorted(us), sorted(vs)
    # 抗抖动的外包络：2%~98% 分位
    U = _pct(us_s, 0.98) - _pct(us_s, 0.02)
    W = _pct(vs_s, 0.98) - _pct(vs_s, 0.02)
    r_est = W / 2.0
    a_est = max(5.0, (U - W) / 2.0)  # 半直道长：外长 - 两个半径
    return {'e1': e1, 'e2': e2, 'U': U, 'W': W, 'r': r_est, 'a': a_est,
            'L0': capsule_lap_len(a_est, r_est)}


# ---- 从母版 FIT 提取一圈 ----

def load_master(path):
    """读取母版 FIT，返回结构化数据：
    dict(laps=[{start_ms,end_ms,distance}], pts=[(ms, lat, lon)], ...)
    只依赖 fit_tool。"""
    from fit_tool.fit_file import FitFile
    f = FitFile.from_file(path)
    laps = []
    pts = []
    for rec in f.records:
        m = getattr(rec, 'message', None)
        if m is None:
            continue
        cls = m.__class__.__name__
        if cls == 'RecordMessage':
            lat = getattr(m, 'position_lat', None)
            lon = getattr(m, 'position_long', None)
            ts = getattr(m, 'timestamp', None)
            if lat is not None and lon is not None:
                pts.append((float(ts), float(lat), float(lon)))
        elif cls == 'LapMessage':
            st = getattr(m, 'start_time', None)
            en = getattr(m, 'timestamp', None)
            d = getattr(m, 'total_distance', None)
            laps.append({'start_ms': float(st), 'end_ms': float(en), 'distance': float(d) if d else None})
    pts.sort(key=lambda p: p[0])
    return {'laps': laps, 'pts': pts}


def pick_one_lap(data):
    """挑选母版中点数最多的一圈，返回其 (lats, lons) 及 lap distance（若有）。"""
    pts = data['pts']
    laps = data['laps']
    if len(pts) < 4:
        return None
    if not laps:
        return [p[1:] for p in pts], None
    # 依据各 lap 时间窗切分记录
    bounds = [lp['start_ms'] for lp in laps]
    if len(bounds) >= 2:
        bounds.append(max(lp['end_ms'] for lp in laps))
    chunks = {}
    for p in pts:
        idx = 0
        for i, b in enumerate(bounds):
            if p[0] < b:
                idx = i
                break
        else:
            idx = len(bounds) - 1
        chunks.setdefault(idx, []).append((p[1], p[2]))
    best = None
    for idx, chunk in chunks.items():
        if len(chunk) >= 10 and (best is None or len(chunk) > len(best)):
            best = chunk
    if best is None:
        return None
    dist = None
    for i, lp in enumerate(laps):
        if lp.get('distance'):
            dist = lp['distance']
            break
    return best, dist


# ---- 主入口：拟合胶囊 ----

def fit_capsule_from_master(path):
    """从母版 FIT 拟合胶囊跑道。返回 Capsule 对象或 None。
    当拟合参数异常时降级为标准 400m 跑道参数（保留中心与朝向），并在 reason 中说明。"""
    data = load_master(path)
    lap = pick_one_lap(data)
    if not lap:
        return None
    coords, lap_dist = lap
    lats = [c[0] for c in coords]
    lons = [c[1] for c in coords]
    center_lat = sum(lats) / len(lats)
    center_lon = sum(lons) / len(lons)
    pts_m = geo_to_meters(center_lat, center_lon, lats, lons)
    fit = fit_capsule_from_points(pts_m)
    a_est, r_est = fit['a'], fit['r']
    e1, e2 = fit['e1'], fit['e2']
    U, W = fit['U'], fit['W']
    L0 = fit['L0']
    ok = True
    reason = ''
    if not (8.0 <= r_est <= 120.0 and 5.0 <= a_est <= 600.0):
        ok = False
        reason = 'r/a 超合理范围 (r=%.1f, a=%.1f)' % (r_est, a_est)
    if ok and not (200.0 <= L0 <= 1500.0):
        ok = False
        reason = '单圈周长超范围 (%.0f m)' % L0
    if not ok:
        # 降级：标准 400m 内道参数，保留中心与朝向
        r_est = 36.5
        a_est = 84.39 / 2.0
        L0 = capsule_lap_len(a_est, r_est)
        reason += '; 使用标准 400m 跑道参数'
        U, W = 2 * (a_est + r_est), 2 * r_est
    # 拟合残差：母版一圈各点到胶囊的平均距离（信息性指标）
    residual = capsule_residual(pts_m, e1, e2, a_est, r_est)
    return {
        'center_lat': center_lat, 'center_lon': center_lon,
        'e1': e1, 'e2': e2, 'a': a_est, 'r': r_est, 'L0': L0,
        'U': U, 'W': W, 'residual': residual,
        'ok': ok, 'reason': reason, 'master_points': len(lats),
    }


def capsule_residual(pts_m, e1, e2, a, r):
    """一圈母版点到拟合胶囊的平均垂直距离（米）。"""
    if not pts_m:
        return 0.0
    tot = 0.0
    for p in pts_m:
        u = p[0] * e1[0] + p[1] * e1[1]
        v = p[0] * e2[0] + p[1] * e2[1]
        if abs(u) <= a:
            d = abs(abs(v) - r)
        else:
            c = (a if u > 0 else -a, 0.0)  # 端点半圆中心
            d = abs(math.hypot(p[0] - c[0], p[1] - c[1]) - r)
        tot += d
    return tot / len(pts_m)


# ---- 轨迹生成 ----

def sample_loop(capsule, spacing):
    """沿胶囊一圈按 spacing(米) 取点，返回 (coords[(x,y)...], N) 等弧长采样点。"""
    L0 = capsule['L0']
    n = max(2, int(round(L0 / spacing)))
    ds = L0 / (n - 1) if n > 1 else 0.0
    coords = []
    for i in range(n):
        s = i * ds
        u, v = capsule_point(capsule['a'], capsule['r'], s, L0)
        coords.append((u, v))
    return coords


def _smooth(vals, radius=2):
    """moving average（窗口半径 radius）。"""
    n = len(vals)
    out = [0.0] * n
    for i in range(n):
        lo = max(0, i - radius)
        hi = min(n, i + radius + 1)
        out[i] = sum(vals[lo:hi]) / (hi - lo)
    return out


def generate_track_loop(capsule, distance_m, spacing_m, jitter_ratio=0.25, jitter_amp_m=10.0,
                        lateral_smooth_m=250.0, end_off_min=10.0, end_off_max=30.0, tail_m=45.0):
    """沿胶囊弧长均匀取点并加横向随机波动。

    规则：
      * 横向波动：随机采样触发 ±amp（默认 ≤10m），长窗平滑（相关长度
        lateral_smooth_m）避免拉长路径，峰值归一至 amp；
      * 起点固定：第 1 个点的横向偏移强制为 0（多次生成起点一致）；
      * 终点出跑道：最后 tail_m 米横向随机逐渐淡出，同时沿跑道外侧法向
        平滑外移，使终点相对跑道的距离落在 (end_off_min, end_off_max)。
    最后按实测折线长度精确标定使总长 == distance_m。
    返回 (经纬度米制点列表[(x,y)...], 每点累计里程列表, 每点所属圈, 缩放因子)。
    """
    L0 = capsule['L0']
    a = capsule['a']
    r = capsule['r']
    n_full = int(distance_m // L0)
    rem = distance_m - n_full * L0
    laps_needed = n_full + (1 if rem > 1.0 else 0)
    # 输出记录间距：目标 3m，点数上限 4000 保护
    rec_spacing = max(3.0, distance_m / 4000.0)
    n_rec = max(2, int(distance_m // rec_spacing) + 1)
    ds = distance_m / (n_rec - 1)

    # 胶囊上等弧长参考点及切线方向（s±0.5m 差分）
    base = []
    tang = []
    for i in range(n_rec):
        s = i * ds
        u0, v0 = capsule_point(a, r, s, L0)
        ua, va = capsule_point(a, r, s + 0.5, L0)
        ub, vb = capsule_point(a, r, s - 0.5, L0)
        base.append((u0, v0))
        tx = ua - ub
        ty = va - vb
        tl = math.hypot(tx, ty) or 1e-9
        tang.append((tx / tl, ty / tl))

    # 横向随机偏移：触发率 jitter_ratio、幅度 ±amp；
    # 长窗平滑（相关长度 lateral_smooth_m）让偏移低频化（避免把轨迹拉长），
    # 再整体归一使峰值恰为 amp。
    raw = []
    for i in range(n_rec):
        if random.random() < jitter_ratio:
            raw.append(random.uniform(-jitter_amp_m, jitter_amp_m))
        else:
            raw.append(0.0)
    rad = max(1, int(round(lateral_smooth_m / ds / 2.0)))
    sm = []
    for i in range(n_rec):
        lo = max(0, i - rad)
        hi = min(n_rec, i + rad + 1)
        sm.append(sum(raw[lo:hi]) / (hi - lo))
    mx = max((abs(x) for x in sm), default=0.0)
    if mx > 1e-9:
        k = jitter_amp_m / mx
        sm = [x * k for x in sm]

    # 终点段：横向随机淡出 + 跑道外侧法向外移（终点出跑道 10~30m）
    tail_len_m = min(tail_m, distance_m * 0.5)
    d_end = random.uniform(end_off_min, end_off_max)
    tail_s0 = distance_m - tail_len_m

    # 起点固定：前 4 个点横向偏移从 0 渐变（首点恒为 0）
    offs = []
    for i in range(n_rec):
        s = i * ds
        z = 0.0
        if tail_len_m > 1.0 and s > tail_s0:
            z = min(1.0, (s - tail_s0) / tail_len_m)
        lateral = sm[i] * (1.0 - z)   # 终点处横向随机淡出为 0
        tail_out = d_end * z * z      # 向外平滑外移，终点恰为 d_end
        if i < 4:
            lateral *= i / 4.0
        offs.append(lateral + tail_out)

    pts_local = []
    for i in range(n_rec):
        nx, ny = -tang[i][1], tang[i][0]  # 法向 = 切线逆时针旋转 90 度（跑道外侧）
        pts_local.append((base[i][0] + nx * offs[i],
                          base[i][1] + ny * offs[i]))

    # 实测折线长度 -> 精确标定（横向/终点偏移对总长影响极小，缩放≈1）
    R = polyline_len(pts_local)
    scale = distance_m / R if R > 0 else 1.0
    pts_local = [(p[0] * scale, p[1] * scale) for p in pts_local]
    # 硬上限：主体段（不含终点外移段）相对"缩放后跑道中线"的横向偏离 ≤9.95m
    # （终点外移段允许 10~30m，不参与阻尼）
    mxdev = 0.0
    for i in range(n_rec):
        if i * ds > tail_s0 + 1e-9:
            continue  # 终点外移段
        d = math.hypot(pts_local[i][0] - base[i][0] * scale,
                       pts_local[i][1] - base[i][1] * scale)
        if d > mxdev:
            mxdev = d
    if mxdev > 9.95:
        damp = 9.95 / mxdev
        pts_local = [(base[i][0] * scale + (pts_local[i][0] - base[i][0] * scale) * damp,
                      base[i][1] * scale + (pts_local[i][1] - base[i][1] * scale) * damp)
                     for i in range(n_rec)]

    # 旋转：胶囊局部坐标(u,v) -> 经纬度米制坐标（沿母版 PCA 朝向 e1/e2）
    e1x, e1y = capsule['e1']
    e2x, e2y = capsule['e2']
    out_pts = [(p[0] * e1x + p[1] * e2x, p[0] * e1y + p[1] * e2y) for p in pts_local]
    # 起点固定：首点锁定为规范起点（胶囊弧长 0 处），跨生成完全一致（米制坐标）
    u0, v0 = capsule_point(a, r, 0.0, L0)
    g0 = (u0 * e1x + v0 * e2x, u0 * e1y + v0 * e2y)
    out_pts[0] = g0
    # 出口双重标定：以返回折线的实测长度再次归一（首点保持规范起点）
    for _ in range(2):
        L2 = polyline_len(out_pts)
        if L2 > 0 and abs(L2 - distance_m) > 0.05:
            f = distance_m / L2
            out_pts = [(p[0] * f, p[1] * f) for p in out_pts]
            out_pts[0] = g0
        else:
            break
    scale = scale * (distance_m / (polyline_len(out_pts) or 1.0))
    out_dist = [i * ds for i in range(n_rec)]
    out_dist[-1] = distance_m
    # 每点所属圈（供参考）
    out_lap = []
    for i in range(n_rec):
        s = i * ds
        k = int(s // L0)
        out_lap.append(min(k, laps_needed - 1))
    return out_pts, out_dist, out_lap, scale


# ---- 便捷换算：得到经纬度 ----

def to_geo_points(capsule, out_pts):
    return meters_to_geo(capsule['center_lat'], capsule['center_lon'], out_pts)


# ---- 速度波动：整体递减趋势 + 每公里随机，总时长保持 ----

def build_pace_curve(distance_m, seg_m=1000.0, amp=0.05, smooth_m=200.0,
                     step_m=5.0, trend_decay=0.2, duration_s=None):
    """生成"时间-距离"累积曲线。
    速度模型：整体随时间线性递减，末端速度 = 首端 × (1-trend_decay)（默认 80%）；
    每 seg_m 段再叠加 ±amp 的独立随机波动；段间按 smooth_m 平滑。
    最后归一化使全程耗时比例恰为 1（总时长恒等于输入时长，与绝对速度无关）。
    返回 (d_grid, q_grid)：q 为距离 d 处已消耗的总时间比例(0..1)。
    注：trend 的绝对速度由 距离/时长 反推；平均速度>9km/h 时首端将超过 10km/h
    （"总时长优先"选项下允许突破 10km/h 上限）。"""
    if distance_m <= 0:
        return [0.0], [0.0]
    # 趋势解析：s(t)=S*(1-trend_decay*t/T)，平均=S*(1-trend_decay/2)=D/T
    T = float(duration_s) if duration_s else None
    S = 0.0
    if T and T > 0:
        S = distance_m / T / (1.0 - trend_decay / 2.0)  # m/s（首端速度）
    # 每段独立随机速度系数 f ∈ [1-amp, 1+amp]
    factors = []
    pos = 0.0
    while pos < distance_m - 1e-9:
        factors.append(random.uniform(1.0 - amp, 1.0 + amp))
        pos += seg_m
    # 细网格上：pace(单位距离耗时比例) = 随机系数 * 趋势项(1/v_rel)
    n = max(2, int(math.ceil(distance_m / step_m)))
    pace = []
    for i in range(n):
        d = i * step_m
        k = min(int(d // seg_m), len(factors) - 1)
        mt = 1.0
        if S > 0 and trend_decay > 0 and T and T > 0:
            # 反解 t0(d)：d = S*t - S*trend_decay*t^2/(2T)
            A = trend_decay / (2.0 * T)
            disc = S * S - 4.0 * S * A * d
            if disc > 0:
                t0 = (S - math.sqrt(disc)) / (2.0 * S * A)
            else:
                t0 = T
            v_rel = 1.0 - trend_decay * t0 / T  # 相对首端速度(1 -> 0.8)
            mt = 1.0 / v_rel if v_rel > 1e-9 else 1.25
        pace.append(factors[k] * mt)
    # 滑动平均平滑（对应 smooth_m 距离）
    if smooth_m > step_m and len(pace) > 3:
        rad = max(1, int(round(smooth_m / step_m / 2.0)))
        sm = []
        for i in range(n):
            lo = max(0, i - rad)
            hi = min(n, i + rad + 1)
            sm.append(sum(pace[lo:hi]) / (hi - lo))
        pace = sm
    # 累积耗时并归一化
    cum = [0.0]
    for i in range(1, n):
        cum.append(cum[-1] + (pace[i - 1] + pace[i]) / 2.0 * step_m)
    total = cum[-1]
    if total <= 0:
        total = 1.0
    d_grid = [i * step_m for i in range(n)]
    d_grid[-1] = distance_m
    q_grid = [c / total for c in cum]
    q_grid[-1] = 1.0
    return d_grid, q_grid


def pace_time_fraction(d_grid, q_grid, d):
    """在线性插值曲线上取距离 d 处的累计时间比例。"""
    if d <= d_grid[0]:
        return q_grid[0]
    if d >= d_grid[-1]:
        return q_grid[-1]
    lo, hi = 0, len(d_grid) - 1
    while lo < hi - 1:
        mid = (lo + hi) // 2
        if d_grid[mid] <= d:
            lo = mid
        else:
            hi = mid
    f = (d - d_grid[lo]) / (d_grid[hi] - d_grid[lo] + 1e-12)
    return q_grid[lo] + (q_grid[hi] - q_grid[lo]) * f


if __name__ == '__main__':
    import sys
    import os
    _def = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'data', 'standard.fit')
    master = sys.argv[1] if len(sys.argv) > 1 else _def
    cap = fit_capsule_from_master(master)
    if cap is None:
        print('FIT-CAPSULE: fit failed')
        sys.exit(1)
    print('FIT-CAPSULE ok=%s reason=%s' % (cap['ok'], cap['reason']))
    print('center=%.6f,%.6f a(half straight)=%.2fm r=%.2fm L0=%.2fm extents_U=%.1fm W=%.1fm residual=%.2fm pts=%d' % (
        cap['center_lat'], cap['center_lon'], cap['a'], cap['r'], cap['L0'],
        cap['U'], cap['W'], cap['residual'], cap['master_points']))
    # 几何连续性自检
    for s in (0.0, 2 * cap['a'] - 1e-6, 2 * cap['a'], 2 * cap['a'] + math.pi * cap['r'] - 1e-6,
              2 * cap['a'] + math.pi * cap['r'], cap['L0'] - 1e-6, cap['L0']):
        capsule_point(cap['a'], cap['r'], s, cap['L0'])
    print('capsule continuity check done')

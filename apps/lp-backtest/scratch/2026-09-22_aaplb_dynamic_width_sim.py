import json, math, datetime, zoneinfo

STRATEGY_START = 1789723904  # 2026-09-18T09:31:44Z
TICK_SPACING = 50
DECIMALS0 = 18
DECIMALS1 = 18
CAPITAL_USD = 768.56
FEE_PIPS = 2500  # 0.25%
VOLUME_DAILY_USD = 974_922.0  # current 24h volume, static-extrapolated (caveat)

EVAL_INTERVAL = 300
COOLDOWN = 300
MIN_HOLD = 600
MIN_OOR = 360

# Empirical per-rebalance costs, derived from live results (gas ~$0.75 / 8 rebal, slippage ~$0.93 / 8 rebal)
GAS_PER_REBALANCE = 0.75 / 8
SLIPPAGE_PER_REBALANCE = 0.93 / 8

LN_1_0001 = math.log(1.0001)

def price_to_tick(p):
    return math.log(p) / LN_1_0001

def ticks_for_pct(pct):
    return math.log(1 + pct) / LN_1_0001

# tick math for the proposed widths
UP_2PCT = ticks_for_pct(0.02)
DOWN_2PCT = ticks_for_pct(-0.02)
UP_02PCT = ticks_for_pct(0.002)
DOWN_02PCT = ticks_for_pct(-0.002)

print(f"+2% = {UP_2PCT:.2f} ticks, -2% = {DOWN_2PCT:.2f} ticks")
print(f"+0.2% = {UP_02PCT:.2f} ticks, -0.2% = {DOWN_02PCT:.2f} ticks (tickSpacing={TICK_SPACING})")

import os
_HERE = os.path.dirname(os.path.abspath(__file__))
candles = json.load(open(os.path.join(_HERE, "2026-09-22_aaplb_5m_ohlcv.json")))
candles = [c for c in candles if c[0] >= STRATEGY_START]
bars = [(c[0], price_to_tick(c[4])) for c in candles]  # (time, tick) using close price
print(f"bars: {len(bars)}, span {datetime.datetime.utcfromtimestamp(bars[0][0])} .. {datetime.datetime.utcfromtimestamp(bars[-1][0])}")

NY = zoneinfo.ZoneInfo("America/New_York")

# 2026 US market holidays (NYSE) - approx standard set
HOLIDAYS_2026 = {
    datetime.date(2026,1,1), datetime.date(2026,1,19), datetime.date(2026,2,16),
    datetime.date(2026,4,3), datetime.date(2026,5,25), datetime.date(2026,6,19),
    datetime.date(2026,7,3), datetime.date(2026,9,7), datetime.date(2026,11,26),
    datetime.date(2026,12,25),
}

def is_market_hours(ts):
    dt = datetime.datetime.fromtimestamp(ts, tz=NY)
    if dt.weekday() >= 5:
        return False
    if dt.date() in HOLIDAYS_2026:
        return False
    minutes = dt.hour * 60 + dt.minute
    return 9*60+30 <= minutes < 16*60

def align_up_exclusive(tick, spacing):
    aligned = math.ceil(tick / spacing) * spacing
    return aligned + spacing if aligned <= tick else aligned

def align_down_inclusive(tick, spacing):
    return math.floor(tick / spacing) * spacing

def align_width(raw, spacing):
    return max(spacing, round(raw / spacing) * spacing)

def limit_width_ticks(lower_off, upper_off, mult, spacing):
    half = (abs(lower_off) + abs(upper_off)) / 2
    raw = max(spacing, half * mult)
    return align_width(raw, spacing)

def replace_gap_ticks(width, spacing, mult):
    return max(spacing, round(max(0, width) * max(0, mult)))

class Position:
    __slots__ = ("lower","upper","opened_at","side")
    def __init__(self, lower, upper, opened_at, side="centered"):
        self.lower = lower; self.upper = upper; self.opened_at = opened_at; self.side = side

def in_range(tick, pos):
    return pos.lower <= tick < pos.upper

def run_sim(bars, width_fn, limit_mult=1.0, replace_mult=0.5, label=""):
    """width_fn(ts) -> (lower_off, upper_off) offsets used both for first centered mint and for
    computing the limit-order width (avg half-offset * limit_mult), mirroring single-sided-limit.ts."""
    t0, tick0 = bars[0]
    lo0, up0 = width_fn(t0)
    spacing = TICK_SPACING
    mid = round(tick0 / spacing) * spacing
    pos = Position(mid + align_width(lo0, spacing) * -1 if False else mid - align_width(abs(lo0), spacing), None, t0)
    # simpler: first centered position uses raw offsets directly (not forced to spacing), matching decideFixedRange semantics loosely
    pos = Position(mid + round(lo0/spacing)*spacing, mid + round(up0/spacing)*spacing, t0)

    fee_flow = VOLUME_DAILY_USD * FEE_PIPS / 1_000_000 / 86400  # USD/sec, full-range-equivalent pool fee flow
    # Base share (at the baseline +-50/+-50, 100-tick-wide position) calibrated in __main__ against
    # the real cumulative fee result. Width-dependent capital-efficiency scaling (Uniswap V3 whitepaper
    # approximation reused from research/apps/lp-backtest's capital_efficiency(): eff(w) = 1/(1-e^-w),
    # w = half-width in log-price units) makes a narrower position earn a proportionally higher fee
    # share per dollar of capital, and a wider one earn less -- this is the mechanism the naive
    # flat-rate model was missing.
    BASE_SHARE = 0.0170
    BASELINE_WIDTH_TICKS = 100  # -50/+50

    def eff(width_ticks):
        half_w_log = max(1e-9, width_ticks / 2) * LN_1_0001
        return 1 / (1 - math.exp(-half_w_log))

    baseline_eff = eff(BASELINE_WIDTH_TICKS)

    def share_for_width(width_ticks):
        return BASE_SHARE * eff(width_ticks) / baseline_eff

    fees_usd = 0.0
    ops = 0
    gas_usd = 0.0
    slip_usd = 0.0
    in_range_seconds = 0.0
    total_seconds = 0.0
    oor_since = None if in_range(tick0, pos) else t0
    last_action_at = t0
    opened_at = t0
    rebalance_log = []

    for i in range(1, len(bars)):
        pt, ptick = bars[i-1]
        t, tick = bars[i]
        dt = max(1, t - pt)
        total_seconds += dt
        frac = 1.0 if (min(ptick,tick) >= pos.lower and max(ptick,tick) < pos.upper) else None
        # occupancy of straight-line segment in [lower, upper)
        lo_seg, hi_seg = min(ptick, tick), max(ptick, tick)
        if lo_seg == hi_seg:
            occ = 1.0 if pos.lower <= lo_seg < pos.upper else 0.0
        else:
            left = max(lo_seg, pos.lower); right = min(hi_seg, pos.upper)
            occ = max(0.0, right - left) / (hi_seg - lo_seg) if right > left else 0.0
        if occ > 0:
            in_range_seconds += dt * occ
            fees_usd += fee_flow * share_for_width(pos.upper - pos.lower) * dt * occ

        currently_in = in_range(tick, pos)
        if currently_in:
            oor_since = None
        elif oor_since is None:
            oor_since = t

        if oor_since is not None and (t - oor_since) < MIN_OOR:
            continue
        if (t - opened_at) < MIN_HOLD:
            continue
        if (t - last_action_at) < COOLDOWN:
            continue
        if currently_in:
            continue

        lo_off, up_off = width_fn(t)
        width = limit_width_ticks(lo_off, up_off, limit_mult, spacing)
        gap = replace_gap_ticks(width, spacing, replace_mult)

        if tick < pos.lower:
            new_lower = align_up_exclusive(tick, spacing)
            if new_lower == pos.lower:
                continue
            new_upper = new_lower + width
        else:
            new_upper = align_down_inclusive(tick, spacing)
            if new_upper == pos.upper:
                continue
            new_lower = new_upper - width

        # dead-zone check only applies once already hanging at target width (mirrors TS logic
        # closely enough for this scope: skip if move is within gap of current edge)
        near_edge = pos.lower - tick if tick < pos.lower else tick - pos.upper
        if (pos.upper - pos.lower) == width and near_edge < gap:
            continue

        pos = Position(new_lower, new_upper, t)
        ops += 1
        gas_usd += GAS_PER_REBALANCE
        slip_usd += SLIPPAGE_PER_REBALANCE
        opened_at = t
        last_action_at = t
        oor_since = None if in_range(tick, pos) else t
        rebalance_log.append((t, new_lower, new_upper))

    net = fees_usd - gas_usd - slip_usd
    roi = net / CAPITAL_USD * 100
    print(f"\n=== {label} ===")
    print(f"rebalances/flips: {ops}")
    print(f"fees_usd: {fees_usd:.4f}")
    print(f"gas_usd: {gas_usd:.4f}  slip_usd: {slip_usd:.4f}")
    print(f"net_pnl_usd: {net:.4f}  roi_pct: {roi:.4f}")
    print(f"in_range_ratio: {in_range_seconds/total_seconds*100:.1f}%")
    return dict(ops=ops, fees=fees_usd, gas=gas_usd, slip=slip_usd, net=net, roi=roi,
                in_range_ratio=in_range_seconds/total_seconds, rebalance_log=rebalance_log)

def baseline_width_fn(ts):
    return (-50, 50)

def dynamic_width_fn(ts):
    if is_market_hours(ts):
        return (DOWN_2PCT, UP_2PCT)
    else:
        return (DOWN_02PCT, UP_02PCT)  # will be clamped by align_width to >=1 spacing anyway

if __name__ == "__main__":
    market_bars = [b for b in bars if is_market_hours(b[0])]
    print(f"\nmarket-hours bars: {len(market_bars)} / {len(bars)} ({len(market_bars)/len(bars)*100:.1f}%)")

    baseline = run_sim(bars, baseline_width_fn, label="BASELINE fixed +-50 ticks (~+-0.5%)")
    # calibrate SHARE so baseline fees ~= real $16.81 over same window, then reuse identical SHARE for proposed
    real_fees = 16.81
    if baseline["fees"] > 0:
        scale = real_fees / baseline["fees"]
        print(f"\n[calibration] baseline sim fees={baseline['fees']:.3f} vs real={real_fees} -> scale={scale:.4f}")

    proposed = run_sim(bars, dynamic_width_fn, label="PROPOSED dynamic +-2% (market) / +-0.2%->clamped (off) ")

    print("\n--- calibrated (scaled to real fee level) ---")
    for name, r in [("baseline", baseline), ("proposed", proposed)]:
        print(f"{name}: fees={r['fees']*scale:.2f} net={( r['fees']*scale - r['gas'] - r['slip']):.2f} "
              f"roi={(r['fees']*scale - r['gas'] - r['slip'])/CAPITAL_USD*100:.2f}% ops={r['ops']} in_range={r['in_range_ratio']*100:.1f}%")

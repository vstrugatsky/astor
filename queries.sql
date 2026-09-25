select id, run_id, session as day, alert_time as time,
       case when latency_sec is null then null else printf('%.1f', latency_sec) end as l,
       symbol as t, direction as d,
       case when price is null then null else printf('%.2f', price) end as 'P',
       case when leg_size_adr is null then null else printf('%.2f', leg_size_adr) end as 'legx',
       case when move_rvol is null then null else printf('%.2f', move_rvol) end as 'mrvol',
       case when ema9_extension_adr is null then null else printf('%.2f', ema9_extension_adr) end as 'e9x',
       case when accel_price is null then null else printf('%.2f', accel_price) end as 'acP',
       case when accel_vol is null then null else printf('%.2f', accel_vol) end as 'acV',
       case when anchor is null then null else printf('%.2f', anchor) end as 'anc',
       case when leg_extreme is null then null else printf('%.2f', leg_extreme) end as 'ext'
from latest_alerts
where kind = 'capitulation'
and day >= '2026-09-17'
order by run_id desc, symbol asc, alert_time asc;

select id, run_id, session as day, trigger_time as time,
       symbol as t, setup as s, case when trigger_price is null then null else printf('%.2f', trigger_price) end as 'P',
       case when move_rvol is null then null else printf('%.2f', move_rvol) end as 'mRvol',
       case when leg_size_pct is null then null else printf('%.2f%%', leg_size_pct) end as 'lS',
       case when leg_size_pct is null or adr_pct is null then null else printf('%.2f', leg_size_pct / adr_pct) end as 'lS/Adr',
       case when leg_duration_minutes is null then null else leg_duration_minutes end as 'legD',
       case when accel_price is null then accel_price else printf('%.2f', accel_price) end as 'accP',
       case when accel_vol is null then accel_price else printf('%.2f', accel_vol) end as 'accV',
       case when ema9_extension_adr is null then ema9_extension_adr else printf('%.2f', ema9_extension_adr) end as 'maExt',
       case when retrace_minutes is null then null else retrace_minutes end as 'rD',
       retrace_via as 'rV',
       case when retrace_pct is null then null else printf('%.2f%%', retrace_pct) end as 'retr',
        case when adr_pct is null then null else printf('%.2f%%', adr_pct) end as 'adr',
       case when avwap_pct is null then null else printf('%.2f%%', avwap_pct) end as 'avw',
       case when avwap_pct is null then null else printf('%.2f%%', avwap_pct / adr_pct)  end as 'ext',
        auto_grade as ag, grade as g, notes
from latest_triggers
where setup in ('REV-S', 'REV-L')
and day >= '2026-09-21'
order by day desc, symbol, time;

SELECT trigger_time, symbol, setup, trigger_price entry, level_value stop, stop_source,
       round(day_range_adr,2) range_adr, round(touch_adr,3) touch, round(depth_adr,2) depth,
       round(recovery_adr,3) recovery, bars_since_touch bars, round(rvol,2) rvol, level_stop_shares sh, latency_sec
FROM trigger_detail
WHERE session='2026-09-21' AND setup LIKE 'AVT%'
ORDER BY symbol, trigger_time;

-- direction as d,
--        case when leg_duration_minutes is null or leg_size_pct is null or adr_pct is null then null
--            else printf('%.2f', leg_size_pct / adr_pct / leg_duration_minutes) end as 'mAcc',
--      case when leg_size_adr is null then null else printf('%.2f', leg_size_adr) end as 'l2Adr',
--        case when level_value is null then null else printf('%.2f', level_value) end as 'Lv',
--        case when session_open is null then null else printf('%.2f', session_open) end as 'O',
--        case when session_low is null then null else printf('%.2f', session_low) end as 'L',
--        case when session_high is null then null else printf('%.2f', session_high) end as 'H',
--       case when rvol is null then null else printf('%.2f', rvol) end as Rvol,
--      case when latency_sec is null then null else printf('%.1f', latency_sec) end as l,
--      case when session_extreme_pct is null then null else printf('%.2f%%', session_extreme_pct) end as 'toE',
-- case when level_stop_pct is null then null else printf('%.2f%%', level_stop_pct) end as 'toLv',
--        case when anchor is null then null else printf('%.2f', anchor) end as 'anch',
--       case when leg_extreme is null then null else printf('%.2f', leg_extreme) end as 'legE',


select * from latest_triggers;

update triggers set grade = 1, notes = 'too late, low adr' where id in (373,383);

{"anchor": 2.43, "leg_extreme": 3.05, "leg_extreme_start": "2026-08-25T11:00:00-04:00",
    "leg_size_adr": 5.4696, "leg_size_pct": 25.5144, "retrace_pct": 25.8226, "retrace_minutes": 75.0,
    "move_rvol": 16.28, "adr_pct": 4.6648, "avwap": 2.9021209773576016, "avwap_pct": -0.4211, "rvol": 13.59,
    "reclaim_pct": 5.2492, "max_loss_dollars": 100.0, "level_stop_pct": 5.2492, "level_stop_shares": 624, "level_stop_dollars": 1803.3,
    "session_open": 2.76, "session_high": 3.05, "session_low": 2.615, "auto_grade": 2}

{"anchor": 2.43, "leg_extreme": 3.09, "leg_extreme_start": "2026-08-25T14:23:00-04:00",
    "leg_size_adr": 5.8224, "leg_size_pct": 27.1605, "retrace_pct": 25.1667, "retrace_minutes": 85.0,
    "move_rvol": 12.67, "adr_pct": 4.6648, "avwap": 2.930769644778437, "avwap_pct": -0.2344, "rvol": 11.76,
    "reclaim_pct": 5.3754, "max_loss_dollars": 100.0, "level_stop_pct": 5.3754, "level_stop_shares": 602, "level_stop_dollars": 1760.19,
    "session_open": 2.76, "session_high": 3.09, "session_low": 2.615, "auto_grade": 1}

select * from triggers where symbol = 'RZLV' order by run_id desc, symbol asc, detection_ts asc;

select format(trigger_price, 2) from triggers order by run_id desc, symbol asc, bar_end asc;

select * from tri

{"undercut_low": 212.04, "undercut_start": "2026-08-20T09:52:00-04:00", "bars_below": 45, "bars_armed": 172, "adr_pct": 11.0406,
    "excursion_adr": 0.0692, "excursion_pct": 0.8, "reclaim_adr": 0.0789, "reclaim_pct": 0.9123, "combined_pct": 1.7123, "avwap": 216.36184433536027,
    "avwap_pct": -0.3059, "rvol": 0.71, "max_loss_dollars": 100.0, "level_stop_shares": 51, "level_stop_dollars": 11000.7, "session_extreme": 212.04,
    "session_extreme_pct": 1.7261, "session_stop_shares": 27, "session_stop_dollars": 5823.9}I
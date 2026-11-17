from . import calendar, candle_patterns, daily_plan, fakeout, fib_day, gann_drive, harmonic, levels, mid_level, morning_edge, ng_drive, ng_fvg, orderflow, sideways, mean_reversion, sr_reversal, standard, wyckoff

BUILTINS = [*standard.INDICATORS, *levels.INDICATORS, *fib_day.INDICATORS, *harmonic.INDICATORS, *sideways.INDICATORS, *fakeout.INDICATORS, *gann_drive.INDICATORS, *morning_edge.INDICATORS, *daily_plan.INDICATORS, *ng_drive.INDICATORS, *ng_fvg.INDICATORS, *mid_level.INDICATORS, *orderflow.INDICATORS, *calendar.INDICATORS, *wyckoff.INDICATORS, *sr_reversal.INDICATORS, *candle_patterns.INDICATORS, *mean_reversion.INDICATORS]
for _i in BUILTINS:
    _i.source = "builtin"

from . import daily_plan, fakeout, gann_drive, harmonic, levels, morning_edge, sideways, standard

BUILTINS = [*standard.INDICATORS, *levels.INDICATORS, *harmonic.INDICATORS, *sideways.INDICATORS, *fakeout.INDICATORS, *gann_drive.INDICATORS, *morning_edge.INDICATORS, *daily_plan.INDICATORS]
for _i in BUILTINS:
    _i.source = "builtin"

import sys, time, json
sys.path.insert(0,'.')
from bench import *
sc, seed, hours = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
F = load(*BOTS['nemesis'])
log = []
class B(F):
    def _track_live(self, position, cash, volume, now):
        had = self.had_position
        arm = self.live_arm
        cb = self.live_cash_before
        super()._track_live(position, cash, volume, now)
        if had and position is None:
            log.append((round(now-self.start_ts), arm.name if arm else None, round(cash-cb,3), round(volume,1), round(cash,2), round(arm.score,3) if arm else None, round(arm.n_eff,1) if arm else None))
r = run_path(B, seed, SCENARIOS[sc], hours=hours)
print('vol %.1f cash %.2f cycles %d' % (r.volume, r.cash, r.cycles))
for l in log[:40]: print(l)

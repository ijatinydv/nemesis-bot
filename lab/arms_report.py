import sys, time, json
sys.path.insert(0,'.')
from bench import *
from multiprocessing import Pool
F = load(*BOTS['nemesis'])
holder = {}
class B(F):
    def _live_entry(self, *a, **k):
        holder['b'] = self
        return {"action": "HOLD"}
def job(a):
    sc, seed = a
    h = {}
    class BB(F):
        def _live_entry(self, *a, **k):
            h['b'] = self
            return {"action": "HOLD"}
    r = run_path(BB, seed, SCENARIOS[sc], hours=24)
    b = h['b']
    rows = []
    for arm in b.arms:
        n = len(arm.cycles)
        if n < 15: continue
        pnl = sum(c[1] for c in arm.cycles); vol = sum(c[2] for c in arm.cycles)
        rows.append((pnl/vol, n, arm.name))
    rows.sort(reverse=True)
    return sc, seed, rows[:4], len(rows)
if __name__ == '__main__':
    scs = sys.argv[1].split(',') if len(sys.argv)>1 else list(SCENARIOS)
    with Pool(2) as p:
        for sc, seed, rows, n in p.map(job, [(s, 1) for s in scs]):
            print(sc, 'arms>=15 cycles:', n)
            for r in rows: print('   %+.4f n=%d %s' % r)

"""Benchmark harness: run a bot over scenarios x seeds and print qualification metrics."""
import importlib.util, sys, time, statistics as st, argparse
from multiprocessing import Pool
sys.path.insert(0, '.')
from yeno_sim import *

def load(path, cls):
    spec = importlib.util.spec_from_file_location(cls + path, path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return getattr(m, cls)

BOTS = {
    'house': ('../yeno-house-bot-v1.py', 'HouseBot'),
    'apex': ('../apex_bot.py', 'ApexBot'),
    'nemesis': ('../nemesis_bot.py', 'NemesisBot'),
}

def job(a):
    bot, scname, seed, hours, cadence = a
    path, cls = BOTS[bot]
    factory = load(path, cls)
    r = run_path(factory, seed, SCENARIOS[scname], hours=hours, cadence=cadence)
    return scname, seed, r

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('bot'); ap.add_argument('--sc', default='all'); ap.add_argument('--seeds', type=int, default=4)
    ap.add_argument('--hours', type=float, default=24); ap.add_argument('--cadence', type=float, default=1.0)
    ap.add_argument('--start', type=int, default=0)
    a = ap.parse_args()
    scs = list(SCENARIOS) if a.sc == 'all' else a.sc.split(',')
    jobs = [(a.bot, s, sd, a.hours, a.cadence) for s in scs for sd in range(a.start, a.start + a.seeds)]
    t0 = time.time()
    with Pool(2) as p:
        out = p.map(job, jobs, chunksize=1)
    for s in scs:
        rs = [r for n, sd, r in out if n == s]
        vols = [r.volume for r in rs]
        q = [r for r in rs if r.crossed]
        print(f"{s:10s} vol med {st.median(vols):7.1f} max {max(vols):7.1f} | cross {len(q)}/{len(rs)} flat {sum(r.flat for r in rs)}/{len(rs)}"
              f" | cash med {st.median(r.cash for r in rs):5.2f} | cash@cross " +
              (f"med {st.median(r.cash_at_cross for r in q):5.2f} min {min(r.cash_at_cross for r in q):5.2f}" if q else "-") +
              f" | cycles med {st.median(r.cycles for r in rs):.0f} unsold {sum(r.unsold_settlements for r in rs)}")
    print(f"elapsed {time.time()-t0:.0f}s")

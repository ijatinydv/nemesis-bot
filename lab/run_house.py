import importlib.util, sys, time, statistics as st
sys.path.insert(0, '.')
from yeno_sim import *
def load(path, cls):
    spec = importlib.util.spec_from_file_location(cls, path); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return getattr(m, cls)
House = load('../yeno-house-bot-v1.py', 'HouseBot')
sc = SCENARIOS[sys.argv[1]]
t0=time.time()
for seed in range(int(sys.argv[2])):
    r = run_path(House, seed, sc, hours=float(sys.argv[3]) if len(sys.argv)>3 else 24)
    print(seed, round(r.volume,1), round(r.cash,2), r.flat, r.cycles, r.wins, r.rejected, r.unsold_settlements, round(r.fees,2), round(time.time()-t0,1))

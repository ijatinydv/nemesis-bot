import sys, time
sys.path.insert(0,'.')
from bench import *
bot, sc, seed = sys.argv[1], sys.argv[2], int(sys.argv[3])
hours = float(sys.argv[4]) if len(sys.argv)>4 else 24
path, cls = BOTS[bot]
F = load(path, cls)
holder = {}
def fac():
    b = F(); holder['b']=b; return b
t0=time.time()
r = run_path(fac, seed, SCENARIOS[sc], hours=hours)
print(bot, sc, seed, 'vol %.1f cash %.2f flat %s cycles %d wins %d rej %d unsold %d crossed %s t=%.0fs' % (r.volume, r.cash, r.flat, r.cycles, r.wins, r.rejected, r.unsold_settlements, r.crossed, time.time()-t0))
b = holder['b']
if hasattr(b,'status'):
    import json; st=b.status(); print(json.dumps(st, indent=1))

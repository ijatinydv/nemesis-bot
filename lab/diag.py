import sys
sys.path.insert(0,'.')
from bench import *
sc, seed, hours = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
F = load(*BOTS['nemesis'])
inst=[]
class B(F):
    def __init__(s):
        super().__init__(); inst.append(s)
r = run_path(B, seed, SCENARIOS[sc], hours=hours)
b=inst[0]
print(sc,'vol %.1f cash %.2f cycles %d'%(r.volume,r.cash,r.cycles))
print('active',[a.name for a in b.active],'delta',b.delta)
top=sorted(b.arms,key=lambda a:a.score,reverse=True)[:6]
for a in top: print(' ',a.name,round(a.score,4),round(a.n_eff,1),len(a.cycles))
print(sum(len(a.cycles) for a in b.arms),'total shadow cycles')

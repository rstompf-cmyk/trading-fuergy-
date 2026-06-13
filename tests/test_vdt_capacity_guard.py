"""Testy VDT kapacitnej poistky — kombinovaná SOC (DAM+VDT) nesmie prekročiť pásmo."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.vdt_capacity_guard import clip_extras_to_capacity as clip, simulate_combined_soc as sim

CAP = 2000.0
def _max(soc0,dc,dd,ex): return max(sim(soc0,dc,dd,ex,0.95,0.95))
def _min(soc0,dc,dd,ex): return min(sim(soc0,dc,dd,ex,0.95,0.95))

def test_fit_no_clip():
    dc=[0.0]*24; dd=[0.0]*24; ex={12:('BUY',500.0),18:('SELL',500.0)}
    c,r=clip(1000,dc,dd,ex,CAP,0.95,0.95,5,100,0)
    assert r==[] and c[12][1]==500.0

def test_clip_to_soc_max():
    dc=[0.0]*24; dd=[0.0]*24; ex={12:('BUY',500.0)}
    c,r=clip(1900,dc,dd,ex,CAP,0.95,0.95,5,100,0)
    assert _max(1900,dc,dd,c) <= CAP+1 and r

def test_reserve_respected():
    dc=[0.0]*24; dd=[0.0]*24; ex={12:('BUY',500.0)}
    c,r=clip(1700,dc,dd,ex,CAP,0.95,0.95,5,100,reserve_pct=10.0)
    assert _max(1700,dc,dd,c) <= CAP*0.90+1

def test_future_dam_discharge_reserved():
    dc=[0.0]*24; dd=[0.0]*24; dd[20]=1500.0; ex={12:('SELL',800.0)}
    c,r=clip(1900,dc,dd,ex,CAP,0.95,0.95,5,100,0)
    assert _min(1900,dc,dd,c) >= -1   # SOC neklesne pod min ani po DAM výdaji o 20h

if __name__=="__main__":
    test_fit_no_clip(); test_clip_to_soc_max(); test_reserve_respected(); test_future_dam_discharge_reserved()
    print("✓ všetky 4 testy prešli")

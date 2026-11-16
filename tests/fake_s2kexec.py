#!/usr/bin/env python3
"""Stand-in for the s2kexec bus helper: same line protocol, backed by the simulated S2000.
Env FAIL_AFTER=N makes it report a bus error on command N+1 (to test failure handling)."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import s2ksend as s

mock = s.MockS2000(samples=['EXISTING'], free_words=1_900_000)
fail_after = int(os.environ.get('FAIL_AFTER', '-1'))
print('READY fake helper', flush=True)
n = 0
for line in sys.stdin:
    line = line.strip()
    if line == 'Q':
        break
    _, cdbhex, d, ln, datahex = line.split(' ')
    if fail_after >= 0 and n >= fail_after:
        print('E selection timeout (selected=0)', flush=True)
        break
    n += 1
    try:
        st, xfer, data = mock.execute(bytes.fromhex(cdbhex), {'N': 'none', 'I': 'in', 'O': 'out'}[d],
                                      b'' if datahex == '-' else bytes.fromhex(datahex), int(ln))
    except s.SendError as e:
        print('E ' + str(e), flush=True)
        break
    print(f'R {st} {xfer} {data.hex() if data else "-"}', flush=True)

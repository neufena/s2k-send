#!/usr/bin/env python3
"""Compare s2ksend.py's wire bytes with the output of tests/parity.cpp (the reference C++).
Usage:  ./parity <reference-output-file>   i.e.   parity.py /tmp/parity.out"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import s2ksend as s

ref = dict(l.split(' ', 1) for l in open(sys.argv[1]).read().splitlines() if ' ' in l)
h = s.sample_header('TEST TONE-1', 60, 30000)
mine = {
    'header': h.hex(),
    'sysex_sample': s.sysex_sample(h, 1).hex(),
    'post': s.post_object().hex(),
    'cdb_send392': s.transfer_cdb(12, 392).hex(),
    'cdb_pcm16384': s.transfer_cdb(12, 16384, True).hex(),
    'cdb_recv141': s.transfer_cdb(14, 141).hex(),
    'hdrreq0': s.header_read_request(5, 0).hex(),
    'hdrreq1': s.header_read_request(5, 64).hex(),
    'hdrreq2': s.header_read_request(5, 128).hex(),
    'pcmreq': s.pcm_read_request(5, 60000).hex(),
    'inv0': s.LIST_PROGRAMS.hex(),
}
bad = 0
for k, v in mine.items():
    ok = ref.get(k) == v
    bad += not ok
    print(('MATCH    ' if ok else 'MISMATCH ') + k)
sys.exit(1 if bad else 0)

#!/usr/bin/env python3
"""Stereo-pair decoding (read-only). Uses the real values reported by an S2000 on 16 Nov 2026."""
import os, sys, types
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import s2kcore as s

# real S2000: 'LP-1 102  -L' at slot 0 -> partner field 3c60, 'LP-1 102  -R' at slot 1 -> 3060 (little-endian)
real = [(0, 0x603c, 1, 0x6030)]
stride, base, ok, notes = s.partner_map(real)
assert (stride, base, ok, notes) == (12, 0x6030, True, []), (stride, base, ok, notes)
# a second pair that follows the same rule (slots 2/3), and one that does not
assert s.partner_map(real + [(2, 0x6030 + 12 * 3, 3, 0x6030 + 12 * 2)])[2] is True
assert s.partner_map(real + [(2, 0x7000, 3, 0x6048)])[2] is False
assert s.partner_map([(0, 0x603d, 2, 0x6030)])[0] is None          # 13 is not a multiple of 2 slots: no rule

# end to end against the simulator: two samples named like the S2000's own pair, fields as reported above
m = s.MockS2000(samples=[])
for nm, field in (('LP-1 102  -L', 0x603c), ('LP-1 102  -R', 0x6030)):
    h = bytearray(s.SAMPLE_TEMPLATE); h[3:15] = s.encode_name(nm); h[136:138] = field.to_bytes(2, 'little')
    m.samples.append(dict(name=s.encode_name(nm), header=bytes(h), pcm=b''))
def report(mock):
    bus = s.BusManager(mock=mock, keep_bus=0)
    with bus.session() as sess:
        return s.op_pairs(sess)

r = report(m)
assert r['samples'] == 2 and len(r['pairs']) == 1 and r['pairs'][0]['linked'], r
pr = r['pairs'][0]
assert (pr['left']['slot'], pr['left']['partner'], pr['right']['slot'], pr['right']['partner']) == (0, 0x603c, 1, 0x6030), pr
assert r['rule'] == {'stride': 12, 'base': 0x6030, 'consistent': True, 'notes': []}, r['rule']
# no pairs on the S2000
assert report(s.MockS2000(samples=['KICK', 'SNARE'])) == {'samples': 2, 'pairs': [], 'rule': None, 'layout': None}
# an unlinked pair (mono-style ffff) is reported as such
m3 = s.MockS2000(samples=[])
for nm in ('A         -L', 'A         -R'):
    h = bytearray(s.SAMPLE_TEMPLATE); h[3:15] = s.encode_name(nm); h[136:138] = b'\xff\xff'
    m3.samples.append(dict(name=s.encode_name(nm), header=bytes(h), pcm=b''))
r3 = report(m3)
assert len(r3['pairs']) == 1 and not r3['pairs'][0]['linked'] and r3['rule'] is None, r3

# the real S2000 with 29 pairs loaded from disk (17 Nov 2026): (slot_l, field_l, slot_r, field_r)
real29 = [(0, 0x6030, 1, 0x6024), (2, 0x6048, 3, 0x603c), (4, 0x6084, 5, 0x6078), (6, 0x609c, 7, 0x6090),
          (8, 0x60e4, 9, 0x60d8), (10, 0x60fc, 11, 0x60f0), (12, 0x6114, 13, 0x6108), (14, 0x6168, 15, 0x615c),
          (16, 0x6180, 17, 0x6174), (18, 0x6198, 19, 0x618c), (20, 0x61b0, 21, 0x61a4), (22, 0x6204, 23, 0x61f8),
          (24, 0x621c, 25, 0x6210), (26, 0x6234, 27, 0x6228), (28, 0x624c, 29, 0x6240), (30, 0x6294, 31, 0x6288),
          (32, 0x62ac, 33, 0x62a0), (34, 0x62c4, 35, 0x62b8), (36, 0x630c, 37, 0x6300), (38, 0x6324, 39, 0x6318),
          (40, 0x633c, 41, 0x6330), (42, 0x6378, 43, 0x636c), (44, 0x6390, 45, 0x6384), (46, 0x63d8, 47, 0x63cc),
          (48, 0x63f0, 49, 0x63e4), (50, 0x6408, 51, 0x63fc), (52, 0x6450, 53, 0x6444), (54, 0x6468, 55, 0x645c),
          (56, 0x6480, 57, 0x6474)]
lay = s.pair_layout(real29)
assert lay['pairs'] == 29 and lay['spacing'] == 12 and lay['spacing_consistent'], lay
assert [(h['before_slot'], h['entries']) for h in lay['holes']] == [(4, 3), (8, 4), (14, 5), (22, 5), (30, 4), (36, 4), (42, 3), (46, 4), (52, 4)]
assert lay['extra_entries'] == 36 and lay['first_address'] == 0x6024 and lay['last_address'] == 0x6480
assert s.partner_map(real29)[2] is False                      # a plain "base + 12 x slot" rule does NOT hold
# a contiguous table has no holes
assert s.pair_layout([(0, 0x603c, 1, 0x6030), (2, 0x6054, 3, 0x6048)])['holes'] == []
print('test_pairs OK')

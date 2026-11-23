#!/usr/bin/env python3
"""PiSCSI setup retention: snapshot before stopping, reconcile after restarting. Uses a fake systemctl/scsictl."""
import os, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
import s2kcore as core
from s2kcore import PiscsiManager, parse_scsictl_list


class FakePiscsi:
    """Behaves like systemd + scsictl: stopping clears the devices, starting begins empty, restarting the web UI loads
    its default config (which may differ from what was attached before)."""
    def __init__(self, devices, default, fail_attach=()):
        self.active, self.default, self.fail_attach, self.calls = True, default, set(fail_attach), []
        self.dev = {k: dict(v) for k, v in devices.items()}

    def table(self):
        if not self.dev:
            return 'No images currently attached.\n'
        out = ['+----+-----+------+-------------------------------------', '| ID | LUN | TYPE | IMAGE FILE',
               '+----+-----+------+-------------------------------------']
        for (i, l), d in sorted(self.dev.items()):
            out.append(f'|  {i} |  {l}  | {d["type"]} | {d["file"] or "NO MEDIA"}' + (' (WRITEPROTECTED)' if d['protected'] else ''))
        return '\n'.join(out + ['+----+-----+------+-------------------------------------']) + '\n'

    def __call__(self, cmd, timeout=None):
        self.calls.append(cmd)
        if cmd[0] == 'systemctl':
            verb, unit = cmd[1], cmd[-1]
            if verb == 'is-active':
                return (0, 'active\n') if self.active else (3, 'inactive\n')
            if verb == 'stop':
                self.active, self.dev = False, {}
            elif verb == 'start':
                self.active, self.dev = True, {}
            elif verb == 'is-enabled':
                return 0, 'enabled\n'
            elif verb == 'restart' and unit == 'piscsi-web' and self.active:
                for k, v in self.default.items():
                    self.dev.setdefault(k, dict(v))
            return 0, ''
        assert cmd[0] == 'scsictl'
        if not self.active:
            return 1, 'Cannot connect to piscsi'
        if cmd[1] == '-l':
            return 0, self.table()
        addr = cmd[cmd.index('-i') + 1]
        i, l = (int(x) for x in (addr.split(':') + ['0'])[:2])
        verb = cmd[cmd.index('-c') + 1]
        if verb == 'attach':
            if (i, l) in self.dev or (i, l) in self.fail_attach:
                return 1, 'cannot attach'
            self.dev[(i, l)] = dict(type=cmd[cmd.index('-t') + 1].upper(), protected=False,
                                    file=cmd[cmd.index('-f') + 1] if '-f' in cmd else None)
        elif verb == 'detach':
            if (i, l) not in self.dev:
                return 1, 'nothing there'
            del self.dev[(i, l)]
        elif verb == 'protect':
            if (i, l) not in self.dev:
                return 1, 'nothing there'
            self.dev[(i, l)]['protected'] = True
        return 0, ''


dev = lambda t, f=None, p=False: dict(type=t, file=f, protected=p)
BEFORE = {(0, 0): dev('SCHD', '/img/a.hds'), (1, 0): dev('SCCD'), (4, 0): dev('SCRM', '/img/f.hdr', True),
          (5, 1): dev('SCHD', '/home/pi/images/My Disk.hda')}
DEFAULT = {(0, 0): dev('SCHD', '/img/a.hds'), (2, 0): dev('SCHD', '/img/old.hds')}     # the web UI's default config
tmp = tempfile.mkdtemp()

def manager(fake, mode='exact', state=None):
    logs = []
    return PiscsiManager(run=fake, log=logs.append, mode=mode, state_file=state, settle_s=5, sleep=lambda s: None), logs

# ---- parsing
assert parse_scsictl_list('No images currently attached.\n') == []
t = FakePiscsi(BEFORE, {}).table()
got = parse_scsictl_list(t)
assert [(d['id'], d['lun'], d['type'], d['file'], d['protected']) for d in got] == [
    (0, 0, 'SCHD', '/img/a.hds', False), (1, 0, 'SCCD', None, False), (4, 0, 'SCRM', '/img/f.hdr', True),
    (5, 1, 'SCHD', '/home/pi/images/My Disk.hda', False)], got
old = '+----+----+------+------------\n| ID | UN | TYPE | DEVICE STATUS\n+----+----+------+------------\n|  6 |  0 | SCHD | /home/pi/x.hda\n'
assert parse_scsictl_list(old)[0]['file'] == '/home/pi/x.hda'
assert parse_scsictl_list('| 0 | 0 | SCHD | /p/x.hds | SOME PRODUCT | 512\n')[0]['file'] == '/p/x.hds'

# ---- exact: back to exactly the setup before, even though the default config differs
fake = FakePiscsi(BEFORE, DEFAULT)
pm, logs = manager(fake)
pm.snapshot()
assert pm.info()['devices'] == 4 and pm.info()['pending']
pm.stop()
assert not fake.active and fake.dev == {}
res = pm.restore()
assert res['restored'] and fake.dev == BEFORE, (fake.dev, res)
assert any('ID 2:0' in x for x in res['detached']) and len(res['attached']) == 3, res
assert not pm.info()['pending']

# ---- attach-only keeps what the default config added
fake = FakePiscsi(BEFORE, DEFAULT)
pm, _ = manager(fake, 'attach-only')
pm.snapshot(); pm.stop(); res = pm.restore()
assert (2, 0) in fake.dev and all(k in fake.dev for k in BEFORE) and res['detached'] == [], (fake.dev, res)

# ---- off: just a restart, as before this feature
fake = FakePiscsi(BEFORE, DEFAULT)
pm, _ = manager(fake, 'off')
pm.snapshot(); pm.stop(); res = pm.restore()
assert fake.dev == DEFAULT and not res['restored'] and pm.info() is None

# ---- a device that cannot be re-attached is reported, nothing raises
fake = FakePiscsi(BEFORE, DEFAULT, fail_attach={(4, 0)})
pm, logs = manager(fake)
pm.snapshot(); pm.stop(); res = pm.restore()
assert not res['restored'] and [f['id'] for f in res['failed']] == [4] and 'WARNING' in res['note'], res
assert (0, 0) in fake.dev and (5, 1) in fake.dev

# ---- scsictl missing at snapshot time: no snapshot, restore is a plain restart
fake = FakePiscsi(BEFORE, DEFAULT)
nofail = lambda cmd, timeout=None: (127, '') if cmd[0] == 'scsictl' else fake(cmd)
pm, logs = manager(nofail)
pm.snapshot(); assert pm.info() is None and any('could not read' in x for x in logs)

# ---- the snapshot survives a server restart while PiSCSI is stopped
state = os.path.join(tmp, 'snap.json')
fake = FakePiscsi(BEFORE, DEFAULT)
pm1, _ = manager(fake, state=state); pm1.snapshot(); pm1.stop()
pm2, logs2 = manager(fake, state=state)                      # "new server process"
assert pm2.info()['pending'] and any('waiting to be restored' in x for x in logs2)
assert pm2.restore()['restored'] and fake.dev == BEFORE
pm3, _ = manager(fake, state=state); assert pm3.info() is None   # not pending any more

# ---- BusManager: stopped during a session, restored after
helper = [sys.executable, os.path.join(HERE, 'fake_s2kexec.py')]
fake = FakePiscsi(BEFORE, DEFAULT)
pm, _ = manager(fake)
bus = core.BusManager(helper=helper, manage_piscsi=True, require_root=False, keep_bus=0, piscsi=pm)
with bus.session() as sess:
    assert not fake.active and fake.dev == {}
    assert core.op_inventory(sess)['samples']
assert fake.active and fake.dev == BEFORE and bus.last_restore['restored'], (fake.dev, bus.last_restore)
assert bus.status()['last_restore']['restored']

# keep_bus > 0: PiSCSI stays stopped with the snapshot pending; closing the server restores it at once
fake = FakePiscsi(BEFORE, DEFAULT)
pm, _ = manager(fake)
bus = core.BusManager(helper=helper, manage_piscsi=True, require_root=False, keep_bus=60, piscsi=pm)
with bus.session() as sess:
    pass
assert not fake.active and bus.status()['piscsi_snapshot']['pending']
with bus.session() as sess:                                    # a second operation does not re-snapshot an empty setup
    pass
assert pm.info()['devices'] == 4
bus.close()
assert fake.active and fake.dev == BEFORE

# a failed send leaves PiSCSI stopped; the snapshot is kept; recover() restores everything
os.environ['FAIL_AFTER'] = '40'
fake = FakePiscsi(BEFORE, DEFAULT)
pm, _ = manager(fake)
bus = core.BusManager(helper=helper, manage_piscsi=True, require_root=False, keep_bus=0, piscsi=pm)
try:
    with bus.session() as sess:
        core.op_send(sess, [core.Item('BROKEN', bytes(range(256)) * 400, 44100, 'x', False)], 60)
    raise SystemExit('expected the send to fail')
except core.SendError:
    pass
os.environ.pop('FAIL_AFTER')
assert bus.state == 'needs_attention' and not fake.active and pm.info()['pending']
bus.recover()
assert bus.state == 'idle' and fake.active and fake.dev == BEFORE
# the keep-bus wait is reported as a state, not left to look like a fault
import time
rb = core.BusManager(mock=core.MockS2000(samples=[]), keep_bus=0)
assert rb.status()['piscsi_restore'] is None
rb._restart_at = time.monotonic() + 15
st = rb.status()['piscsi_restore']
assert st['state'] == 'scheduled' and 14 <= st['restart_in_s'] <= 15, st
rb._restoring = True
assert rb.status()['piscsi_restore'] == {'state': 'restoring', 'restart_in_s': 0}
print('test_piscsi OK')

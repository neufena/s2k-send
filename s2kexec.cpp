// SPDX-License-Identifier: BSD-3-Clause
//
// s2kexec - tiny SCSI initiator helper for s2ksend.py.
//
// Runs ON the Raspberry Pi, as root, with PiSCSI STOPPED. It opens the PiSCSI
// board as a SCSI initiator (using the gpio-source/ files, which are BSD-3
// code derived from SCSI2Pi/PiSCSI - see gpio-source/LICENSE and NOTICE) and
// executes exactly the commands s2ksend.py asks for, one per line. It contains
// no Akai logic at all.
//
// Line protocol (stdin -> stdout, hex = lowercase hex, "-" = empty):
//   start-up   ->  READY <bus description>        or  ERROR <message>
//   X <cdb-hex> <N|I|O> <len> <data-hex|->  ->  R <scsi-status> <transferred> <data-hex|->
//                                            or  E <message>      (helper then exits)
//   Q                                        ->  (releases the bus and exits)
//
// Deliberately: no retries, no bus reset on timeout, no automatic REQUEST
// SENSE. After any transport error the helper stops touching the bus.
//
// Build (on the Pi):
//   make        (see Makefile)

#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <memory>
#include <sstream>
#include <string>
#include <vector>
#include <unistd.h>

#include "gpio_initiator.h"

namespace scsiexec {
std::unique_ptr<GpioBus> make_rpi_bus();
}
using namespace scsiexec;

static volatile sig_atomic_t g_stop = 0;
static void on_signal(int) { g_stop = 1; }

static bool from_hex(const std::string &s, std::vector<uint8_t> &out)
{
    out.clear();
    if (s == "-") return true;
    if (s.size() % 2) return false;
    auto nib = [](char c) -> int {
        if (c >= '0' && c <= '9') return c - '0';
        if (c >= 'a' && c <= 'f') return c - 'a' + 10;
        if (c >= 'A' && c <= 'F') return c - 'A' + 10;
        return -1;
    };
    for (size_t i = 0; i < s.size(); i += 2) {
        int h = nib(s[i]), l = nib(s[i + 1]);
        if (h < 0 || l < 0) return false;
        out.push_back(static_cast<uint8_t>(h << 4 | l));
    }
    return true;
}

static std::string to_hex(const uint8_t *p, size_t n)
{
    static const char *d = "0123456789abcdef";
    if (!n) return "-";
    std::string s;
    s.reserve(n * 2);
    for (size_t i = 0; i < n; ++i) {
        s += d[p[i] >> 4];
        s += d[p[i] & 15];
    }
    return s;
}

int main(int argc, char **argv)
{
    int initiator = 7, target = -1, timeout = 1;
    bool irq_mask = true;
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "--initiator" && i + 1 < argc) initiator = atoi(argv[++i]);
        else if (a == "--target" && i + 1 < argc) target = atoi(argv[++i]);
        else if (a == "--timeout" && i + 1 < argc) timeout = atoi(argv[++i]);
        else if (a == "--no-irq-mask") irq_mask = false;
        else {
            fprintf(stderr, "usage: s2kexec --initiator ID --target ID [--timeout SECS] [--no-irq-mask]\n");
            return 2;
        }
    }
    setvbuf(stdout, nullptr, _IOLBF, 0);
    if (geteuid() != 0) { printf("ERROR must run as root\n"); return 2; }
    if (target < 0 || target > 7 || initiator < 0 || initiator > 7 || target == initiator) {
        printf("ERROR bad --initiator/--target\n");
        return 2;
    }

    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = on_signal;           // no SA_RESTART: getline is interrupted
    sigaction(SIGINT, &sa, nullptr);
    sigaction(SIGTERM, &sa, nullptr);
    sigaction(SIGHUP, &sa, nullptr);

    auto bus = make_rpi_bus();
    bus->set_irq_mask(irq_mask);
    std::string err;
    if (!bus->init(err)) { printf("ERROR %s\n", err.c_str()); return 2; }

    GpioInitiator ini(*bus, initiator);
    ini.set_target(target, 0);
    ini.disable_timeout_reset();         // never reset the bus behind our back

    std::string warn = bus->warning();
    printf("READY %s%s%s\n", bus->describe().c_str(), warn.empty() ? "" : " | WARNING: ", warn.c_str());

    int rc = 0;
    std::string line;
    while (!g_stop && std::getline(std::cin, line)) {
        if (line == "Q") break;
        std::istringstream in(line);
        std::string op, cdbhex, dir, lenstr, datahex;
        in >> op >> cdbhex >> dir >> lenstr >> datahex;
        std::vector<uint8_t> cdb, data;
        if (op != "X" || !from_hex(cdbhex, cdb) || cdb.empty() || cdb.size() > 16 ||
            (dir != "N" && dir != "I" && dir != "O") || !from_hex(datahex, data)) {
            printf("E malformed request\n");
            rc = 3;
            break;
        }
        size_t len = strtoul(lenstr.c_str(), nullptr, 10);
        std::vector<uint8_t> buf;
        if (dir == "O") {
            if (data.size() != len) { printf("E data length mismatch\n"); rc = 3; break; }
            buf = data;
        } else if (dir == "I") {
            buf.assign(len, 0);
        } else if (len) {
            printf("E length given for a no-data command\n");
            rc = 3;
            break;
        }
        InitiatorResult r = ini.execute(cdb, buf.empty() ? nullptr : buf.data(), buf.size(), dir == "O", timeout);
        if (r.status < 0) {
            // Selection failure / phase error / timeout. Stop: do not touch the bus again.
            std::string msg = r.error;
            for (auto &c : msg) if (c == '\n') c = ' ';
            printf("E %s (selected=%d)\n", msg.c_str(), r.selected ? 1 : 0);
            rc = 4;
            break;
        }
        size_t got = dir == "I" ? std::min(r.transferred, buf.size()) : 0;
        printf("R %d %zu %s\n", r.status, r.transferred, to_hex(buf.data(), got).c_str());
    }
    if (g_stop) rc = 5;
    bus->cleanup();
    return rc;
}

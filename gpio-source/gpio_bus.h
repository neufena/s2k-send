// SPDX-License-Identifier: BSD-3-Clause
//
// Copyright (c) 2026 Eric Helgeson <eric@bluescsi.com>
//
// Adapted from SCSI2Pi cpp/buses/bus.{h,cpp} and cpp/buses/board.h
// Copyright (C) 2016-2020 GIMONS, Copyright (C) 2022-2026 Uwe Seimet
// BSD-3-Clause.

#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace scsiexec {

// GPIO numbers on the PiSCSI / SCSI2Pi board layout. These are BCM pin numbers,
// not header positions.
enum GpioPin {
    PIN_ACT = 4,   // command in progress
    PIN_ENB = 5,   // legacy ENB name; debug LED on upstream RaSCSI 2.3
    PIN_IND = 6,   // initiator signal direction (FULLSPEC boards)
    PIN_TAD = 7,   // target signal direction
    PIN_DTD = 8,   // data direction

    PIN_DT0 = 10,
    PIN_DP = 18,   // parity

    PIN_ATN = 19,
    PIN_RST = 20,
    PIN_ACK = 21,
    PIN_REQ = 22,
    PIN_MSG = 23,
    PIN_CD = 24,
    PIN_IO = 25,
    PIN_BSY = 26,
    PIN_SEL = 27,
};

// Which Raspberry Pi generation the peripheral base address comes from.
// Anything newer than a Pi 4 is Unknown on purpose: a Pi 5 puts its GPIO behind
// the RP1 southbridge at a completely different address, so guessing would poke
// someone else's registers.
enum class PiType { Unknown = 0, Pi1 = 1, Pi2 = 2, Pi3 = 3, Pi4 = 4 };

const char *pi_type_name(PiType t);

// 0 for Unknown, so an unmapped board fails at the mmap instead of poking a
// guessed address.
uint32_t pi_peripheral_base(PiType t);

// Parse /proc/device-tree/model. Pure, so the whole model-string family can be
// tested off a Pi.
PiType pi_type_from_model(const std::string &model);

constexpr uint32_t PIN_ATN_MASK = 1u << PIN_ATN;
constexpr uint32_t PIN_RST_MASK = 1u << PIN_RST;
constexpr uint32_t PIN_ACK_MASK = 1u << PIN_ACK;
constexpr uint32_t PIN_REQ_MASK = 1u << PIN_REQ;
constexpr uint32_t PIN_MSG_MASK = 1u << PIN_MSG;
constexpr uint32_t PIN_CD_MASK = 1u << PIN_CD;
constexpr uint32_t PIN_IO_MASK = 1u << PIN_IO;
constexpr uint32_t PIN_BSY_MASK = 1u << PIN_BSY;
constexpr uint32_t PIN_SEL_MASK = 1u << PIN_SEL;

enum class BusPhase {
    BusFree,
    Arbitration,
    Selection,
    Reselection,
    Command,
    DataIn,
    DataOut,
    Status,
    MsgIn,
    MsgOut,
    Reserved,
};

const char *phase_name(BusPhase p);

class GpioBus;

// Interrupts stay masked for as long as this is alive and no longer. A scope
// guard rather than paired calls, because an early return out of a handshake
// loop would otherwise leave the core with interrupts off.
class IrqGuard {
public:
    explicit IrqGuard(GpioBus &bus);
    ~IrqGuard();

    IrqGuard(const IrqGuard &) = delete;
    IrqGuard &operator=(const IrqGuard &) = delete;

private:
    GpioBus &bus_;
};

// SCSI message codes the initiator cares about.
enum : uint8_t {
    MSG_COMMAND_COMPLETE = 0x00,
    MSG_MESSAGE_REJECT = 0x07,
    MSG_IDENTIFY = 0x80,
};

// One SCSI bus, seen from the initiator side.
//
// Signals are stored in NEGATIVE logic exactly as the hardware presents them:
// a bit that is CLEAR means the signal is ASSERTED. get_signal() inverts, so
// everything above this layer reads in positive logic.
class GpioBus {
public:
    virtual ~GpioBus() = default;

    // Sample every signal into the cached word. Everything else reads the
    // cache, so a phase decision is made from one consistent snapshot rather
    // than from pins read at different instants.
    virtual void acquire() = 0;

    // Assert the set bits and release the rest, with odd parity. On hardware
    // this is one write, because "asserted" is a pin direction rather than a
    // level - see the header of gpio_bus_rpi.cpp.
    virtual void set_dat(uint8_t value) = 0;
    virtual uint8_t get_dat() = 0;

    // Which way the board's data transceiver points: true = this initiator is
    // driving the bus. It moves DTD and the output latch, NOT the data pins'
    // direction, which set_dat() sets per byte.
    virtual void set_dir(bool out) = 0;

    virtual bool init(std::string &err) = 0;
    virtual void cleanup() {}
    // Compatibility API: implementations may verify GPIO register state only.
    // A true result is not independent evidence of electrical bus release.
    virtual bool cleanup_verified() { cleanup(); return false; }
    virtual void reset() = 0;

    // Board-level busy indicator, if the board has one.
    virtual void set_active(bool) {}

    // On hardware this sleeps. The mock returns immediately, so a test does
    // not pay the real bus timings.
    virtual void wait_ns(long ns) = 0;

    // Mask interrupts on this core for a transfer, so a scheduler tick cannot
    // land between REQ and ACK. A no-op without the /dev/mem mapping. Call
    // through IrqGuard, never directly.
    virtual void disable_irq() {}
    virtual void enable_irq() {}

    // Turn the masking off. Call before init().
    virtual void set_irq_mask(bool on) { (void)on; }

    // What this bus actually is, for the self test's report.
    virtual std::string describe() const { return "simulated bus"; }

    // Non-empty when the bus came up working but degraded, so the reduced
    // margin is reported instead of turning up later on a marginal cable.
    // Empty on a clean setup.
    virtual std::string warning() const { return {}; }

    // Raw pin access for --gpio-selftest. A mmap of the wrong physical address
    // accepts every write, so only a read-back catches it. raw_gpio() is false
    // on a bus that is not hardware.
    virtual bool raw_gpio() const { return false; }
    virtual void raw_set_mode(int pin, bool output) { (void)pin; (void)output; }
    virtual int raw_get_mode(int pin) const { (void)pin; return -1; }
    virtual void raw_set_level(int pin, bool high) { (void)pin; (void)high; }
    virtual int raw_get_level(int pin) const { (void)pin; return -1; }

    uint32_t signals() const { return signals_; }

    bool get_signal(uint32_t mask) const { return !(signals_ & mask); }
    virtual void set_signal(int pin, bool asserted) = 0;

    bool get_bsy() const { return get_signal(PIN_BSY_MASK); }
    bool get_sel() const { return get_signal(PIN_SEL_MASK); }
    bool get_req() const { return get_signal(PIN_REQ_MASK); }
    bool get_ack() const { return get_signal(PIN_ACK_MASK); }
    bool get_rst() const { return get_signal(PIN_RST_MASK); }
    bool get_msg() const { return get_signal(PIN_MSG_MASK); }
    bool get_cd() const { return get_signal(PIN_CD_MASK); }
    bool get_io() const { return get_signal(PIN_IO_MASK); }

    void set_atn(bool s) { set_signal(PIN_ATN, s); }
    void set_ack(bool s) { set_signal(PIN_ACK, s); }
    void set_rst(bool s) { set_signal(PIN_RST, s); }

    // Virtual: asserting BSY is this initiator arbitrating, so a real board
    // also has to point its target-signal transceivers outwards.
    virtual void set_sel(bool s) { set_signal(PIN_SEL, s); }
    virtual void set_bsy(bool s);

    BusPhase phase() const;
    bool is_phase(BusPhase p) const { return phase() == p; }

    // Wait for REQ or ACK to reach a state. False on timeout or a bus reset,
    // which is how every transfer loop terminates when a target goes away.
    bool wait_handshake(uint32_t pin_mask, bool state);

    // Returns the number of bytes actually transferred, which is less than
    // asked for when the target changes phase early. That residual is the
    // caller's signal, not an error.
    bool last_count_known() const { return last_count_known_; }
    size_t last_confirmed_bytes() const { return last_confirmed_bytes_; }
    int send(const uint8_t *buf, int count);
    int receive(uint8_t *buf, int count);

    // One MESSAGE IN byte. Asserts ATN for any message the initiator does not
    // support, which requests a MESSAGE OUT so it can be rejected.
    int msg_in();

    void set_handshake_timeout_ms(int ms) { handshake_timeout_ms_ = ms; }

protected:
    bool last_count_known_ = true;
    size_t last_confirmed_bytes_ = 0;
    uint32_t signals_ = 0xffffffff;
    int handshake_timeout_ms_ = 3000;
};

}  // namespace scsiexec

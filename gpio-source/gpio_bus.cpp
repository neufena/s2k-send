// SPDX-License-Identifier: BSD-3-Clause
//
// Copyright (c) 2026 Eric Helgeson <eric@bluescsi.com>
//
// Adapted from SCSI2Pi cpp/buses/bus.cpp
// Copyright (C) 2022-2026 Uwe Seimet, BSD-3-Clause.

#include "gpio_bus.h"

#include <array>
#include <chrono>
#include <string>

namespace scsiexec {

namespace {

// Phase from the five signals SEL, BSY, I/O, C/D, MSG, indexed by
// (signals >> PIN_MSG) & 0b11111 so bit 0 is MSG and bit 4 is SEL. Negative
// logic, so a clear bit means the signal is asserted: SEL asserted (bit 4
// clear) is a selection regardless of anything else, and SEL and BSY both
// released is bus free.
constexpr std::array<BusPhase, 32> PHASES = {
    // 0-15: SEL asserted
    BusPhase::Selection, BusPhase::Selection, BusPhase::Selection, BusPhase::Selection,
    BusPhase::Selection, BusPhase::Selection, BusPhase::Selection, BusPhase::Selection,
    BusPhase::Selection, BusPhase::Selection, BusPhase::Selection, BusPhase::Selection,
    BusPhase::Selection, BusPhase::Selection, BusPhase::Selection, BusPhase::Selection,
    // 16-23: SEL released, BSY asserted - the information transfer phases
    BusPhase::MsgIn,    // MSG CD IO all asserted
    BusPhase::Status,   // CD IO asserted
    BusPhase::Reserved,
    BusPhase::DataIn,   // IO asserted
    BusPhase::MsgOut,   // MSG CD asserted
    BusPhase::Command,  // CD asserted
    BusPhase::Reserved,
    BusPhase::DataOut,
    // 24-31: SEL and BSY both released
    BusPhase::BusFree, BusPhase::BusFree, BusPhase::BusFree, BusPhase::BusFree,
    BusPhase::BusFree, BusPhase::BusFree, BusPhase::BusFree, BusPhase::BusFree,
};

}  // namespace

const char *pi_type_name(PiType t)
{
    switch (t) {
    case PiType::Pi1: return "Pi 1";
    case PiType::Pi2: return "Pi 2";
    case PiType::Pi3: return "Pi 3";
    case PiType::Pi4: return "Pi 4";
    default: return "unknown";
    }
}

uint32_t pi_peripheral_base(PiType t)
{
    switch (t) {
    case PiType::Pi1: return 0x20000000;
    case PiType::Pi2:
    case PiType::Pi3: return 0x3f000000;
    case PiType::Pi4: return 0xfe000000;
    default: return 0;
    }
}

PiType pi_type_from_model(const std::string &model)
{
    const std::string prefix = "Raspberry Pi ";
    if (model.rfind(prefix, 0) != 0) {
        return PiType::Unknown;
    }

    // Everything after "Raspberry Pi ". The generation, when there is one, is
    // the first token here, so it is read positionally. A substring match will
    // not do: "Raspberry Pi 3 Model B Plus" contains "Model B Plus", which is
    // how a Pi 1 spells itself.
    std::string rest = model.substr(prefix.size());

    // A "Compute Module" carries its generation after the words, and a bare
    // "Compute Module" with no number is the original BCM2835 one.
    const std::string cm = "Compute Module ";
    if (rest.rfind("Compute Module", 0) == 0) {
        rest = rest.size() > cm.size() ? rest.substr(cm.size()) : std::string();
    }

    // The Zero line is named, not numbered: "Zero 2 W" is a BCM2710, i.e. Pi 3
    // silicon, while every other Zero is Pi 1 silicon.
    if (rest.rfind("Zero 2", 0) == 0) {
        return PiType::Pi3;
    }
    if (rest.rfind("Zero", 0) == 0) {
        return PiType::Pi1;
    }

    // The leading integer, if any. No digit at all means an original Pi 1:
    // "Model B Plus", "Model A", "Compute Module Rev 1.1".
    size_t i = 0;
    while (i < rest.size() && rest[i] >= '0' && rest[i] <= '9') {
        ++i;
    }
    if (i == 0) {
        return PiType::Pi1;
    }

    const long n = std::stol(rest.substr(0, i));
    switch (n) {
    case 1: return PiType::Pi1;
    case 2: return PiType::Pi2;
    case 3: return PiType::Pi3;
    case 4:
    case 400:  // the keyboard Pi, BCM2711 like a Pi 4
        return PiType::Pi4;
    default: return PiType::Unknown;  // Pi 5 and anything newer
    }
}

const char *phase_name(BusPhase p)
{
    switch (p) {
    case BusPhase::BusFree: return "BUS FREE";
    case BusPhase::Arbitration: return "ARBITRATION";
    case BusPhase::Selection: return "SELECTION";
    case BusPhase::Reselection: return "RESELECTION";
    case BusPhase::Command: return "COMMAND";
    case BusPhase::DataIn: return "DATA IN";
    case BusPhase::DataOut: return "DATA OUT";
    case BusPhase::Status: return "STATUS";
    case BusPhase::MsgIn: return "MESSAGE IN";
    case BusPhase::MsgOut: return "MESSAGE OUT";
    default: return "RESERVED";
    }
}

IrqGuard::IrqGuard(GpioBus &bus) : bus_(bus)
{
    bus_.disable_irq();
}

IrqGuard::~IrqGuard()
{
    bus_.enable_irq();
}

BusPhase GpioBus::phase() const
{
    return PHASES[(signals_ >> PIN_MSG) & 0b11111];
}

void GpioBus::set_bsy(bool s)
{
    // BSY only. MSG, C/D, I/O and REQ belong to the target, so an initiator
    // never touches them here. On real hardware the direction pins leave them
    // as inputs and a stray write would be harmless, but it still tears down a
    // phase the target has just entered.
    set_signal(PIN_BSY, s);
}

bool GpioBus::wait_handshake(uint32_t pin_mask, bool state)
{
    acquire();
    if (get_signal(pin_mask) == state) {
        return true;
    }

    const auto start = std::chrono::steady_clock::now();
    for (;;) {
        if (get_rst()) {
            return false;
        }

        acquire();
        if (get_signal(pin_mask) == state) {
            return true;
        }

        const auto waited = std::chrono::duration_cast<std::chrono::milliseconds>(
                                std::chrono::steady_clock::now() - start)
                                .count();
        if (waited >= handshake_timeout_ms_) {
            return false;
        }
    }
}

int GpioBus::send(const uint8_t *buf, int count)
{
    last_count_known_ = true;
    last_confirmed_bytes_ = 0;
    const IrqGuard irq(*this);

    const BusPhase start_phase = phase();

    // In MESSAGE OUT, ATN must drop as the last byte goes out or the target
    // keeps asking for more messages.
    const int last_msg_out = start_phase == BusPhase::MsgOut ? count - 1 : -1;

    // A byte counts as sent once its handshake has completed, and a phase
    // change only ends the transfer early if bytes remain. Re-checking the
    // phase after the last byte would make a target that moves on promptly
    // report a complete transfer one byte short.
    int sent = 0;
    while (sent < count) {
        set_dat(buf[sent]);

        if (!wait_handshake(PIN_REQ_MASK, true) || !is_phase(start_phase)) {
            break;
        }

        if (sent == last_msg_out) {
            set_atn(false);
        }

        set_ack(true);
        const bool req = wait_handshake(PIN_REQ_MASK, false);
        set_ack(false);

        ++sent;

        if (!req) {
            last_count_known_ = false;
            break;
        }
        ++last_confirmed_bytes_;
        if (sent < count && !is_phase(start_phase)) {
            break;
        }
    }

    return sent;
}

int GpioBus::receive(uint8_t *buf, int count)
{
    last_count_known_ = true;
    last_confirmed_bytes_ = 0;
    const IrqGuard irq(*this);

    const BusPhase start_phase = phase();

    // Same counting rule as send(): the byte is in the buffer before the phase
    // is re-checked, so a target that ends the phase immediately after the last
    // byte still reports a full transfer.
    int received = 0;
    while (received < count) {
        if (!wait_handshake(PIN_REQ_MASK, true) || !is_phase(start_phase)) {
            break;
        }

        buf[received] = get_dat();

        set_ack(true);
        const bool req = wait_handshake(PIN_REQ_MASK, false);
        set_ack(false);

        ++received;

        if (!req) {
            last_count_known_ = false;
            break;
        }
        ++last_confirmed_bytes_;
        if (received < count && !is_phase(start_phase)) {
            break;
        }
    }

    return received;
}

int GpioBus::msg_in()
{
    last_count_known_ = true;
    last_confirmed_bytes_ = 0;
    const IrqGuard irq(*this);

    if (!wait_handshake(PIN_REQ_MASK, true) || !is_phase(BusPhase::MsgIn)) {
        return -1;
    }

    const int msg = get_dat();

    set_ack(true);

    // Anything that is not a completion or a reject gets ATN asserted, which
    // asks the target for a MESSAGE OUT so the initiator can reject it.
    if (msg != MSG_COMMAND_COMPLETE && msg != 0x0a /* linked */ && msg != 0x0b /* linked with flag */
        && msg != MSG_MESSAGE_REJECT) {
        set_atn(true);
    }

    last_count_known_ = wait_handshake(PIN_REQ_MASK, false);

    set_ack(false);

    return msg;
}

}  // namespace scsiexec

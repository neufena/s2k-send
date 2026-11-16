// SPDX-License-Identifier: BSD-3-Clause
//
// Copyright (c) 2026 Eric Helgeson <eric@bluescsi.com>
//
// Adapted from SCSI2Pi cpp/initiator/initiator_executor.cpp
// Copyright (C) 2023-2025 Uwe Seimet, BSD-3-Clause.

#include "gpio_initiator.h"

#include <chrono>
#include <algorithm>
#include <limits>

namespace scsiexec {

bool GpioInitiator::wait_for_free()
{
    // Up to 2 s, polled in 20 ms steps.
    for (int i = 0; i < 100; ++i) {
        bus_.wait_ns(20'000'000);
        bus_.acquire();
        if (!bus_.get_bsy() && !bus_.get_sel()) {
            return true;
        }
    }
    return false;
}

bool GpioInitiator::wait_for_busy()
{
    for (int i = 0; i < 100; ++i) {
        bus_.wait_ns(20'000'000);
        bus_.acquire();
        if (bus_.get_bsy()) {
            return true;
        }
    }
    return false;
}

bool GpioInitiator::arbitrate()
{
    if (!wait_for_free()) {
        return false;
    }

    bus_.wait_ns(BUS_FREE_NS);

    bus_.set_dat(static_cast<uint8_t>(1 << initiator_id_));
    bus_.set_bsy(true);

    bus_.wait_ns(ARBITRATION_NS);

    // A higher id on the data bus means another initiator won. Yield rather
    // than drive the bus on top of it.
    if (bus_.get_dat() > (1 << initiator_id_)) {
        return false;
    }

    bus_.set_sel(true);

    bus_.wait_ns(BUS_CLEAR_NS);
    bus_.wait_ns(BUS_SETTLE_NS);

    return true;
}

bool GpioInitiator::select(bool lun_in_cdb)
{
    bus_.set_dat(static_cast<uint8_t>((1 << initiator_id_) | (1 << target_id_)));
    bus_.set_sel(true);

    // ATN at selection is what asks the target for a MESSAGE OUT, which is how
    // IDENTIFY gets sent. Skipped when the caller put the LUN in the CDB
    // instead, so an old target that does not do messages still works.
    if (!lun_in_cdb) {
        bus_.set_atn(true);
        bus_.wait_ns(DESKEW_NS);
        bus_.wait_ns(DESKEW_NS);
    }

    bus_.set_bsy(false);
    bus_.wait_ns(BUS_SETTLE_NS);

    if (!wait_for_busy()) {
        return false;
    }

    bus_.wait_ns(DESKEW_NS);
    bus_.wait_ns(DESKEW_NS);

    bus_.set_sel(false);

    return true;
}

void GpioInitiator::reset_bus()
{
    bus_.set_rst(true);
    bus_.wait_ns(50'000);
    bus_.set_rst(false);
}

InitiatorResult GpioInitiator::execute(std::vector<uint8_t> cdb, uint8_t *buffer, size_t buflen, bool dir_out,
                                       int timeout_secs)
{
    InitiatorResult res;

    if (cdb.size() < 6) {
        res.error = "CDB has fewer than 6 bytes";
        return res;
    }
    if (target_id_ < 0) {
        res.error = "no target selected";
        return res;
    }
    if (target_id_ == initiator_id_) {
        res.error = "target id is the same as the initiator id";
        return res;
    }

    next_message_ = MSG_IDENTIFY;
    bus_.set_active(true);
    bus_.set_dir(true);

    // The caller may have deliberately put the LUN in the CDB. If so, do not
    // also send IDENTIFY, or the target would take the LUN from the message
    // and the CDB bits would be ignored.
    const bool lun_in_cdb = (cdb[1] & 0xe0) != 0;

    if (!arbitrate()) {
        bus_.reset();
        bus_.set_active(false);
        res.error = "lost arbitration or the bus never went free";
        return res;
    }

    if (!select(lun_in_cdb)) {
        bus_.reset();
        bus_.set_active(false);
        res.error = "no response from target";
        return res;
    }

    res.selected = true;

    size_t cdb_offset = 0;
    bool complete = false;
    auto last_progress = std::chrono::steady_clock::now();

    while (!complete) {
        if (std::chrono::duration_cast<std::chrono::seconds>(std::chrono::steady_clock::now() - last_progress)
                .count() >= timeout_secs) {
            if (timeout_reset_) { res.reset_issued = true; reset_bus(); }
            bus_.set_active(false);
            res.status = -1;
            res.error = "timed out waiting for the target";
            return res;
        }

        bus_.acquire();

        // Follow the direction the target asked for, or the transceivers fight.
        bus_.set_dir(!bus_.get_io());

        if (!bus_.get_req()) {
            continue;
        }

        bool progress = true;

        switch (bus_.phase()) {
        case BusPhase::Command: {
            ++res.command_phases;
            const int n = bus_.send(cdb.data() + cdb_offset, static_cast<int>(cdb.size() - cdb_offset));
            res.completion_handshakes_known &= bus_.last_count_known();
            cdb_offset += static_cast<size_t>(n);
            if (cdb_offset < cdb.size()) {
                bus_.set_active(false);
                res.status = -1;
                res.error = "target stopped accepting the CDB";
                return res;
            }
            break;
        }

        case BusPhase::Status: {
            uint8_t st = 0;
            if (bus_.receive(&st, 1) != 1) {
                bus_.set_active(false);
                res.status = -1;
                res.error = "STATUS phase failed";
                return res;
            }
            res.completion_handshakes_known &= bus_.last_count_known();
            res.status = st;
            break;
        }

        case BusPhase::DataIn: {
            if (!buflen) {
                bus_.set_active(false);
                res.status = -1;
                res.error = "target entered DATA IN but no buffer was offered";
                return res;
            }
            // DATA IN can resume after an intervening message. Keep the
            // prefix already received and offer only remaining capacity.
            if (res.transferred >= buflen) {
                bus_.set_active(false);
                res.status = -1;
                res.error = "target exceeded DATA IN buffer capacity";
                return res;
            }
            const size_t remaining = buflen - res.transferred;
            const int received = bus_.receive(buffer + res.transferred, static_cast<int>(std::min(remaining, static_cast<size_t>(std::numeric_limits<int>::max()))));
            if (received < 0 || static_cast<size_t>(received) > remaining) {
                bus_.set_active(false);
                res.status = -1;
                res.error = "invalid DATA IN receive count";
                return res;
            }
            // A short phase is not itself a command failure. Subsequent
            // phases accumulate; GOOD still does not imply a full buffer.
            res.transferred += static_cast<size_t>(received);
            res.transferred_known &= bus_.last_count_known();
            res.confirmed_bytes += bus_.last_confirmed_bytes();
            break;
        }

        case BusPhase::DataOut: {
            if (!buflen || !dir_out) {
                bus_.set_active(false);
                res.status = -1;
                res.error = "target entered DATA OUT but no data was offered";
                return res;
            }
            if (res.transferred >= buflen) {
                bus_.set_active(false);
                res.status = -1;
                res.error = "target exceeded DATA OUT buffer capacity";
                return res;
            }
            const size_t remaining = buflen - res.transferred;
            const int offered = static_cast<int>(std::min(remaining, static_cast<size_t>(std::numeric_limits<int>::max())));
            const int n = bus_.send(buffer + res.transferred, offered);
            if (n < 0 || n > offered) {
                bus_.set_active(false);
                res.status = -1;
                res.error = "invalid DATA OUT send count";
                return res;
            }
            // A phase may end before the command. Resume at the suffix, but
            // require the complete payload before accepting final GOOD.
            res.transferred += static_cast<size_t>(n);
            res.transferred_known &= bus_.last_count_known();
            res.confirmed_bytes += bus_.last_confirmed_bytes();
            break;
        }

        case BusPhase::MsgIn: {
            const int msg = bus_.msg_in();
            res.completion_handshakes_known &= bus_.last_count_known();
            if (msg < 0) {
                bus_.set_active(false);
                res.status = -1;
                res.error = "MESSAGE IN phase failed";
                return res;
            }
            // Anything the initiator does not understand was met with ATN in
            // msg_in(), so a MESSAGE OUT follows to reject it and the cycle
            // continues. Otherwise this is the end of the command.
            if (next_message_ == MSG_IDENTIFY) {
                if (msg == MSG_COMMAND_COMPLETE || msg == 0x0a || msg == 0x0b) {
                    complete = true;
                } else {
                    res.unsupported_message = true;
                    next_message_ = MSG_MESSAGE_REJECT;
                }
            }
            break;
        }

        case BusPhase::MsgOut: {
            const uint8_t msg = next_message_ == MSG_IDENTIFY
                                    ? static_cast<uint8_t>(MSG_IDENTIFY | (target_lun_ & 7))
                                    : static_cast<uint8_t>(MSG_MESSAGE_REJECT);
            const int sent = bus_.send(&msg, 1);
            // Monotonic observation: a later successful phase cannot erase an
            // incomplete IDENTIFY or MESSAGE REJECT handshake.
            res.completion_handshakes_known &= sent == 1 && bus_.last_count_known();
            next_message_ = MSG_IDENTIFY;
            break;
        }

        case BusPhase::BusFree:
            complete = true;
            break;

        default:
            progress = false;
            break;
        }

        if (progress) {
            last_progress = std::chrono::steady_clock::now();
        }
    }

    bus_.set_active(false);

    if (res.status < 0) {
        res.error = "target completed without reporting status";
    } else if (res.status == 0 && dir_out && res.transferred != buflen) {
        res.status = -1;
        res.error = "target completed with incomplete DATA OUT";
    }

    return res;
}

}  // namespace scsiexec

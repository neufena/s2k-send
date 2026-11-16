// SPDX-License-Identifier: BSD-3-Clause
//
// Copyright (c) 2026 Eric Helgeson <eric@bluescsi.com>
//
// Adapted from SCSI2Pi cpp/initiator/initiator_executor.{h,cpp}
// Copyright (C) 2023-2025 Uwe Seimet, BSD-3-Clause.

#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include "gpio_bus.h"

namespace scsiexec {

// Result of one command cycle. Kept separate from Transport::Result so this
// layer has no dependency on the rest of the tool and stays unit-testable.
struct InitiatorResult {
    int status = -1;            // SCSI status byte, or negative on failure
    bool unsupported_message = false; // Observed a message rejected by this initiator.
    bool selected = false;      // the target answered selection at all
    size_t transferred = 0;     // bytes actually moved in the data phase
    bool transferred_known = true;
    size_t confirmed_bytes = 0;
    unsigned command_phases = 0;
    bool reset_issued = false;
    bool completion_handshakes_known = true;
    std::string error;          // set when status < 0
};

// SCSI initiator driven over a GpioBus. Arbitrates, selects, then follows the
// target through whatever phases it asks for until COMMAND COMPLETE.
class GpioInitiator {
public:
    GpioInitiator(GpioBus &bus, int initiator_id) : bus_(bus), initiator_id_(initiator_id) {}

    void set_target(int id, int lun)
    {
        target_id_ = id;
        target_lun_ = lun;
    }

    // dir_out selects DATA OUT; buffer is used in whichever direction applies.
    // A short DATA IN is reported through transferred rather than treated as a
    // failure, matching what a host adapter reports as a residual.
    InitiatorResult execute(std::vector<uint8_t> cdb, uint8_t *buffer, size_t buflen, bool dir_out,
                            int timeout_secs);

    void reset_bus();
    void disable_timeout_reset() { timeout_reset_ = false; }

    // The initiator's own SCSI id, which must not collide with any target.
    int initiator_id() const { return initiator_id_; }

private:
    bool timeout_reset_ = true;
    bool arbitrate();
    bool select(bool lun_in_cdb);
    bool wait_for_free();
    bool wait_for_busy();

    GpioBus &bus_;
    int initiator_id_;
    int target_id_ = -1;
    int target_lun_ = 0;

    uint8_t next_message_ = MSG_IDENTIFY;

    // SCSI-2 bus timings, in nanoseconds.
    static constexpr long BUS_SETTLE_NS = 400;
    static constexpr long BUS_CLEAR_NS = 800;
    static constexpr long BUS_FREE_NS = 800;
    static constexpr long DESKEW_NS = 45;
    static constexpr long ARBITRATION_NS = 2400;
};

}  // namespace scsiexec

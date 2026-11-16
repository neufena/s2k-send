// SPDX-License-Identifier: BSD-3-Clause
//
// Copyright (c) 2026 Eric Helgeson <eric@bluescsi.com>
//
// Adapted from SCSI2Pi cpp/pi/rpi_bus.{h,cpp}
// Copyright (C) 2016-2020 GIMONS, Copyright (C) 2023-2026 Uwe Seimet
// BSD-3-Clause.
//
// Raspberry Pi 1 through 4. The BCM GPIO registers are mapped straight out of
// /dev/mem, which is why this needs root.
//
// HOW THE BUS IS DRIVEN. A SCSI bus is wired-OR: any device may pull a line low
// and nobody may drive one high. The Pi's pins have no open-collector mode, so
// that is emulated with the pin direction. Every signal pin's output latch is
// cleared once during setup and never touched again, and a signal is then
// driven low on the Pi side by switching the pin to an OUTPUT. Switching
// it back to INPUT relinquishes Pi-pad output drive, where it is high
// impedance at the Pi pad. Upstream RaSCSI2.3 has separate10k Pi-side
// pull-ups; cable-side termination acts through the board transceivers.
// Electrical release depends on independently verified actual-board wiring.
//
// GPIO low-output/input transitions use GPFSEL, not GPSET and GPCLR.
// Pi-side high output drive violates this driver's intended scheme; external
// transceiver behavior cannot be inferred from a GPIO mode alone.
//
// NOT SUPPORTED: Pi 5. Its GPIO is behind the RP1 southbridge over PCIe with a
// completely different register layout. Anything above a Pi 4 is refused rather
// than poking the wrong addresses, which on a Pi 5 would be someone else's
// registers.

#include <array>
#include <cerrno>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <memory>
#include <string>

#include <fcntl.h>
#include <sched.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

#include "gpio_bus.h"

namespace scsiexec {

namespace {

// BCM register indices, in 32-bit words from the start of the GPIO block.
constexpr uint32_t GPFSEL0 = 0;     // 0x00, then GPFSEL1 and GPFSEL2
constexpr uint32_t GPSET0 = 7;      // 0x1c
constexpr uint32_t GPCLR0 = 10;     // 0x28
constexpr uint32_t GPLEV0 = 13;     // 0x34
constexpr uint32_t GPPUD = 37;      // 0x94, Pi 1 to 3
constexpr uint32_t GPPUDCLK0 = 38;  // 0x98, Pi 1 to 3
constexpr uint32_t GPPUPPDN0 = 57;  // 0xe4, Pi 4

// Drive strength for GPIO 0-27, in the pads block.
constexpr uint32_t PAD_0_27 = 11;

constexpr uint32_t GPIO_OFFSET = 0x00200000;
constexpr uint32_t PADS_OFFSET = 0x00100000;

// /dev/gpiomem exposes only the GPIO block, mapped at offset zero.
constexpr size_t GPIOMEM_SIZE = 0x1000;

// Interrupt controllers, for masking a scheduler tick out of a handshake.
constexpr uint32_t IRPT_OFFSET = 0x0000b200;  // Pi 1
constexpr uint32_t QA7_OFFSET = 0x01000000;   // Pi 2 and 3 core timer
constexpr uint32_t IRPT_ENB_IRQ_1 = 4;
constexpr uint32_t IRPT_DIS_IRQ_1 = 7;
constexpr uint32_t QA7_CORE0_TINTC = 16;

// The Pi 4 masks interrupts through the GIC, which is outside the peripheral
// range and needs a mapping of its own.
constexpr off_t PI4_GICC_CTLR = 0xff842000;

// One mapping covering everything in the peripheral range this needs: the GPIO
// block, the pads block that holds drive strength, and the interrupt
// controllers. A GPIO-only window cannot reach drive strength.
constexpr size_t MEM_MAP_SIZE = QA7_OFFSET + 0x100;

constexpr uint32_t FSEL_INPUT = 0b000;
constexpr uint32_t FSEL_OUTPUT = 0b001;

// Every SCSI signal the board can drive or read. Eighteen pins: nine data
// lines with parity, and the nine control lines.
constexpr std::array<int, 18> SIGNAL_PINS = {PIN_DT0,     PIN_DT0 + 1, PIN_DT0 + 2, PIN_DT0 + 3, PIN_DT0 + 4,
                                             PIN_DT0 + 5, PIN_DT0 + 6, PIN_DT0 + 7, PIN_DP,      PIN_SEL,
                                             PIN_ATN,     PIN_RST,     PIN_ACK,     PIN_BSY,     PIN_MSG,
                                             PIN_CD,      PIN_IO,      PIN_REQ};

// DT0-DT7 and DP are GPIO 10-18, so all nine sit in GPFSEL1 and the whole data
// bus is one register write. This mask keeps the bits of GPFSEL1 that belong to
// GPIO 19 (ATN) and above.
constexpr std::array<int, 9> DATA_PINS = {PIN_DT0,     PIN_DT0 + 1, PIN_DT0 + 2, PIN_DT0 + 3, PIN_DT0 + 4,
                                          PIN_DT0 + 5, PIN_DT0 + 6, PIN_DT0 + 7, PIN_DP};
constexpr uint32_t DATA_MASK = 0b11111000000000000000000000000000;

// The board model out of the device tree. The parse itself lives in the core
// library so it can be tested without a Pi; this is only the file read.
std::string read_model(const std::string &model_path = "/proc/device-tree/model")
{
    std::ifstream f(model_path);
    std::string model;
    if (f) {
        std::getline(f, model);
    }
    return model;
}

class RpiGpioBus final : public GpioBus {
public:
    ~RpiGpioBus() override { cleanup(); }

    bool init(std::string &err) override
    {
        model_ = read_model();
        pi_ = pi_type_from_model(model_);
        if (pi_ == PiType::Unknown) {
            err = "unsupported board";
            if (!model_.empty()) {
                err += " '" + model_ + "'";
            }
            err += ": this backend drives the BCM GPIO registers directly and covers "
                   "Raspberry Pi 1 through 4 only. A Pi 5 puts its GPIO behind the RP1 "
                   "southbridge with a different register layout.";
            return false;
        }

        // /dev/mem is the whole peripheral range, so it reaches the GPIO block
        // and the pads block that holds drive strength. Root only.
        //
        // /dev/gpiomem is root:gpio 0660 and exposes the GPIO block alone at
        // offset zero. Everything here works through it except drive strength,
        // so it is the fallback.
        if (!map_dev_mem() && !map_dev_gpiomem()) {
            err = "cannot map the GPIO registers: /dev/mem needs root (" + std::string(mem_error_) +
                  ") and /dev/gpiomem was not usable either (" + gpiomem_error_ + ")";
            return false;
        }

        build_data_table();

        if (pads_) {
            // Preserve the reviewed16mA pad setting. This setting alone does not
            // establish cable-side drive margin on the actual adapter.
            set_drive_strength(7);
        } else {
            warning_ = "mapped /dev/gpiomem instead of /dev/mem, so GPIO drive strength is left at "
                       "the 8 mA default rather than raised to 16 mA. Actual adapter electrical "
                       "margin and bus release are not established by this diagnostic.";
        }

        initialize_signals();

        // The five board control pins are ordinary push-pull outputs. They are
        // not SCSI signals and take no part in the wired-OR scheme.
        for (const int p : {PIN_ACT, PIN_TAD, PIN_IND, PIN_DTD, PIN_ENB}) {
            pin_latch(p, false);
            pin_config(p, FSEL_OUTPUT);
        }

        // From here on GPFSEL is read from this copy, so asserting a signal is
        // a single store rather than a read-modify-write of a live register.
        for (uint32_t i = 0; i < 3; ++i) {
            gpfsel_[i] = gpio_[GPFSEL0 + i];
        }

        // IND high points the initiator's own signals (SEL, ATN, ACK, RST) out
        // at the bus for as long as this is an initiator. TAD stays low so the
        // target's signals read back in, and rises only while this initiator is
        // itself asserting BSY to arbitrate.
        pin_latch(PIN_IND, true);

        // Data transceiver pointing outwards, which is where a command starts.
        // Same thing set_dir(true) does; the latches are already low from
        // initialize_signals().
        pin_latch(PIN_DTD, false);

        // Preserve the FULLSPEC legacy ENB output level. On upstream RaSCSI
        // 2.3 BCM5 drives the debug LED, not the transceiver enable inputs.
        pin_latch(PIN_ENB, true);

        return true;
    }

    bool cleanup_verified() override
    {
        if (!base_) return false;
        register_state_verified_ = false;
        cleanup();
        return register_state_verified_;
    }

    void cleanup() override
    {
        if (!base_) {
            return;
        }

        release_all();

        pin_latch(PIN_ENB, false);
        for (const int p : {PIN_ACT, PIN_TAD, PIN_IND, PIN_DTD}) {
            pin_latch(p, false);
            pin_config(p, FSEL_INPUT);
        }

        initialize_signals();
        // Read back direction registers and legacy ENB level BEFORE unmapping.
        // This proves register state, not external wiring/electrical correctness.
        __sync_synchronize();
        bool verified = (gpio_[GPLEV0] & (1u << PIN_ENB)) == 0;
        for (const int p : SIGNAL_PINS) {
            verified = verified && (((gpio_[GPFSEL0 + p / 10] >> ((p % 10) * 3)) & 7u) == FSEL_INPUT);
        }
        for (const int p : {PIN_ACT, PIN_TAD, PIN_IND, PIN_DTD}) {
            if (p >= 0) verified = verified && (((gpio_[GPFSEL0 + p / 10] >> ((p % 10) * 3)) & 7u) == FSEL_INPUT);
        }
        register_state_verified_ = verified;
        set_drive_strength(3);  // back to the 8 mA default

        enable_irq();

        if (gicc_pmr_) {
            munmap(const_cast<uint32_t *>(gicc_pmr_ - 1), 8);
            gicc_pmr_ = nullptr;
        }

        munmap(const_cast<uint32_t *>(base_), map_size_);
        base_ = nullptr;
        gpio_ = nullptr;
        pads_ = nullptr;
    }

    void reset() override
    {
        release_all();
        pin_latch(PIN_ACT, false);
        pin_latch(PIN_TAD, false);
    }

    void acquire() override
    {
        // One read of the level register gives one consistent view of every
        // signal, so the phase decode never sees pins sampled at different
        // instants.
        signals_ = gpio_[GPLEV0];
    }

    // Set data bits become outputs and drive the low sitting in their latch;
    // clear ones go high impedance. One store covers all nine pins.
    void set_dat(uint8_t v) override
    {
        const uint32_t fsel = (gpfsel_[1] & DATA_MASK) | dat_fsel_[v];
        gpfsel_[1] = fsel;
        gpio_[GPFSEL0 + 1] = fsel;
    }

    uint8_t get_dat() override
    {
        wait_ns(BUS_SETTLE_NS);
        acquire();
        // Negative logic: a low line is a one.
        return static_cast<uint8_t>(~(signals_ >> PIN_DT0));
    }

    // Latches only. The data pins' own direction belongs to set_dat(); change
    // it here and the transceiver never turns round for DATA IN.
    void set_dir(bool out) override
    {
        pin_latch(PIN_DTD, !out);
        for (const int p : DATA_PINS) {
            pin_latch(p, !out);
        }
    }

    void set_active(bool on) override { pin_latch(PIN_ACT, on); }

    void set_signal(int pin, bool asserted) override
    {
        const uint32_t index = static_cast<uint32_t>(pin) / 10;
        const uint32_t shift = (static_cast<uint32_t>(pin) % 10) * 3;

        uint32_t v = gpfsel_[index];
        if (asserted) {
            v |= FSEL_OUTPUT << shift;
        } else {
            v &= ~(0b111u << shift);
        }

        gpfsel_[index] = v;
        gpio_[GPFSEL0 + index] = v;
    }

    // Asserting BSY is this initiator arbitrating, so the target-signal
    // transceivers have to turn around to let it out - and turn back when it is
    // released and the target owns BSY again.
    void set_bsy(bool asserted) override
    {
        GpioBus::set_bsy(asserted);
        pin_latch(PIN_ACT, asserted);
        pin_latch(PIN_TAD, asserted);
    }

    void set_sel(bool asserted) override
    {
        GpioBus::set_sel(asserted);
        pin_latch(PIN_ACT, asserted);
    }

    // Busy-wait. The delays that matter here are hundreds of nanoseconds and
    // nanosleep cannot do those - it rounds up to a scheduler tick, so a 400 ns
    // bus settle really takes tens of microseconds. The ARM free running timer
    // would need /dev/vcio to learn the core clock; CLOCK_MONOTONIC needs no
    // privilege and is read through the vDSO.
    void wait_ns(long ns) override { spin_ns(ns); }

    std::string describe() const override
    {
        char buf[64];
        snprintf(buf, sizeof(buf), "0x%08x", pi_peripheral_base(pi_));
        return (model_.empty() ? std::string("unknown board") : model_) + " (" + pi_type_name(pi_) +
               ", peripheral base " + buf + ", via " + (pads_ ? "/dev/mem" : "/dev/gpiomem") + ")";
    }

    std::string warning() const override { return warning_; }

    void set_irq_mask(bool on) override { irq_mask_enabled_ = on; }

    // Masking interrupts keeps a scheduler tick from landing between REQ and
    // ACK. Without it a loaded Pi can see a transfer time out mid-phase.
    //
    // It needs the interrupt controller, which is only in the /dev/mem mapping,
    // so an unprivileged run simply does not get it. That is a timing margin,
    // not a correctness difference.
    void disable_irq() override
    {
        if (!irq_maskable()) {
            return;
        }

        switch (pi_) {
        case PiType::Pi1:
            irpt_saved_ = irp_ctl_[IRPT_ENB_IRQ_1];
            irp_ctl_[IRPT_DIS_IRQ_1] = irpt_saved_ & 0xf;
            break;
        case PiType::Pi2:
        case PiType::Pi3:
            // Per core, and the thread can be moved between calls, so the core
            // that gets re-enabled has to be the one that was masked.
            tint_core_ = static_cast<uint32_t>(sched_getcpu()) + QA7_CORE0_TINTC;
            tint_saved_ = qa7_[tint_core_];
            qa7_[tint_core_] = 0;
            break;
        case PiType::Pi4:
            gicc_saved_ = *gicc_pmr_;
            *gicc_pmr_ = 0;
            break;
        default:
            break;
        }
        masked_ = true;
    }

    void enable_irq() override
    {
        if (!masked_) {
            return;
        }
        masked_ = false;

        switch (pi_) {
        case PiType::Pi1: irp_ctl_[IRPT_ENB_IRQ_1] = irpt_saved_ & 0xf; break;
        case PiType::Pi2:
        case PiType::Pi3: qa7_[tint_core_] = tint_saved_; break;
        case PiType::Pi4: *gicc_pmr_ = gicc_saved_; break;
        default: break;
        }
    }

    // The self test needs to read registers back, not just write them: an mmap
    // of a wrong-but-mapped address accepts every write and returns garbage.
    bool raw_gpio() const override { return gpio_ != nullptr; }

    void raw_set_mode(int pin, bool output) override { pin_config(pin, output ? FSEL_OUTPUT : FSEL_INPUT); }

    int raw_get_mode(int pin) const override
    {
        if (!gpio_ || pin < 0) {
            return -1;
        }
        const uint32_t reg = GPFSEL0 + static_cast<uint32_t>(pin) / 10;
        const uint32_t shift = (static_cast<uint32_t>(pin) % 10) * 3;
        return static_cast<int>((gpio_[reg] >> shift) & 7u);
    }

    void raw_set_level(int pin, bool high) override { pin_latch(pin, high); }

    int raw_get_level(int pin) const override
    {
        if (!gpio_ || pin < 0) {
            return -1;
        }
        return static_cast<int>((gpio_[GPLEV0] >> pin) & 1u);
    }

private:
    static constexpr long BUS_SETTLE_NS = 400;

    bool irq_maskable() const
    {
        if (masked_ || !irq_mask_enabled_) {
            return false;
        }
        switch (pi_) {
        case PiType::Pi1: return irp_ctl_ != nullptr;
        case PiType::Pi2:
        case PiType::Pi3: return qa7_ != nullptr;
        case PiType::Pi4: return gicc_pmr_ != nullptr;
        default: return false;
        }
    }

    // The whole peripheral range: GPIO plus the pads block. Needs root.
    bool map_dev_mem()
    {
        const int fd = open("/dev/mem", O_RDWR | O_SYNC);
        if (fd < 0) {
            mem_error_ = strerror(errno);
            return false;
        }

        void *map = mmap(nullptr, MEM_MAP_SIZE, PROT_READ | PROT_WRITE, MAP_SHARED, fd,
                         static_cast<off_t>(pi_peripheral_base(pi_)));
        if (map == MAP_FAILED) {
            mem_error_ = strerror(errno);
            close(fd);
            return false;
        }

        base_ = static_cast<volatile uint32_t *>(map);
        map_size_ = MEM_MAP_SIZE;
        gpio_ = base_ + GPIO_OFFSET / sizeof(uint32_t);
        pads_ = base_ + PADS_OFFSET / sizeof(uint32_t);
        irp_ctl_ = base_ + IRPT_OFFSET / sizeof(uint32_t);
        qa7_ = base_ + QA7_OFFSET / sizeof(uint32_t);

        // The Pi 4's GIC is not in the peripheral range.
        if (pi_ == PiType::Pi4) {
            void *gic = mmap(nullptr, 8, PROT_READ | PROT_WRITE, MAP_SHARED, fd, PI4_GICC_CTLR);
            if (gic != MAP_FAILED) {
                // The priority mask register is at offset 1.
                gicc_pmr_ = static_cast<volatile uint32_t *>(gic) + 1;
            }
        }

        close(fd);
        return true;
    }

    // The GPIO block on its own, at offset zero, with no pads block behind it.
    // Openable by the gpio group, so this path needs no privilege at all.
    bool map_dev_gpiomem()
    {
        const int fd = open("/dev/gpiomem", O_RDWR | O_SYNC);
        if (fd < 0) {
            gpiomem_error_ = strerror(errno);
            return false;
        }

        void *map = mmap(nullptr, GPIOMEM_SIZE, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
        close(fd);

        if (map == MAP_FAILED) {
            gpiomem_error_ = strerror(errno);
            return false;
        }

        base_ = static_cast<volatile uint32_t *>(map);
        map_size_ = GPIOMEM_SIZE;
        gpio_ = base_;
        pads_ = nullptr;  // not in this mapping, so no drive strength
        return true;
    }

    static void spin_ns(long ns)
    {
        if (ns <= 0) {
            return;
        }

        timespec now;
        clock_gettime(CLOCK_MONOTONIC, &now);
        const long long deadline = static_cast<long long>(now.tv_sec) * 1'000'000'000LL + now.tv_nsec + ns;

        for (;;) {
            clock_gettime(CLOCK_MONOTONIC, &now);
            if (static_cast<long long>(now.tv_sec) * 1'000'000'000LL + now.tv_nsec >= deadline) {
                return;
            }
        }
    }

    // The output latch. For a signal pin this is the low that gets driven when
    // the pin is switched to an output; for a control pin it is the pin level.
    void pin_latch(int pin, bool high) const
    {
        if (pin < 0) {
            return;
        }
        gpio_[high ? GPSET0 : GPCLR0] = 1u << pin;
    }

    // Direction, written straight to the hardware. Used during setup and by the
    // self test; everything on the hot path goes through the GPFSEL copy.
    void pin_config(int pin, uint32_t mode) const
    {
        if (pin < 0) {
            return;
        }
        const uint32_t reg = GPFSEL0 + static_cast<uint32_t>(pin) / 10;
        const uint32_t shift = (static_cast<uint32_t>(pin) % 10) * 3;
        gpio_[reg] = (gpio_[reg] & ~(0b111u << shift)) | (mode << shift);
    }

    // Preserve the driver's no-internal-pull setting. Upstream2.3 provides
    // external10k Pi-side pull-ups; cable-side termination is separate.
    // Actual-board pull behavior is not established by this register write.
    void clear_pull(int pin) const
    {
        if (pin < 0) {
            return;
        }

        if (pi_ == PiType::Pi4) {
            const uint32_t reg = GPPUPPDN0 + static_cast<uint32_t>(pin) / 16;
            const uint32_t shift = (static_cast<uint32_t>(pin) % 16) * 2;
            gpio_[reg] &= ~(3u << shift);
            return;
        }

        gpio_[GPPUD] = 0;
        spin_ns(2000);
        gpio_[GPPUDCLK0] = 1u << pin;
        spin_ns(2000);
        gpio_[GPPUD] = 0;
        gpio_[GPPUDCLK0] = 0;
    }

    // Configure each SCSI-associated GPIO as input with a low output latch
    // for the next output-mode assertion. This is GPIO configuration, not
    // an independent measurement of electrical bus release.
    void initialize_signals() const
    {
        for (const int p : SIGNAL_PINS) {
            pin_latch(p, false);
            pin_config(p, FSEL_INPUT);
            clear_pull(p);
        }
    }

    bool register_state_verified_ = false;

    void release_all()
    {
        for (const int p : SIGNAL_PINS) {
            set_signal(p, false);
        }
    }

    void set_drive_strength(uint32_t drive) const
    {
        if (!pads_) {
            return;
        }
        // 0x5a is the write password; without it the write is ignored.
        pads_[PAD_0_27] = (pads_[PAD_0_27] & 0xfffffff8) | drive | 0x5a000000;
    }

    // The GPFSEL1 value for each data byte: a set bit becomes an output, a
    // clear bit an input. Bit 8 is odd parity across the eight data bits, so
    // the number of lines pulled low is always odd.
    void build_data_table()
    {
        for (uint32_t v = 0; v < 256; ++v) {
            uint32_t odd = 0;
            for (int i = 0; i < 8; ++i) {
                odd ^= (v >> i) & 1;
            }

            uint32_t bits = v | ((odd ? 0u : 1u) << 8);

            uint32_t fsel = 0;
            for (const int pin : DATA_PINS) {
                if (bits & 1) {
                    fsel |= FSEL_OUTPUT << ((static_cast<uint32_t>(pin) % 10) * 3);
                }
                bits >>= 1;
            }

            dat_fsel_[v] = fsel;
        }
    }

    volatile uint32_t *base_ = nullptr;
    volatile uint32_t *gpio_ = nullptr;
    volatile uint32_t *pads_ = nullptr;  // null when mapped through /dev/gpiomem
    size_t map_size_ = 0;

    // Interrupt masking. All null on the /dev/gpiomem path, which simply does
    // not mask; irq_mask_enabled_ is the --no-irq-mask escape hatch.
    volatile uint32_t *irp_ctl_ = nullptr;
    volatile uint32_t *qa7_ = nullptr;
    volatile uint32_t *gicc_pmr_ = nullptr;
    bool irq_mask_enabled_ = true;
    bool masked_ = false;
    uint32_t irpt_saved_ = 0;
    uint32_t tint_saved_ = 0;
    uint32_t tint_core_ = QA7_CORE0_TINTC;
    uint32_t gicc_saved_ = 0;
    std::string mem_error_ = "not tried";
    std::string gpiomem_error_ = "not tried";
    std::string warning_;
    PiType pi_ = PiType::Unknown;
    std::string model_;

    // Copy of GPFSEL0-2. Reading these back from the hardware on every signal
    // change is both slower and a read of a register the bus is changing.
    std::array<uint32_t, 3> gpfsel_ = {};

    std::array<uint32_t, 256> dat_fsel_ = {};
};

}  // namespace

std::unique_ptr<GpioBus> make_rpi_bus()
{
    return std::make_unique<RpiGpioBus>();
}

}  // namespace scsiexec

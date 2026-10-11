#include "eeg_drdy_health.h"

#include <stddef.h>

eeg_drdy_health_state_t eeg_drdy_health_classify(const eeg_drdy_health_input_t *input)
{
    if (input == NULL) {
        return EEG_DRDY_HEALTH_UNAVAILABLE;
    }
    if (input->paused) {
        return EEG_DRDY_HEALTH_PAUSED;
    }
    if (!input->hardware_available || input->window_us == 0U || input->expected_rate_hz == 0U) {
        return EEG_DRDY_HEALTH_UNAVAILABLE;
    }

    const uint64_t expected_edges =
        ((uint64_t)input->expected_rate_hz * input->window_us + 500000ULL) / 1000000ULL;
    if (expected_edges == 0U) {
        return EEG_DRDY_HEALTH_UNAVAILABLE;
    }

    const uint64_t hardware_percent = (uint64_t)input->hardware_edges * 100ULL;
    if (hardware_percent < expected_edges * 90ULL ||
        hardware_percent > expected_edges * 110ULL) {
        return EEG_DRDY_HEALTH_DRDY_RATE;
    }

    if ((uint64_t)input->isr_edges * 100ULL <
        (uint64_t)input->hardware_edges * 95ULL) {
        return EEG_DRDY_HEALTH_IRQ_LOSS;
    }

    if ((uint64_t)input->acquired_samples * 100ULL <
        (uint64_t)input->isr_edges * 95ULL) {
        return EEG_DRDY_HEALTH_ACQ_STALL;
    }

    if ((uint64_t)input->acquired_samples * 100ULL >
            (uint64_t)input->isr_edges * 105ULL ||
        input->low_level_recovery > 0U || input->reinit_count > 0U) {
        return EEG_DRDY_HEALTH_RECOVERY_READ;
    }

    return EEG_DRDY_HEALTH_OK;
}

const char *eeg_drdy_health_state_name(eeg_drdy_health_state_t state)
{
    switch (state) {
    case EEG_DRDY_HEALTH_OK:
        return "OK";
    case EEG_DRDY_HEALTH_PAUSED:
        return "PAUSED";
    case EEG_DRDY_HEALTH_UNAVAILABLE:
        return "UNAVAILABLE";
    case EEG_DRDY_HEALTH_DRDY_RATE:
        return "DRDY_RATE";
    case EEG_DRDY_HEALTH_IRQ_LOSS:
        return "IRQ_LOSS";
    case EEG_DRDY_HEALTH_ACQ_STALL:
        return "ACQ_STALL";
    case EEG_DRDY_HEALTH_RECOVERY_READ:
        return "RECOVERY_READ";
    default:
        return "UNKNOWN";
    }
}

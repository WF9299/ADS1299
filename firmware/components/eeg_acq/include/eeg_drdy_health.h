#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    EEG_DRDY_HEALTH_OK = 0,
    EEG_DRDY_HEALTH_PAUSED,
    EEG_DRDY_HEALTH_UNAVAILABLE,
    EEG_DRDY_HEALTH_DRDY_RATE,
    EEG_DRDY_HEALTH_IRQ_LOSS,
    EEG_DRDY_HEALTH_ACQ_STALL,
    EEG_DRDY_HEALTH_RECOVERY_READ,
} eeg_drdy_health_state_t;

typedef struct {
    uint32_t hardware_edges;
    uint32_t isr_edges;
    uint32_t acquired_samples;
    uint32_t low_level_recovery;
    uint32_t reinit_count;
    uint64_t window_us;
    uint32_t expected_rate_hz;
    bool hardware_available;
    bool paused;
} eeg_drdy_health_input_t;

eeg_drdy_health_state_t eeg_drdy_health_classify(const eeg_drdy_health_input_t *input);
const char *eeg_drdy_health_state_name(eeg_drdy_health_state_t state);

#ifdef __cplusplus
}
#endif

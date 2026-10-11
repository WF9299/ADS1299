#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "ads1299.h"
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

#define EEG_ACQ_CHANNEL_COUNT 8
#define EEG_ACQ_CONFIG_NORMAL 1U
#define EEG_ACQ_CONFIG_INTERNAL_SHORT 2U
#define EEG_ACQ_MODE_SETTLE_SAMPLES 200U

typedef enum {
    EEG_ACQ_MODE_SWITCH_OK = 0,
    EEG_ACQ_MODE_SWITCH_INVALID_REQUEST = 2,
    EEG_ACQ_MODE_SWITCH_PAUSE_TIMEOUT = 4,
    EEG_ACQ_MODE_SWITCH_CONFIG_FAILED = 5,
    EEG_ACQ_MODE_SWITCH_RESTORE_FAILED = 6,
    EEG_ACQ_MODE_SWITCH_INTERNAL_ERROR = 7,
} eeg_acq_mode_switch_status_t;

typedef enum {
    EEG_ACQ_BIAS_SWITCH_OK = 0,
    EEG_ACQ_BIAS_SWITCH_INVALID_REQUEST = 2,
    EEG_ACQ_BIAS_SWITCH_PAUSE_TIMEOUT = 3,
    EEG_ACQ_BIAS_SWITCH_APPLY_FAILED = 4,
    EEG_ACQ_BIAS_SWITCH_RESTORE_FAILED = 5,
    EEG_ACQ_BIAS_SWITCH_INTERNAL_ERROR = 6,
} eeg_acq_bias_switch_status_t;

typedef enum {
    EEG_ACQ_IMPEDANCE_STATUS_OK = 0,
    EEG_ACQ_IMPEDANCE_STATUS_BUSY = 1,
    EEG_ACQ_IMPEDANCE_STATUS_INVALID_REQUEST = 2,
    EEG_ACQ_IMPEDANCE_STATUS_PAUSE_TIMEOUT = 3,
    EEG_ACQ_IMPEDANCE_STATUS_CONFIG_FAILED = 4,
    EEG_ACQ_IMPEDANCE_STATUS_SAMPLE_TIMEOUT = 5,
    EEG_ACQ_IMPEDANCE_STATUS_RESTORE_FAILED = 6,
    EEG_ACQ_IMPEDANCE_STATUS_INTERNAL_ERROR = 7,
} eeg_acq_impedance_status_t;

enum {
    EEG_ACQ_IMPEDANCE_FLAG_DC_VALID = 1U << 0,
    EEG_ACQ_IMPEDANCE_FLAG_AC_VALID = 1U << 1,
    EEG_ACQ_IMPEDANCE_FLAG_BACKGROUND_SUBTRACTED = 1U << 2,
    EEG_ACQ_IMPEDANCE_FLAG_CLIPPED = 1U << 3,
};

typedef struct {
    eeg_acq_impedance_status_t status;
    uint8_t tested_mask;
    uint8_t open_mask;
    uint8_t flags;
    uint16_t duration_ms;
    uint16_t sample_count;
    uint32_t nominal_ohm[EEG_ACQ_CHANNEL_COUNT];
    uint32_t excitation_nv_peak[EEG_ACQ_CHANNEL_COUNT];
} eeg_acq_impedance_result_t;

typedef struct {
    uint32_t seq;
    uint64_t timestamp_us;
    int32_t channel[EEG_ACQ_CHANNEL_COUNT];
} eeg_acq_sample_t;

typedef struct {
    uint32_t drdy_count;
    uint32_t acquired_count;
    uint32_t missed_drdy;
    uint32_t spi_fail;
    uint32_t ring_drop;
    uint32_t low_level_recovery;
    uint32_t reinit_count;
    uint32_t buffered;
} eeg_acq_stats_t;

esp_err_t eeg_acq_start(const board_config_t *board);
esp_err_t eeg_acq_pause_ads1299_stream(void);
esp_err_t eeg_acq_resume_ads1299_stream(void);
bool eeg_acq_pop(eeg_acq_sample_t *sample);
void eeg_acq_clear_buffer(void);
void eeg_acq_get_stats(eeg_acq_stats_t *stats);
bool eeg_acq_is_running(void);
uint8_t eeg_acq_get_config_id(void);
eeg_acq_mode_switch_status_t eeg_acq_set_config_id(uint8_t target_config_id);
uint8_t eeg_acq_get_normal_bias_mask(void);
bool eeg_acq_normal_bias_is_verified(void);
bool eeg_acq_is_settling(void);
eeg_acq_bias_switch_status_t eeg_acq_set_normal_bias_mask(uint8_t target_mask);
esp_err_t eeg_acq_measure_impedance(uint8_t channel_mask,
                                    eeg_acq_impedance_result_t *result);

#ifdef __cplusplus
}
#endif

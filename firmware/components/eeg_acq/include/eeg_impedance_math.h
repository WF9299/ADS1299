#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define EEG_IMPEDANCE_CHANNELS 8U
#define EEG_IMPEDANCE_FREQUENCY_HZ 31.25
#define EEG_IMPEDANCE_VREF_V 4.5
#define EEG_IMPEDANCE_PGA_GAIN 12.0
#define EEG_IMPEDANCE_CURRENT_A 24.0e-9

typedef struct {
    double in_phase[EEG_IMPEDANCE_CHANNELS];
    double quadrature[EEG_IMPEDANCE_CHANNELS];
    uint32_t sample_count;
    uint8_t clipped_mask;
} eeg_impedance_accumulator_t;

typedef struct {
    double nominal_ohm[EEG_IMPEDANCE_CHANNELS];
    double voltage_peak_v[EEG_IMPEDANCE_CHANNELS];
    uint8_t valid_mask;
    uint8_t clipped_mask;
} eeg_impedance_result_t;

void eeg_impedance_accumulator_reset(eeg_impedance_accumulator_t *accumulator);
void eeg_impedance_accumulator_add(eeg_impedance_accumulator_t *accumulator,
                                   uint64_t timestamp_us,
                                   const int32_t channel[EEG_IMPEDANCE_CHANNELS]);
bool eeg_impedance_calculate(const eeg_impedance_accumulator_t *background,
                             const eeg_impedance_accumulator_t *excited,
                             uint8_t channel_mask,
                             eeg_impedance_result_t *result);

#ifdef __cplusplus
}
#endif

#include "eeg_impedance_math.h"

#include <limits.h>
#include <math.h>
#include <string.h>

#define EEG_IMPEDANCE_TWO_PI 6.283185307179586476925286766559
#define EEG_IMPEDANCE_PI 3.1415926535897932384626433832795
#define EEG_IMPEDANCE_PERIOD_US 32000ULL
#define EEG_IMPEDANCE_ADC_POSITIVE_FULL_SCALE 8388607.0

void eeg_impedance_accumulator_reset(eeg_impedance_accumulator_t *accumulator)
{
    if (accumulator != NULL) {
        memset(accumulator, 0, sizeof(*accumulator));
    }
}

void eeg_impedance_accumulator_add(eeg_impedance_accumulator_t *accumulator,
                                   uint64_t timestamp_us,
                                   const int32_t channel[EEG_IMPEDANCE_CHANNELS])
{
    if (accumulator == NULL || channel == NULL || accumulator->sample_count == UINT32_MAX) {
        return;
    }

    const uint64_t phase_us = timestamp_us % EEG_IMPEDANCE_PERIOD_US;
    const double phase = EEG_IMPEDANCE_TWO_PI * (double)phase_us /
                         (double)EEG_IMPEDANCE_PERIOD_US;
    const double cosine = cos(phase);
    const double sine = sin(phase);
    for (uint8_t index = 0; index < EEG_IMPEDANCE_CHANNELS; ++index) {
        const int32_t code = channel[index];
        accumulator->in_phase[index] += (double)code * cosine;
        accumulator->quadrature[index] += (double)code * sine;
        if (code <= -8388608 || code >= 8388607) {
            accumulator->clipped_mask |= (uint8_t)(1U << index);
        }
    }
    accumulator->sample_count++;
}

bool eeg_impedance_calculate(const eeg_impedance_accumulator_t *background,
                             const eeg_impedance_accumulator_t *excited,
                             uint8_t channel_mask,
                             eeg_impedance_result_t *result)
{
    if (background == NULL || excited == NULL || result == NULL || channel_mask == 0U ||
        background->sample_count == 0U || excited->sample_count == 0U) {
        return false;
    }

    memset(result, 0, sizeof(*result));
    result->clipped_mask = (uint8_t)((background->clipped_mask | excited->clipped_mask) &
                                     channel_mask);
    const double code_to_volts = EEG_IMPEDANCE_VREF_V /
                                 (EEG_IMPEDANCE_PGA_GAIN *
                                  EEG_IMPEDANCE_ADC_POSITIVE_FULL_SCALE);
    const double current_fundamental_peak = 4.0 * EEG_IMPEDANCE_CURRENT_A /
                                            EEG_IMPEDANCE_PI;

    for (uint8_t index = 0; index < EEG_IMPEDANCE_CHANNELS; ++index) {
        const uint8_t bit = (uint8_t)(1U << index);
        if ((channel_mask & bit) == 0U) {
            continue;
        }

        const double background_i = 2.0 * background->in_phase[index] /
                                    (double)background->sample_count;
        const double background_q = 2.0 * background->quadrature[index] /
                                    (double)background->sample_count;
        const double excited_i = 2.0 * excited->in_phase[index] /
                                 (double)excited->sample_count;
        const double excited_q = 2.0 * excited->quadrature[index] /
                                 (double)excited->sample_count;
        const double code_peak = hypot(excited_i - background_i,
                                       excited_q - background_q);
        const double voltage_peak = code_peak * code_to_volts;
        const double nominal_ohm = voltage_peak / current_fundamental_peak;
        if (!isfinite(voltage_peak) || !isfinite(nominal_ohm) ||
            voltage_peak < 0.0 || nominal_ohm < 0.0) {
            continue;
        }
        result->voltage_peak_v[index] = voltage_peak;
        result->nominal_ohm[index] = nominal_ohm;
        result->valid_mask |= bit;
    }
    return result->valid_mask != 0U;
}

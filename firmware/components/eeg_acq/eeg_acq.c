#include "eeg_acq.h"

#include <inttypes.h>
#include <limits.h>
#include <math.h>
#include <string.h>

#include "driver/gpio.h"
#include "eeg_drdy_health.h"
#include "eeg_drdy_monitor.h"
#include "eeg_impedance_math.h"
#include "esp_check.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "sdkconfig.h"
#include "app_config.h"

static const char *TAG = "eeg_acq";

#define EEG_ACQ_RING_SIZE P300_EEG_ACQ_RING_SIZE
#define EEG_ACQ_WAIT_TIMEOUT_MS 5
#define EEG_ACQ_RESTART_TIMEOUT_MS 50
#define EEG_ACQ_MAX_LOW_DRDY_READS 4
#define EEG_ACQ_STATS_INTERVAL_US (P300_EEG_ACQ_STATS_INTERVAL_MS * 1000LL)

#if P300_ADS1299_INTERNAL_TEST
#define ADS1299_SELF_TEST_WARMUP_SAMPLES 200U
#define ADS1299_SELF_TEST_ANALYSIS_SAMPLES 2048U
#define ADS1299_SELF_TEST_MIN_P2P_CODES 50000LL
#define ADS1299_SELF_TEST_MAX_P2P_CODES 500000LL
#define ADS1299_SELF_TEST_MIN_TRANSITIONS 4U
#define ADS1299_SELF_TEST_MAX_TRANSITIONS 16U

typedef struct {
    int32_t min[8];
    int32_t max[8];
    bool previous_positive[8];
    uint16_t transitions[8];
    uint32_t samples;
    uint32_t bad_status;
    bool complete;
} ads1299_self_test_t;
#endif

static const board_config_t *s_board;
static board_config_t s_board_storage;
static TaskHandle_t s_task;
static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;
static eeg_acq_sample_t *s_ring;
static const char *s_ring_mem_name = "none";
static volatile uint16_t s_write_index;
static volatile uint16_t s_read_index;
static volatile uint32_t s_drdy_isr_count;
static eeg_acq_stats_t s_stats;
static uint32_t s_next_seq;
static int64_t s_last_acq_us;
static int64_t s_last_stats_log_us;
static int64_t s_health_window_start_us;
static uint32_t s_health_last_isr_count;
static uint32_t s_health_last_acquired_count;
static uint32_t s_health_last_low_recovery;
static uint32_t s_health_last_reinit_count;
static bool s_first_frame_logged;
static bool s_running;
static volatile bool s_pause_requested;
static volatile bool s_paused;
static volatile bool s_health_window_suppressed;
static volatile bool s_health_reset_requested;
static volatile TaskHandle_t s_measurement_task;
static uint32_t s_bad_status_frames;
static volatile uint8_t s_config_id = EEG_ACQ_CONFIG_NORMAL;
static volatile uint16_t s_discard_samples;
static volatile uint8_t s_normal_bias_mask = P300_ADS_CHANNEL_MASK;
static volatile bool s_normal_bias_verified;
#if P300_ADS1299_INTERNAL_TEST
static ads1299_self_test_t s_self_test;
#endif

static esp_err_t apply_ads1299_capture_config(void)
{
    return s_config_id == EEG_ACQ_CONFIG_INTERNAL_SHORT
               ? ads1299_apply_internal_short_config()
               : ads1299_apply_verified_reference_config_with_bias(s_normal_bias_mask);
}

#if P300_ADS1299_INTERNAL_TEST
static void reset_ads1299_self_test(void)
{
    memset(&s_self_test, 0, sizeof(s_self_test));
    for (uint8_t ch = 0; ch < 8; ++ch) {
        s_self_test.min[ch] = INT32_MAX;
        s_self_test.max[ch] = INT32_MIN;
    }
}

static void consume_ads1299_self_test_frame(const ads1299_frame_t *frame, uint32_t sequence)
{
    if (frame == NULL || s_self_test.complete || sequence < ADS1299_SELF_TEST_WARMUP_SAMPLES) {
        return;
    }

    if ((frame->status[0] & 0xF0U) != 0xC0U) {
        s_self_test.bad_status++;
    }

    for (uint8_t ch = 0; ch < 8; ++ch) {
        const int32_t value = frame->channel[ch];
        const bool positive = value >= 0;
        if (value < s_self_test.min[ch]) {
            s_self_test.min[ch] = value;
        }
        if (value > s_self_test.max[ch]) {
            s_self_test.max[ch] = value;
        }
        if (s_self_test.samples > 0 && positive != s_self_test.previous_positive[ch]) {
            s_self_test.transitions[ch]++;
        }
        s_self_test.previous_positive[ch] = positive;
    }

    s_self_test.samples++;
    if (s_self_test.samples < ADS1299_SELF_TEST_ANALYSIS_SAMPLES) {
        return;
    }

    bool overall_pass = s_self_test.bad_status == 0;
    ESP_LOGI(TAG,
             "================ ADS1299 INTERNAL SELF-TEST ================");
    ESP_LOGI(TAG,
             "source=internal-square frequency=1.953125Hz gain=12 samples=%" PRIu32
             " bad_status=%" PRIu32,
             s_self_test.samples,
             s_self_test.bad_status);

    for (uint8_t ch = 0; ch < 8; ++ch) {
        const int64_t p2p = (int64_t)s_self_test.max[ch] - (int64_t)s_self_test.min[ch];
        const bool channel_pass =
            s_self_test.min[ch] < 0 &&
            s_self_test.max[ch] > 0 &&
            p2p >= ADS1299_SELF_TEST_MIN_P2P_CODES &&
            p2p <= ADS1299_SELF_TEST_MAX_P2P_CODES &&
            s_self_test.transitions[ch] >= ADS1299_SELF_TEST_MIN_TRANSITIONS &&
            s_self_test.transitions[ch] <= ADS1299_SELF_TEST_MAX_TRANSITIONS;
        overall_pass = overall_pass && channel_pass;
        ESP_LOGI(TAG,
                 "SELF-TEST CH%u: min=%" PRId32 " max=%" PRId32 " p2p=%" PRId64
                 " transitions=%u => %s",
                 (unsigned)(ch + 1),
                 s_self_test.min[ch],
                 s_self_test.max[ch],
                 p2p,
                 (unsigned)s_self_test.transitions[ch],
                 channel_pass ? "PASS" : "FAIL");
    }

    if (overall_pass) {
        ESP_LOGI(TAG, "ADS1299 SELF-TEST RESULT: PASS");
    } else {
        ESP_LOGE(TAG,
                 "ADS1299 SELF-TEST RESULT: FAIL (check CLK/RESET/SPI/reference supplies and register log)");
    }
    ESP_LOGI(TAG,
             "============================================================");
    s_self_test.complete = true;
}
#endif

static uint16_t ring_count_locked(void)
{
    if (s_ring == NULL) {
        return 0;
    }

    int count = (int)s_write_index - (int)s_read_index;
    if (count < 0) {
        count += EEG_ACQ_RING_SIZE;
    }
    return (uint16_t)count;
}

static bool ring_push(const eeg_acq_sample_t *sample)
{
    if (s_ring == NULL) {
        return false;
    }

    bool ok = true;
    portENTER_CRITICAL(&s_mux);
    const uint16_t next = (uint16_t)((s_write_index + 1U) % EEG_ACQ_RING_SIZE);
    if (next == s_read_index) {
        s_stats.ring_drop++;
        ok = false;
    } else {
        s_ring[s_write_index] = *sample;
        s_write_index = next;
    }
    portEXIT_CRITICAL(&s_mux);
    return ok;
}

bool eeg_acq_pop(eeg_acq_sample_t *sample)
{
    if (sample == NULL || s_ring == NULL) {
        return false;
    }

    bool ok = false;
    portENTER_CRITICAL(&s_mux);
    if (s_read_index != s_write_index) {
        *sample = s_ring[s_read_index];
        s_read_index = (uint16_t)((s_read_index + 1U) % EEG_ACQ_RING_SIZE);
        ok = true;
    }
    portEXIT_CRITICAL(&s_mux);
    return ok;
}

void eeg_acq_clear_buffer(void)
{
    portENTER_CRITICAL(&s_mux);
    s_read_index = s_write_index;
    portEXIT_CRITICAL(&s_mux);
}

void eeg_acq_get_stats(eeg_acq_stats_t *stats)
{
    if (stats == NULL) {
        return;
    }

    portENTER_CRITICAL(&s_mux);
    *stats = s_stats;
    stats->drdy_count = s_drdy_isr_count;
    stats->buffered = ring_count_locked();
    portEXIT_CRITICAL(&s_mux);
}

static void reset_drdy_health_window(int64_t now_us)
{
    eeg_acq_stats_t stats = {0};
    eeg_acq_get_stats(&stats);

    if (eeg_drdy_monitor_is_available()) {
        const esp_err_t err = eeg_drdy_monitor_reset();
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "DRDY PCNT reset failed: %s", esp_err_to_name(err));
        }
    }

    portENTER_CRITICAL(&s_mux);
    s_health_last_isr_count = stats.drdy_count;
    s_health_last_acquired_count = stats.acquired_count;
    s_health_last_low_recovery = stats.low_level_recovery;
    s_health_last_reinit_count = stats.reinit_count;
    s_health_window_start_us = now_us;
    s_health_window_suppressed = false;
    s_health_reset_requested = false;
    portEXIT_CRITICAL(&s_mux);
    s_last_stats_log_us = now_us;
}

static void log_drdy_health(int64_t now_us, const eeg_acq_stats_t *stats)
{
    uint32_t hardware_edges = 0U;
    bool hardware_available = eeg_drdy_monitor_is_available();
    if (hardware_available) {
        const esp_err_t err = eeg_drdy_monitor_take_count(&hardware_edges);
        if (err != ESP_OK) {
            hardware_available = false;
            ESP_LOGW(TAG, "DRDY PCNT read failed: %s", esp_err_to_name(err));
        }
    }

    uint32_t last_isr_count;
    uint32_t last_acquired_count;
    uint32_t last_low_recovery;
    uint32_t last_reinit_count;
    int64_t window_start_us;
    bool paused;
    portENTER_CRITICAL(&s_mux);
    last_isr_count = s_health_last_isr_count;
    last_acquired_count = s_health_last_acquired_count;
    last_low_recovery = s_health_last_low_recovery;
    last_reinit_count = s_health_last_reinit_count;
    window_start_us = s_health_window_start_us;
    paused = s_health_window_suppressed;
    s_health_last_isr_count = stats->drdy_count;
    s_health_last_acquired_count = stats->acquired_count;
    s_health_last_low_recovery = stats->low_level_recovery;
    s_health_last_reinit_count = stats->reinit_count;
    s_health_window_start_us = now_us;
    s_health_window_suppressed = false;
    portEXIT_CRITICAL(&s_mux);

    const uint64_t window_us = now_us > window_start_us ?
                               (uint64_t)(now_us - window_start_us) : 0ULL;
    const uint32_t isr_edges = stats->drdy_count - last_isr_count;
    const uint32_t acquired_samples = stats->acquired_count - last_acquired_count;
    const uint32_t low_recovery = stats->low_level_recovery - last_low_recovery;
    const uint32_t reinit_count = stats->reinit_count - last_reinit_count;
    const eeg_drdy_health_input_t input = {
        .hardware_edges = hardware_edges,
        .isr_edges = isr_edges,
        .acquired_samples = acquired_samples,
        .low_level_recovery = low_recovery,
        .reinit_count = reinit_count,
        .window_us = window_us,
        .expected_rate_hz = P300_SAMPLE_RATE_HZ,
        .hardware_available = hardware_available,
        .paused = paused,
    };
    const eeg_drdy_health_state_t state = eeg_drdy_health_classify(&input);
    const uint32_t sps_x10 = window_us > 0U ?
                             (uint32_t)(((uint64_t)hardware_edges * 10000000ULL +
                                         window_us / 2ULL) / window_us) : 0U;
    const int drdy_level = (s_board != NULL && s_board->ads_drdy >= 0) ?
                           gpio_get_level(s_board->ads_drdy) : -1;
    const char *detail = "-";
    if (state == EEG_DRDY_HEALTH_DRDY_RATE && hardware_edges < 10U) {
        detail = drdy_level == 0 ? "STUCK_LOW" : "STUCK_HIGH";
    }

    ESP_LOGI(TAG,
             "ADS1299 DRDY health: window_ms=%" PRIu64 " hw=%" PRIu32
             " isr=%" PRIu32 " acq=%" PRIu32 " hw_sps=%" PRIu32 ".%01" PRIu32
             " state=%s detail=%s level=%d",
             window_us / 1000ULL,
             hardware_edges,
             isr_edges,
             acquired_samples,
             sps_x10 / 10U,
             sps_x10 % 10U,
             eeg_drdy_health_state_name(state),
             detail,
             drdy_level);
}

bool eeg_acq_is_running(void)
{
    return s_running;
}

uint8_t eeg_acq_get_config_id(void)
{
    return s_config_id;
}

uint8_t eeg_acq_get_normal_bias_mask(void)
{
    return s_normal_bias_mask;
}

bool eeg_acq_normal_bias_is_verified(void)
{
    return s_normal_bias_verified;
}

bool eeg_acq_is_settling(void)
{
    return s_discard_samples > 0U;
}

esp_err_t eeg_acq_pause_ads1299_stream(void)
{
    if (!s_running || s_board == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    portENTER_CRITICAL(&s_mux);
    s_health_window_suppressed = true;
    portEXIT_CRITICAL(&s_mux);

    if (s_board->ads_drdy >= 0) {
        gpio_intr_disable(s_board->ads_drdy);
    }

    s_pause_requested = true;
    if (s_task != NULL) {
        xTaskNotifyGive(s_task);
    }
    const int64_t deadline_us = esp_timer_get_time() + 100000LL;
    while (!s_paused && esp_timer_get_time() < deadline_us) {
        vTaskDelay(pdMS_TO_TICKS(1));
    }
    if (!s_paused) {
        if (s_board->ads_drdy >= 0) {
            gpio_intr_enable(s_board->ads_drdy);
        }
        s_pause_requested = false;
        s_health_reset_requested = true;
        return ESP_ERR_TIMEOUT;
    }
    return ads1299_stop_continuous();
}

esp_err_t eeg_acq_resume_ads1299_stream(void)
{
    if (!s_running || s_board == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    ESP_RETURN_ON_ERROR(apply_ads1299_capture_config(), TAG, "restore ADS1299 capture config");
    if (s_config_id == EEG_ACQ_CONFIG_NORMAL) {
        s_normal_bias_verified = true;
    }
    ESP_RETURN_ON_ERROR(ads1299_start_continuous(), TAG, "restart ADS1299 continuous");
    s_discard_samples = EEG_ACQ_MODE_SETTLE_SAMPLES;
    s_last_acq_us = esp_timer_get_time();
    s_pause_requested = false;
    s_paused = false;
    s_health_reset_requested = true;
    if (s_board->ads_drdy >= 0) {
        gpio_intr_enable(s_board->ads_drdy);
    }
    return ESP_OK;
}

static esp_err_t resume_after_mode_config(void)
{
    esp_err_t err = ads1299_start_continuous();
    if (err != ESP_OK) {
        return err;
    }
    s_last_acq_us = esp_timer_get_time();
    s_pause_requested = false;
    s_paused = false;
    s_health_reset_requested = true;
    if (s_board != NULL && s_board->ads_drdy >= 0) {
        gpio_intr_enable(s_board->ads_drdy);
    }
    return ESP_OK;
}

eeg_acq_mode_switch_status_t eeg_acq_set_config_id(uint8_t target_config_id)
{
    if (target_config_id != EEG_ACQ_CONFIG_NORMAL &&
        target_config_id != EEG_ACQ_CONFIG_INTERNAL_SHORT) {
        return EEG_ACQ_MODE_SWITCH_INVALID_REQUEST;
    }
    if (!s_running || s_board == NULL) {
        return EEG_ACQ_MODE_SWITCH_INTERNAL_ERROR;
    }
    if (target_config_id == s_config_id && !s_paused) {
        return EEG_ACQ_MODE_SWITCH_OK;
    }
    const uint8_t previous_config_id = s_config_id;

    const esp_err_t pause_err = eeg_acq_pause_ads1299_stream();
    if (pause_err != ESP_OK) {
        return pause_err == ESP_ERR_TIMEOUT ? EEG_ACQ_MODE_SWITCH_PAUSE_TIMEOUT
                                            : EEG_ACQ_MODE_SWITCH_INTERNAL_ERROR;
    }
    eeg_acq_clear_buffer();

    esp_err_t err = target_config_id == EEG_ACQ_CONFIG_INTERNAL_SHORT
                        ? ads1299_apply_internal_short_config()
                        : ads1299_apply_verified_reference_config_with_bias(s_normal_bias_mask);
    if (err == ESP_OK) {
        err = resume_after_mode_config();
    }
    if (err == ESP_OK) {
        s_config_id = target_config_id;
        if (target_config_id == EEG_ACQ_CONFIG_NORMAL) {
            s_normal_bias_verified = true;
        }
        s_discard_samples = EEG_ACQ_MODE_SETTLE_SAMPLES;
        ESP_LOGI(TAG, "ADS1299 runtime config switched: config_id=%u settle_samples=%u",
                 (unsigned)s_config_id,
                 (unsigned)s_discard_samples);
        return EEG_ACQ_MODE_SWITCH_OK;
    }

    ESP_LOGE(TAG, "ADS1299 target config failed; restoring NORMAL: %s",
             esp_err_to_name(err));
    ads1299_stop_continuous();
    if (ads1299_apply_verified_reference_config_with_bias(s_normal_bias_mask) != ESP_OK ||
        resume_after_mode_config() != ESP_OK) {
        ESP_LOGE(TAG, "ADS1299 NORMAL restore failed; acquisition remains paused");
        s_config_id = previous_config_id;
        s_normal_bias_verified = false;
        s_pause_requested = true;
        s_paused = true;
        return EEG_ACQ_MODE_SWITCH_RESTORE_FAILED;
    }
    s_config_id = EEG_ACQ_CONFIG_NORMAL;
    s_normal_bias_verified = true;
    s_discard_samples = EEG_ACQ_MODE_SETTLE_SAMPLES;
    eeg_acq_clear_buffer();
    return EEG_ACQ_MODE_SWITCH_CONFIG_FAILED;
}

eeg_acq_bias_switch_status_t eeg_acq_set_normal_bias_mask(uint8_t target_mask)
{
    if (target_mask == 0U ||
        (target_mask & (uint8_t)~P300_ADS_CHANNEL_MASK) != 0U) {
        return EEG_ACQ_BIAS_SWITCH_INVALID_REQUEST;
    }
    if (!s_running || s_board == NULL || s_config_id != EEG_ACQ_CONFIG_NORMAL) {
        return EEG_ACQ_BIAS_SWITCH_INTERNAL_ERROR;
    }
    if (target_mask == s_normal_bias_mask && s_normal_bias_verified && !s_paused) {
        return EEG_ACQ_BIAS_SWITCH_OK;
    }

    const uint8_t previous_mask = s_normal_bias_mask;
    const esp_err_t pause_err = eeg_acq_pause_ads1299_stream();
    if (pause_err != ESP_OK) {
        return pause_err == ESP_ERR_TIMEOUT ? EEG_ACQ_BIAS_SWITCH_PAUSE_TIMEOUT
                                            : EEG_ACQ_BIAS_SWITCH_INTERNAL_ERROR;
    }
    eeg_acq_clear_buffer();

    esp_err_t err = ads1299_set_bias_sensp_verified(target_mask);
    if (err == ESP_OK) {
        err = resume_after_mode_config();
    }
    if (err == ESP_OK) {
        s_normal_bias_mask = target_mask;
        s_normal_bias_verified = true;
        s_discard_samples = EEG_ACQ_MODE_SETTLE_SAMPLES;
        eeg_acq_clear_buffer();
        ESP_LOGI(TAG, "ADS1299 normal BIAS mask switched: mask=0x%02x settle_samples=%u",
                 target_mask, (unsigned)s_discard_samples);
        return EEG_ACQ_BIAS_SWITCH_OK;
    }

    ESP_LOGE(TAG, "ADS1299 BIAS mask apply failed; restoring mask=0x%02x: %s",
             previous_mask, esp_err_to_name(err));
    ads1299_stop_continuous();
    if (ads1299_apply_verified_reference_config_with_bias(previous_mask) != ESP_OK ||
        resume_after_mode_config() != ESP_OK) {
        s_normal_bias_verified = false;
        s_pause_requested = true;
        s_paused = true;
        eeg_acq_clear_buffer();
        ESP_LOGE(TAG, "ADS1299 BIAS rollback failed; acquisition remains paused");
        return EEG_ACQ_BIAS_SWITCH_RESTORE_FAILED;
    }

    s_normal_bias_mask = previous_mask;
    s_normal_bias_verified = true;
    s_discard_samples = EEG_ACQ_MODE_SETTLE_SAMPLES;
    eeg_acq_clear_buffer();
    return EEG_ACQ_BIAS_SWITCH_APPLY_FAILED;
}

static esp_err_t begin_measurement_capture(void)
{
    if (s_board == NULL || s_board->ads_drdy < 0 || !s_paused) {
        return ESP_ERR_INVALID_STATE;
    }

    gpio_intr_disable(s_board->ads_drdy);
    s_measurement_task = xTaskGetCurrentTaskHandle();
    ulTaskNotifyTake(pdTRUE, 0);
    esp_err_t err = ads1299_start_continuous();
    if (err != ESP_OK) {
        s_measurement_task = NULL;
        return err;
    }
    gpio_intr_enable(s_board->ads_drdy);
    if (gpio_get_level(s_board->ads_drdy) == 0) {
        xTaskNotifyGive((TaskHandle_t)s_measurement_task);
    }
    return ESP_OK;
}

static esp_err_t end_measurement_capture(void)
{
    if (s_board != NULL && s_board->ads_drdy >= 0) {
        gpio_intr_disable(s_board->ads_drdy);
    }
    s_measurement_task = NULL;
    ulTaskNotifyTake(pdTRUE, 0);
    return ads1299_stop_continuous();
}

static esp_err_t capture_measurement_frames(uint16_t frame_count,
                                            eeg_impedance_accumulator_t *accumulator)
{
    for (uint16_t index = 0; index < frame_count; ++index) {
        uint32_t notifications = ulTaskNotifyTake(
            pdTRUE, pdMS_TO_TICKS(P300_IMPEDANCE_FRAME_TIMEOUT_MS));
        if (notifications == 0U && s_board != NULL && s_board->ads_drdy >= 0 &&
            gpio_get_level(s_board->ads_drdy) == 0) {
            notifications = 1U;
        }
        if (notifications != 1U) {
            ESP_LOGE(TAG, "impedance DRDY timeout/miss: frame=%u notifications=%" PRIu32,
                     (unsigned)index, notifications);
            return ESP_ERR_TIMEOUT;
        }

        ads1299_frame_t frame = {0};
        esp_err_t err = ads1299_read_frame(&frame);
        if (err != ESP_OK) {
            return err;
        }
        if ((frame.status[0] & 0xF0U) != 0xC0U) {
            ESP_LOGE(TAG, "impedance frame status invalid: %02x %02x %02x",
                     frame.status[0], frame.status[1], frame.status[2]);
            return ESP_ERR_INVALID_RESPONSE;
        }
        if (accumulator != NULL) {
            eeg_impedance_accumulator_add(accumulator,
                                          (uint64_t)esp_timer_get_time(),
                                          frame.channel);
        }
    }
    return ESP_OK;
}

static uint32_t impedance_u32(double value, bool *clipped)
{
    if (!isfinite(value) || value < 0.0) {
        if (clipped != NULL) {
            *clipped = true;
        }
        return UINT32_MAX;
    }
    if (value >= (double)UINT32_MAX) {
        if (clipped != NULL) {
            *clipped = true;
        }
        return UINT32_MAX - 1U;
    }
    return (uint32_t)(value + 0.5);
}

esp_err_t eeg_acq_measure_impedance(uint8_t channel_mask,
                                    eeg_acq_impedance_result_t *result)
{
    if (result == NULL || channel_mask == 0U) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!s_running || s_board == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_config_id != EEG_ACQ_CONFIG_NORMAL) {
        return ESP_ERR_INVALID_STATE;
    }

    memset(result, 0, sizeof(*result));
    for (uint8_t channel = 0; channel < EEG_ACQ_CHANNEL_COUNT; ++channel) {
        result->nominal_ohm[channel] = UINT32_MAX;
        result->excitation_nv_peak[channel] = UINT32_MAX;
    }
    result->status = EEG_ACQ_IMPEDANCE_STATUS_INTERNAL_ERROR;
    const int64_t started_us = esp_timer_get_time();
    const uint8_t tested_mask = (uint8_t)(channel_mask & P300_ADS_CHANNEL_MASK);
    bool paused = false;
    bool measurement_capture_started = false;
    eeg_impedance_accumulator_t background = {0};
    eeg_impedance_accumulator_t excited = {0};
    eeg_impedance_result_t calculated = {0};

    esp_err_t err = eeg_acq_pause_ads1299_stream();
    paused = s_paused;
    if (err != ESP_OK) {
        result->status = err == ESP_ERR_TIMEOUT ? EEG_ACQ_IMPEDANCE_STATUS_PAUSE_TIMEOUT
                                                : EEG_ACQ_IMPEDANCE_STATUS_CONFIG_FAILED;
        goto cleanup;
    }
    result->tested_mask = tested_mask;
    eeg_acq_clear_buffer();

    err = ads1299_configure_leadoff_dc(tested_mask);
    if (err != ESP_OK || ads1299_start_continuous() != ESP_OK) {
        result->status = EEG_ACQ_IMPEDANCE_STATUS_CONFIG_FAILED;
        goto cleanup;
    }
    vTaskDelay(pdMS_TO_TICKS(P300_IMPEDANCE_DC_SETTLE_MS));
    ads1299_leadoff_result_t dc_result = {0};
    err = ads1299_read_leadoff_status(tested_mask, &dc_result);
    if (err != ESP_OK) {
        result->status = EEG_ACQ_IMPEDANCE_STATUS_CONFIG_FAILED;
        goto cleanup;
    }
    result->tested_mask = dc_result.tested_channel_mask;
    result->open_mask = dc_result.loff_statp;
    result->flags |= EEG_ACQ_IMPEDANCE_FLAG_DC_VALID;

    err = apply_ads1299_capture_config();
    if (err != ESP_OK) {
        result->status = EEG_ACQ_IMPEDANCE_STATUS_CONFIG_FAILED;
        goto cleanup;
    }
    eeg_impedance_accumulator_reset(&background);
    err = begin_measurement_capture();
    measurement_capture_started = err == ESP_OK;
    if (err == ESP_OK) {
        err = capture_measurement_frames(P300_IMPEDANCE_BACKGROUND_SAMPLES, &background);
    }
    if (measurement_capture_started) {
        const esp_err_t stop_err = end_measurement_capture();
        measurement_capture_started = false;
        if (err == ESP_OK) {
            err = stop_err;
        }
    }
    if (err != ESP_OK) {
        result->status = EEG_ACQ_IMPEDANCE_STATUS_SAMPLE_TIMEOUT;
        goto cleanup;
    }

    err = ads1299_configure_leadoff_ac(tested_mask);
    if (err != ESP_OK) {
        result->status = EEG_ACQ_IMPEDANCE_STATUS_CONFIG_FAILED;
        goto cleanup;
    }
    eeg_impedance_accumulator_reset(&excited);
    err = begin_measurement_capture();
    measurement_capture_started = err == ESP_OK;
    if (err == ESP_OK) {
        err = capture_measurement_frames(P300_IMPEDANCE_SETTLE_SAMPLES, NULL);
    }
    if (err == ESP_OK) {
        err = capture_measurement_frames(P300_IMPEDANCE_MEASURE_SAMPLES, &excited);
    }
    if (measurement_capture_started) {
        const esp_err_t stop_err = end_measurement_capture();
        measurement_capture_started = false;
        if (err == ESP_OK) {
            err = stop_err;
        }
    }
    result->sample_count = excited.sample_count > UINT16_MAX ? UINT16_MAX
                                                              : (uint16_t)excited.sample_count;
    if (err != ESP_OK) {
        result->status = EEG_ACQ_IMPEDANCE_STATUS_SAMPLE_TIMEOUT;
        goto cleanup;
    }
    if (!eeg_impedance_calculate(&background, &excited, tested_mask, &calculated)) {
        result->status = EEG_ACQ_IMPEDANCE_STATUS_INTERNAL_ERROR;
        goto cleanup;
    }

    bool value_clipped = calculated.clipped_mask != 0U;
    for (uint8_t channel = 0; channel < EEG_ACQ_CHANNEL_COUNT; ++channel) {
        const uint8_t bit = (uint8_t)(1U << channel);
        if ((calculated.valid_mask & bit) == 0U) {
            continue;
        }
        result->excitation_nv_peak[channel] =
            impedance_u32(calculated.voltage_peak_v[channel] * 1.0e9, &value_clipped);
        if ((result->open_mask & bit) == 0U) {
            result->nominal_ohm[channel] =
                impedance_u32(calculated.nominal_ohm[channel], &value_clipped);
        }
    }
    result->flags |= EEG_ACQ_IMPEDANCE_FLAG_AC_VALID |
                     EEG_ACQ_IMPEDANCE_FLAG_BACKGROUND_SUBTRACTED;
    if (value_clipped) {
        result->flags |= EEG_ACQ_IMPEDANCE_FLAG_CLIPPED;
    }
    result->status = EEG_ACQ_IMPEDANCE_STATUS_OK;

cleanup:
    if (measurement_capture_started) {
        end_measurement_capture();
    }
    if (paused) {
        if (eeg_acq_resume_ads1299_stream() != ESP_OK) {
            result->status = EEG_ACQ_IMPEDANCE_STATUS_RESTORE_FAILED;
        } else {
            eeg_acq_clear_buffer();
        }
    }
    const int64_t duration_us = esp_timer_get_time() - started_us;
    const uint64_t duration_ms = duration_us <= 0 ? 0U : (uint64_t)duration_us / 1000U;
    result->duration_ms = duration_ms > UINT16_MAX ? UINT16_MAX : (uint16_t)duration_ms;
    ESP_LOGI(TAG,
             "impedance complete: status=%u tested=0x%02x open=0x%02x flags=0x%02x "
             "samples=%u duration_ms=%u",
             (unsigned)result->status,
             result->tested_mask,
             result->open_mask,
             result->flags,
             (unsigned)result->sample_count,
             (unsigned)result->duration_ms);
    return ESP_OK;
}

static esp_err_t allocate_ring_buffer(void)
{
    if (s_ring != NULL) {
        memset(s_ring, 0, sizeof(eeg_acq_sample_t) * EEG_ACQ_RING_SIZE);
        return ESP_OK;
    }

    const size_t bytes = sizeof(eeg_acq_sample_t) * EEG_ACQ_RING_SIZE;
    s_ring = heap_caps_calloc(EEG_ACQ_RING_SIZE, sizeof(eeg_acq_sample_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    s_ring_mem_name = "psram";
    if (s_ring == NULL) {
        s_ring = heap_caps_calloc(EEG_ACQ_RING_SIZE, sizeof(eeg_acq_sample_t), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
        s_ring_mem_name = "internal";
    }
    if (s_ring == NULL) {
        s_ring_mem_name = "none";
        ESP_LOGE(TAG, "ADS1299 ring allocation failed: size=%u bytes=%u",
                 (unsigned)EEG_ACQ_RING_SIZE,
                 (unsigned)bytes);
        return ESP_ERR_NO_MEM;
    }

    ESP_LOGI(TAG,
             "ADS1299 ring allocated: samples=%u sample_bytes=%u total_bytes=%u mem=%s ptr=%p",
             (unsigned)EEG_ACQ_RING_SIZE,
             (unsigned)sizeof(eeg_acq_sample_t),
             (unsigned)bytes,
             s_ring_mem_name,
             s_ring);
    return ESP_OK;
}

static void IRAM_ATTR drdy_isr(void *arg)
{
    (void)arg;
    s_drdy_isr_count++;

    BaseType_t task_woken = pdFALSE;
    TaskHandle_t target = (TaskHandle_t)s_measurement_task;
    if (target == NULL) {
        target = s_task;
    }
    if (target != NULL) {
        vTaskNotifyGiveFromISR(target, &task_woken);
    }
    if (task_woken == pdTRUE) {
        portYIELD_FROM_ISR();
    }
}

static void restart_ads_stream(const char *reason)
{
    if (s_board == NULL || s_board->ads_drdy < 0) {
        return;
    }

    gpio_intr_disable(s_board->ads_drdy);
    ulTaskNotifyTake(pdTRUE, 0);

    ads1299_stop_continuous();
    vTaskDelay(pdMS_TO_TICKS(2));
    esp_err_t err = apply_ads1299_capture_config();
    if (err == ESP_OK && s_config_id == EEG_ACQ_CONFIG_NORMAL) {
        s_normal_bias_verified = true;
    }
    if (err == ESP_OK) {
        err = ads1299_start_continuous();
    }
    if (err != ESP_OK) {
        s_normal_bias_verified = false;
        s_pause_requested = true;
        s_paused = true;
        s_health_reset_requested = true;
        ESP_LOGE(TAG, "ADS1299 stream restart failed; acquisition halted: %s",
                 esp_err_to_name(err));
        return;
    }
#if P300_ADS1299_INTERNAL_TEST
    reset_ads1299_self_test();
#endif

    portENTER_CRITICAL(&s_mux);
    s_stats.reinit_count++;
    portEXIT_CRITICAL(&s_mux);
    s_last_acq_us = esp_timer_get_time();

    gpio_intr_enable(s_board->ads_drdy);
    ESP_LOGW(TAG, "ADS1299 stream restarted: %s", reason ? reason : "unknown");
}

static void capture_sample(void)
{
    ads1299_frame_t frame = {0};
    esp_err_t err = ads1299_read_frame(&frame);
    if (err != ESP_OK) {
        portENTER_CRITICAL(&s_mux);
        s_stats.spi_fail++;
        portEXIT_CRITICAL(&s_mux);
        return;
    }
    if ((frame.status[0] & 0xF0U) != 0xC0U) {
        portENTER_CRITICAL(&s_mux);
        s_stats.spi_fail++;
        portEXIT_CRITICAL(&s_mux);
        s_bad_status_frames++;
        if (s_bad_status_frames <= 3U) {
            ESP_LOGW(TAG,
                     "ADS1299 dropped misaligned frame: status=%02x %02x %02x count=%" PRIu32,
                     frame.status[0], frame.status[1], frame.status[2], s_bad_status_frames);
        }
        return;
    }

    if (!s_first_frame_logged) {
        s_first_frame_logged = true;
        ESP_LOGI(TAG,
                 "ADS1299 first data frame: status=%02x %02x %02x raw=[%" PRId32 ",%" PRId32 ",%" PRId32
                 ",%" PRId32 ",%" PRId32 ",%" PRId32 ",%" PRId32 ",%" PRId32 "]",
                 frame.status[0],
                 frame.status[1],
                 frame.status[2],
                 frame.channel[0],
                 frame.channel[1],
                 frame.channel[2],
                 frame.channel[3],
                 frame.channel[4],
                 frame.channel[5],
                 frame.channel[6],
                 frame.channel[7]);
    }

    if (s_discard_samples > 0U) {
        s_discard_samples--;
        portENTER_CRITICAL(&s_mux);
        s_stats.acquired_count++;
        portEXIT_CRITICAL(&s_mux);
        s_last_acq_us = esp_timer_get_time();
        return;
    }

#if P300_ADS1299_INTERNAL_TEST
    consume_ads1299_self_test_frame(&frame, s_next_seq);
#endif

    eeg_acq_sample_t sample = {
        .seq = s_next_seq++,
        .timestamp_us = (uint64_t)esp_timer_get_time(),
    };
    memcpy(sample.channel, frame.channel, sizeof(sample.channel));
    ring_push(&sample);

    portENTER_CRITICAL(&s_mux);
    s_stats.acquired_count++;
    portEXIT_CRITICAL(&s_mux);
    s_last_acq_us = esp_timer_get_time();
}

static void acq_task(void *arg)
{
    (void)arg;
    s_last_acq_us = esp_timer_get_time();
    reset_drdy_health_window(s_last_acq_us);

    while (true) {
        if (s_pause_requested) {
            ulTaskNotifyTake(pdTRUE, 0);
            s_paused = true;
            s_last_acq_us = esp_timer_get_time();
            vTaskDelay(pdMS_TO_TICKS(1));
            continue;
        }
        s_paused = false;
        if (s_health_reset_requested) {
            reset_drdy_health_window(esp_timer_get_time());
        }

        const uint32_t notify_count = ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(EEG_ACQ_WAIT_TIMEOUT_MS));
        bool recovered_low_level = false;

        if (notify_count == 0) {
            if (s_board != NULL && s_board->ads_drdy >= 0 && gpio_get_level(s_board->ads_drdy) == 0) {
                recovered_low_level = true;
            } else if ((esp_timer_get_time() - s_last_acq_us) >= (EEG_ACQ_RESTART_TIMEOUT_MS * 1000LL)) {
                restart_ads_stream("notify-timeout");
                continue;
            } else {
                continue;
            }
        }

        if (notify_count > 1) {
            portENTER_CRITICAL(&s_mux);
            s_stats.missed_drdy += (notify_count - 1U);
            portEXIT_CRITICAL(&s_mux);
        }

        if (recovered_low_level) {
            portENTER_CRITICAL(&s_mux);
            s_stats.low_level_recovery++;
            portEXIT_CRITICAL(&s_mux);
        }

        capture_sample();

        uint8_t extra_reads = 0;
        while (s_board != NULL && s_board->ads_drdy >= 0 && gpio_get_level(s_board->ads_drdy) == 0) {
            portENTER_CRITICAL(&s_mux);
            s_stats.low_level_recovery++;
            portEXIT_CRITICAL(&s_mux);

            extra_reads++;
            if (extra_reads > EEG_ACQ_MAX_LOW_DRDY_READS) {
                portENTER_CRITICAL(&s_mux);
                s_stats.missed_drdy++;
                portEXIT_CRITICAL(&s_mux);
                restart_ads_stream("drdy-low");
                break;
            }
            capture_sample();
        }

        const int64_t now_us = esp_timer_get_time();
        if (P300_LOG_ACQ_STATS &&
            (now_us - s_last_stats_log_us) >= EEG_ACQ_STATS_INTERVAL_US) {
            s_last_stats_log_us = now_us;
            eeg_acq_stats_t stats = {0};
            eeg_acq_get_stats(&stats);
            ESP_LOGI(TAG,
                     "ADS1299 acq stats: drdy=%" PRIu32 " acq=%" PRIu32 " spi_fail=%" PRIu32
                     " missed=%" PRIu32 " low_recover=%" PRIu32 " reinit=%" PRIu32
                     " ring_drop=%" PRIu32 " buffered=%" PRIu32 " drdy_level=%d",
                     stats.drdy_count,
                     stats.acquired_count,
                     stats.spi_fail,
                     stats.missed_drdy,
                     stats.low_level_recovery,
                     stats.reinit_count,
                     stats.ring_drop,
                     stats.buffered,
                     (s_board != NULL && s_board->ads_drdy >= 0) ? gpio_get_level(s_board->ads_drdy) : -1);
            log_drdy_health(now_us, &stats);
        }
    }
}

esp_err_t eeg_acq_start(const board_config_t *board)
{
    if (s_running) {
        return ESP_OK;
    }
    if (board == NULL || board->ads_drdy < 0) {
        return ESP_ERR_INVALID_ARG;
    }

    s_board_storage = *board;
    s_board = &s_board_storage;
    s_write_index = 0;
    s_read_index = 0;
    s_next_seq = 0;
    memset(&s_stats, 0, sizeof(s_stats));
    s_drdy_isr_count = 0;
    s_first_frame_logged = false;
    s_bad_status_frames = 0;
    s_pause_requested = false;
    s_paused = false;
    s_health_window_suppressed = false;
    s_health_reset_requested = false;
    s_measurement_task = NULL;
    s_config_id = EEG_ACQ_CONFIG_NORMAL;
    s_discard_samples = 0U;
    s_normal_bias_mask = P300_ADS_CHANNEL_MASK;
    s_normal_bias_verified = false;
#if P300_ADS1299_INTERNAL_TEST
    reset_ads1299_self_test();
#endif
    ESP_RETURN_ON_ERROR(allocate_ring_buffer(), TAG, "allocate ADS1299 ring");

    ESP_LOGI(TAG,
             "ADS1299 acquisition bring-up: DRDY gpio=%d current_level=%d sample_rate=%dHz",
             board->ads_drdy,
             gpio_get_level(board->ads_drdy),
             P300_SAMPLE_RATE_HZ);
    ESP_RETURN_ON_ERROR(apply_ads1299_capture_config(), TAG, "ADS1299 capture config");
    s_normal_bias_verified = true;
    ESP_RETURN_ON_ERROR(ads1299_start_continuous(), TAG, "ADS1299 start continuous");

    const esp_err_t monitor_err = eeg_drdy_monitor_init(board->ads_drdy);
    if (monitor_err != ESP_OK) {
        ESP_LOGW(TAG,
                 "DRDY PCNT diagnostics unavailable on gpio=%d: %s",
                 board->ads_drdy,
                 esp_err_to_name(monitor_err));
    } else {
        ESP_LOGI(TAG, "DRDY PCNT diagnostics enabled on gpio=%d", board->ads_drdy);
    }

    gpio_set_intr_type(board->ads_drdy, GPIO_INTR_NEGEDGE);
    esp_err_t err = gpio_install_isr_service(0);
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        eeg_drdy_monitor_deinit();
        ESP_RETURN_ON_ERROR(err, TAG, "install GPIO ISR service");
    }
    err = gpio_isr_handler_add(board->ads_drdy, drdy_isr, NULL);
    if (err != ESP_OK) {
        eeg_drdy_monitor_deinit();
        ESP_RETURN_ON_ERROR(err, TAG, "add DRDY ISR");
    }

    /* The C5 has one core: process each DRDY before lower-priority TCP/UDP work. */
    BaseType_t ok = xTaskCreate(acq_task, "eeg_acq", 4096, NULL,
                                P300_EEG_ACQ_TASK_PRIORITY, &s_task);
    if (ok != pdPASS) {
        gpio_isr_handler_remove(board->ads_drdy);
        eeg_drdy_monitor_deinit();
        return ESP_ERR_NO_MEM;
    }

    s_running = true;
    ESP_LOGI(TAG, "ADS1299 acquisition started: drdy_gpio=%d expected_sample_rate=%dHz ring=%d mem=%s",
             board->ads_drdy,
             P300_SAMPLE_RATE_HZ,
             EEG_ACQ_RING_SIZE,
             s_ring_mem_name);
    ESP_LOGI(TAG, "ADS1299 waiting for DRDY falling edges; if drdy stays constant, check CLK/START/PWDN/RESET");
    return ESP_OK;
}

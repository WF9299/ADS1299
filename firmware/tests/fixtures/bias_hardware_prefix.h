/* Host fault-injection fixture. Production function bodies are included by
 * test_p300_bias_hardware.py; only register IO, scheduling and IRQ are mocked. */
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "ads1299_regs.h"
typedef int esp_err_t;
typedef int eeg_acq_bias_switch_status_t;
enum { ESP_OK=0, ESP_FAIL=-1, ESP_ERR_INVALID_STATE=-2, ESP_ERR_INVALID_ARG=-3,
       ESP_ERR_TIMEOUT=-4 };
enum { EEG_ACQ_BIAS_SWITCH_OK=0, EEG_ACQ_BIAS_SWITCH_INVALID_REQUEST=2,
       EEG_ACQ_BIAS_SWITCH_PAUSE_TIMEOUT=3, EEG_ACQ_BIAS_SWITCH_APPLY_FAILED=4,
       EEG_ACQ_BIAS_SWITCH_RESTORE_FAILED=5, EEG_ACQ_BIAS_SWITCH_INTERNAL_ERROR=6 };
#define EEG_ACQ_CONFIG_NORMAL 1
#define EEG_ACQ_MODE_SETTLE_SAMPLES 200
#define P300_ADS_CHANNEL_MASK 0xff
#define ADS1299_CHSET_GAIN12_NORMAL 0x50
#define ADS1299_CHSET_POWERDOWN_GAIN12_SHORT 0xd1
#define ESP_RETURN_ON_ERROR(call, ...) do { int e=(call); if(e!=ESP_OK) return e; } while(0)
#define ESP_LOGI(...) ((void)0)
#define ESP_LOGE(...) ((void)0)
static struct { uint8_t enabled_channel_mask; } s_config = {0xff};
static bool s_initialized=true, s_running=true, s_paused=false, s_pause_requested=false;
static bool s_normal_bias_verified=true;
static uint8_t s_config_id=1, s_normal_bias_mask=0xff;
static unsigned s_discard_samples;
static const void *s_board=(void*)1;
static uint8_t regs[32];
static int write_fail_reg=-1, write_fail_count, read_fail_reg=-1, read_fail_count;
static int resume_fail_count, pause_error, resumes, clears;
static esp_err_t ads1299_write_reg(uint8_t reg, uint8_t value) {
    if(reg==write_fail_reg && write_fail_count!=0) { --write_fail_count; return ESP_FAIL; }
    regs[reg]=value; return ESP_OK;
}
static esp_err_t ads1299_check_reg_masked(uint8_t reg, uint8_t expected, uint8_t mask) {
    if(reg==read_fail_reg && read_fail_count!=0) { --read_fail_count; return ESP_FAIL; }
    return (regs[reg]&mask)==(expected&mask) ? ESP_OK : ESP_FAIL;
}
static esp_err_t ads1299_check_reg(uint8_t reg, uint8_t expected) {
    return ads1299_check_reg_masked(reg, expected, 255);
}
static esp_err_t eeg_acq_pause_ads1299_stream(void) {
    if(pause_error) return pause_error;
    s_paused=true; return ESP_OK;
}
static void eeg_acq_clear_buffer(void) { ++clears; }
static esp_err_t resume_after_mode_config(void) {
    ++resumes;
    /* Settling must be armed before the IRQ/sample producer is resumed. */
    assert(s_discard_samples==EEG_ACQ_MODE_SETTLE_SAMPLES);
    if(resume_fail_count) { --resume_fail_count; return ESP_FAIL; }
    s_paused=false; s_pause_requested=false; return ESP_OK;
}
static esp_err_t ads1299_stop_continuous(void) { s_paused=true; return ESP_OK; }
static const char *esp_err_to_name(esp_err_t e) { (void)e; return "injected"; }

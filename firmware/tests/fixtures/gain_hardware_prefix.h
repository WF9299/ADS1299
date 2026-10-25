typedef int eeg_acq_gain_switch_status_t;
typedef int eeg_acq_mode_switch_status_t;
enum { EEG_ACQ_GAIN_SWITCH_OK=0, EEG_ACQ_GAIN_SWITCH_INVALID_REQUEST=2,
       EEG_ACQ_GAIN_SWITCH_APPLY_FAILED=4, EEG_ACQ_GAIN_SWITCH_RESTORE_FAILED=5,
       EEG_ACQ_GAIN_SWITCH_INTERNAL_ERROR=6 };
enum { EEG_ACQ_MODE_SWITCH_OK=0, EEG_ACQ_MODE_SWITCH_INVALID_REQUEST=2,
       EEG_ACQ_MODE_SWITCH_PAUSE_TIMEOUT=4, EEG_ACQ_MODE_SWITCH_CONFIG_FAILED=5,
       EEG_ACQ_MODE_SWITCH_RESTORE_FAILED=6, EEG_ACQ_MODE_SWITCH_INTERNAL_ERROR=7 };
#define EEG_ACQ_CONFIG_INTERNAL_SHORT 2
#define ADS1299_CHSET_GAIN12_SHORT 0x51
#define ADS1299_INTERNAL_REF_STARTUP_MS 150
#define pdMS_TO_TICKS(ms) (ms)
#define vTaskDelay(ticks) ((void)(ticks))
static int snapshot_fail_reg=-1;
static esp_err_t ads1299_read_reg(uint8_t reg, uint8_t *value) {
    if (reg==snapshot_fail_reg) { snapshot_fail_reg=-1; return ESP_FAIL; }
    *value=regs[reg]; return ESP_OK;
}

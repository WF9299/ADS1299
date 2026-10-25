/* IO and measurement timing only. Compile the production impedance transaction
 * below and assert its register gain at every measurement phase. */
#define EEG_ACQ_CHANNEL_COUNT 8
#define P300_IMPEDANCE_DC_SETTLE_MS 1
#define P300_IMPEDANCE_BACKGROUND_SAMPLES 1000
#define P300_IMPEDANCE_SETTLE_SAMPLES 200
#define P300_IMPEDANCE_MEASURE_SAMPLES 1000
enum { EEG_ACQ_IMPEDANCE_STATUS_OK=0, EEG_ACQ_IMPEDANCE_STATUS_BUSY=1,
       EEG_ACQ_IMPEDANCE_STATUS_INVALID_REQUEST=2, EEG_ACQ_IMPEDANCE_STATUS_PAUSE_TIMEOUT=3,
       EEG_ACQ_IMPEDANCE_STATUS_CONFIG_FAILED=4, EEG_ACQ_IMPEDANCE_STATUS_SAMPLE_TIMEOUT=5,
       EEG_ACQ_IMPEDANCE_STATUS_RESTORE_FAILED=6, EEG_ACQ_IMPEDANCE_STATUS_INTERNAL_ERROR=7 };
enum { EEG_ACQ_IMPEDANCE_FLAG_DC_VALID=1, EEG_ACQ_IMPEDANCE_FLAG_AC_VALID=2,
       EEG_ACQ_IMPEDANCE_FLAG_BACKGROUND_SUBTRACTED=4, EEG_ACQ_IMPEDANCE_FLAG_CLIPPED=8 };
typedef struct { int status; uint8_t tested_mask, open_mask, flags; uint16_t duration_ms,sample_count;
    uint32_t nominal_ohm[8], excitation_nv_peak[8]; } eeg_acq_impedance_result_t;
typedef struct { uint8_t tested_channel_mask,loff_statp,loff_statn; } ads1299_leadoff_result_t;
typedef struct { uint32_t sample_count; } eeg_impedance_accumulator_t;
typedef struct { uint8_t valid_mask,clipped_mask; double voltage_peak_v[8],nominal_ohm[8]; } eeg_impedance_result_t;
static int measure_phase, fail_measure_phase, fail_dc;
static int64_t esp_timer_get_time(void) { return 1000; }
static void assert_diagnostic_gain(void) {
    for(int ch=0;ch<8;++ch) assert((regs[ADS1299_REG_CH1SET+ch]&0x70)==0x50);
}
static esp_err_t ads1299_start_continuous(void) { return ESP_OK; }
static esp_err_t ads1299_configure_leadoff_dc(uint8_t m) {
    (void)m; assert_diagnostic_gain(); return fail_dc ? ESP_FAIL : ESP_OK;
}
static esp_err_t ads1299_configure_leadoff_ac(uint8_t m) {
    (void)m; assert_diagnostic_gain(); return ESP_OK;
}
static esp_err_t ads1299_read_leadoff_status(uint8_t m,ads1299_leadoff_result_t *r) {
    r->tested_channel_mask=m; r->loff_statp=0; return ESP_OK;
}
static void eeg_impedance_accumulator_reset(eeg_impedance_accumulator_t *a) { memset(a,0,sizeof(*a)); }
static esp_err_t begin_measurement_capture(void) { assert_diagnostic_gain(); return ESP_OK; }
static esp_err_t end_measurement_capture(void) { return ESP_OK; }
static esp_err_t capture_measurement_frames(unsigned n,eeg_impedance_accumulator_t *a) {
    assert_diagnostic_gain(); ++measure_phase;
    if(measure_phase==fail_measure_phase) return ESP_FAIL;
    if(a) a->sample_count=n;
    return ESP_OK;
}
static bool eeg_impedance_calculate(const eeg_impedance_accumulator_t *a,
    const eeg_impedance_accumulator_t *b,uint8_t m,eeg_impedance_result_t *r) {
    (void)a; (void)b; memset(r,0,sizeof(*r)); r->valid_mask=m; return true;
}
static uint32_t impedance_u32(double value,bool *clipped) { (void)clipped; return (uint32_t)value; }
static esp_err_t eeg_acq_resume_ads1299_stream(void) {
    esp_err_t err=apply_ads1299_capture_config();
    if(err!=ESP_OK) return err;
    return resume_after_mode_config();
}

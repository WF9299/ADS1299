#include "ads1299.h"

#include <inttypes.h>
#include <string.h>

#include "ads1299_regs.h"
#include "driver/gpio.h"
#include "driver/spi_master.h"
#include "esp_check.h"
#include "esp_err.h"
#include "esp_log.h"
#include "esp_rom_sys.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "app_config.h"

static const char *TAG = "ads1299";

#define ADS1299_SPI_HOST SPI2_HOST
#define ADS1299_FRAME_BYTES 27
#define ADS1299_CHSET_GAIN12_NORMAL 0x50
#define ADS1299_CHSET_GAIN12_TEST 0x55
#define ADS1299_CHSET_GAIN12_SHORT 0x51
/* TI recommends MUX=input-short (001) whenever a channel is powered down. */
#define ADS1299_CHSET_POWERDOWN_GAIN12_SHORT 0xD1
#define ADS1299_ID_READ_ATTEMPTS 3
/* Ignore revision bits [7:5]; fixed bits [4:0] identify the 8-channel ADS1299. */
#define ADS1299_ID_DEVICE_MASK 0x1FU
#define ADS1299_ID_8_CHANNEL 0x1EU
/* tSCCS >= 4*tCLK: 2.664 us at the slowest specified 1.5-MHz ADS clock. */
#define ADS1299_CS_HOLD_US 3U
#define ADS1299_INTERNAL_REF_STARTUP_MS 160
#define ADS1299_LEADOFF_SETTLE_MS 250
#define ADS1299_CONFIG4_PD_LOFF_COMP (1U << 1)
#define ADS1299_LOFF_DC_6NA 0x00U
#define ADS1299_LOFF_AC_24NA_31HZ 0x06U

static ads1299_config_t s_config;
static spi_device_handle_t s_spi;
static bool s_initialized;
static bool s_rdatac;

static const char *ads1299_spi_host_name(void)
{
    switch (ADS1299_SPI_HOST) {
    case SPI2_HOST:
        return "SPI2";
    default:
        return "SPI?";
    }
}

static bool gpio_is_valid(gpio_num_t gpio)
{
    return gpio >= 0;
}

static esp_err_t ads1299_spi_transfer(const uint8_t *tx, uint8_t *rx, size_t len)
{
    if (s_spi == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    spi_transaction_t trans = {
        .length = len * 8,
        .tx_buffer = tx,
        .rx_buffer = rx,
    };
    return spi_device_transmit(s_spi, &trans);
}

static esp_err_t ads1299_send_cmd(uint8_t cmd)
{
    uint8_t tx = cmd;
    esp_err_t err = ads1299_spi_transfer(&tx, NULL, 1);
    esp_rom_delay_us(4);
    return err;
}

static int32_t sign_extend_24(uint32_t value)
{
    if (value & 0x800000U) {
        value |= 0xFF000000U;
    }
    return (int32_t)value;
}

static const char *ads1299_reg_name(uint8_t reg)
{
    switch (reg) {
    case ADS1299_REG_ID:
        return "ID";
    case ADS1299_REG_CONFIG1:
        return "CONFIG1";
    case ADS1299_REG_CONFIG2:
        return "CONFIG2";
    case ADS1299_REG_CONFIG3:
        return "CONFIG3";
    case ADS1299_REG_CH1SET:
        return "CH1SET";
    case ADS1299_REG_CH2SET:
        return "CH2SET";
    case ADS1299_REG_CH3SET:
        return "CH3SET";
    case ADS1299_REG_CH4SET:
        return "CH4SET";
    case ADS1299_REG_CH5SET:
        return "CH5SET";
    case ADS1299_REG_CH6SET:
        return "CH6SET";
    case ADS1299_REG_CH7SET:
        return "CH7SET";
    case ADS1299_REG_CH8SET:
        return "CH8SET";
    case ADS1299_REG_BIAS_SENSP:
        return "BIAS_SENSP";
    case ADS1299_REG_BIAS_SENSN:
        return "BIAS_SENSN";
    case ADS1299_REG_LOFF_SENSP:
        return "LOFF_SENSP";
    case ADS1299_REG_LOFF_SENSN:
        return "LOFF_SENSN";
    case ADS1299_REG_LOFF_FLIP:
        return "LOFF_FLIP";
    case ADS1299_REG_LOFF_STATP:
        return "LOFF_STATP";
    case ADS1299_REG_LOFF_STATN:
        return "LOFF_STATN";
    case ADS1299_REG_MISC1:
        return "MISC1";
    case ADS1299_REG_CONFIG4:
        return "CONFIG4";
    default:
        return "REG";
    }
}

static esp_err_t ads1299_check_reg(uint8_t reg, uint8_t expected)
{
    uint8_t actual = 0;
    esp_err_t err = ads1299_read_reg(reg, &actual);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "SPI readback %s(0x%02x) failed: %s",
                 ads1299_reg_name(reg),
                 reg,
                 esp_err_to_name(err));
        return err;
    }

    if (actual != expected) {
        ESP_LOGE(TAG, "SPI readback mismatch %s(0x%02x): expected=0x%02x actual=0x%02x",
                 ads1299_reg_name(reg),
                 reg,
                 expected,
                 actual);
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "SPI readback OK %s(0x%02x)=0x%02x",
             ads1299_reg_name(reg),
             reg,
             actual);
    return ESP_OK;
}

static esp_err_t ads1299_check_reg_masked(uint8_t reg, uint8_t expected, uint8_t mask)
{
    uint8_t actual = 0;
    esp_err_t err = ads1299_read_reg(reg, &actual);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "SPI readback %s(0x%02x) failed: %s",
                 ads1299_reg_name(reg),
                 reg,
                 esp_err_to_name(err));
        return err;
    }

    if ((actual & mask) != (expected & mask)) {
        ESP_LOGE(TAG,
                 "SPI readback mismatch %s(0x%02x): expected=0x%02x actual=0x%02x mask=0x%02x",
                 ads1299_reg_name(reg),
                 reg,
                 expected,
                 actual,
                 mask);
        return ESP_FAIL;
    }

    if (actual != expected) {
        ESP_LOGW(TAG,
                 "SPI readback OK masked %s(0x%02x): expected=0x%02x actual=0x%02x mask=0x%02x",
                 ads1299_reg_name(reg),
                 reg,
                 expected,
                 actual,
                 mask);
    } else {
        ESP_LOGI(TAG, "SPI readback OK %s(0x%02x)=0x%02x",
                 ads1299_reg_name(reg),
                 reg,
                 actual);
    }
    return ESP_OK;
}

ads1299_config_t ads1299_default_config(const board_config_t *board)
{
    ads1299_config_t config = {
        .board = board ? *board : board_default_config(),
        .spi_clock_hz = 1 * 1000 * 1000,
        .enabled_channel_mask = P300_ADS_CHANNEL_MASK,
    };
    return config;
}

esp_err_t ads1299_read_reg(uint8_t reg, uint8_t *value)
{
    if (!s_initialized) {
        return ESP_ERR_INVALID_STATE;
    }
    if (value == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    if (s_rdatac) {
        ESP_RETURN_ON_ERROR(ads1299_stop_continuous(), TAG, "stop RDATAC before RREG");
    }

    uint8_t tx[3] = {
        (uint8_t)(ADS1299_CMD_RREG | (reg & 0x1F)),
        0x00,
        0x00,
    };
    uint8_t rx[sizeof(tx)] = {0};
    ESP_RETURN_ON_ERROR(ads1299_spi_transfer(tx, rx, sizeof(tx)), TAG, "RREG 0x%02x failed", reg);
    *value = rx[2];
    return ESP_OK;
}

esp_err_t ads1299_write_reg(uint8_t reg, uint8_t value)
{
    if (!s_initialized) {
        return ESP_ERR_INVALID_STATE;
    }

    if (s_rdatac) {
        ESP_RETURN_ON_ERROR(ads1299_stop_continuous(), TAG, "stop RDATAC before WREG");
    }

    uint8_t tx[3] = {
        (uint8_t)(ADS1299_CMD_WREG | (reg & 0x1F)),
        0x00,
        value,
    };
    ESP_RETURN_ON_ERROR(ads1299_spi_transfer(tx, NULL, sizeof(tx)), TAG, "WREG 0x%02x failed", reg);
    return ESP_OK;
}

esp_err_t ads1299_read_id(uint8_t *id)
{
    return ads1299_read_reg(ADS1299_REG_ID, id);
}

static esp_err_t ads1299_apply_verified_reference_config_impl(bool enable_all_channels,
                                                              uint8_t bias_pos)
{
    if (!s_initialized) {
        return ESP_ERR_INVALID_STATE;
    }

    const uint8_t mask = enable_all_channels ? 0xFF : s_config.enabled_channel_mask;
    if (bias_pos == 0U || (bias_pos & (uint8_t)~mask) != 0U) {
        return ESP_ERR_INVALID_ARG;
    }
    /* Only enabled, electrode-connected P inputs may enter the BIAS loop.
     * Including floating inputs injects their mains pickup back into BIASOUT. */

    ESP_RETURN_ON_ERROR(ads1299_write_reg(ADS1299_REG_CONFIG1, 0xD4), TAG, "write CONFIG1");
    /* Normal electrode acquisition: keep the internal calibration square wave
     * and unused lead-off comparators disabled. Leaving CONFIG2 at 0xD5 makes
     * the 1.953125-Hz calibration generator toggle even with MUX=normal. */
    ESP_RETURN_ON_ERROR(ads1299_write_reg(ADS1299_REG_CONFIG2, 0xC0), TAG, "write CONFIG2");
    ESP_RETURN_ON_ERROR(ads1299_write_reg(ADS1299_REG_CONFIG3, 0xEC), TAG, "write CONFIG3");

    for (uint8_t ch = 0; ch < 8; ++ch) {
        const uint8_t reg = (uint8_t)(ADS1299_REG_CH1SET + ch);
        const bool enabled = (mask & (1U << ch)) != 0;
        const uint8_t value = enabled ? ADS1299_CHSET_GAIN12_NORMAL
                                      : ADS1299_CHSET_POWERDOWN_GAIN12_SHORT;
        ESP_RETURN_ON_ERROR(ads1299_write_reg(reg, value), TAG, "write CH%uSET", (unsigned)(ch + 1));
    }

    ESP_RETURN_ON_ERROR(ads1299_write_reg(ADS1299_REG_MISC1, 0x20), TAG, "write MISC1");
    ESP_RETURN_ON_ERROR(ads1299_write_reg(ADS1299_REG_BIAS_SENSP, bias_pos), TAG, "write BIAS_SENSP");
    ESP_RETURN_ON_ERROR(ads1299_write_reg(ADS1299_REG_BIAS_SENSN, 0x00), TAG, "write BIAS_SENSN");
    ESP_RETURN_ON_ERROR(ads1299_write_reg(ADS1299_REG_LOFF, 0x00), TAG, "write LOFF");
    ESP_RETURN_ON_ERROR(ads1299_write_reg(ADS1299_REG_LOFF_SENSP, 0x00), TAG, "write LOFF_SENSP");
    ESP_RETURN_ON_ERROR(ads1299_write_reg(ADS1299_REG_LOFF_SENSN, 0x00), TAG, "write LOFF_SENSN");
    ESP_RETURN_ON_ERROR(ads1299_write_reg(ADS1299_REG_LOFF_FLIP, 0x00), TAG, "write LOFF_FLIP");
    ESP_RETURN_ON_ERROR(ads1299_write_reg(ADS1299_REG_CONFIG4, 0x00), TAG, "write CONFIG4");

    ESP_LOGI(TAG,
             "ADS1299 verified reference config applied: mode=%s ch_mask=0x%02x bias_pos=0x%02x",
             enable_all_channels ? "all-channels-normal" : "reference-mask",
             mask,
             bias_pos);

    ESP_LOGI(TAG, "ADS1299 SPI register readback test begin");
    ESP_RETURN_ON_ERROR(ads1299_check_reg(ADS1299_REG_CONFIG1, 0xD4), TAG, "check CONFIG1");
    ESP_RETURN_ON_ERROR(ads1299_check_reg(ADS1299_REG_CONFIG2, 0xC0), TAG, "check CONFIG2");
    ESP_RETURN_ON_ERROR(ads1299_check_reg_masked(ADS1299_REG_CONFIG3, 0xEC, 0xFE), TAG, "check CONFIG3");
    for (uint8_t ch = 0; ch < 8; ++ch) {
        const uint8_t reg = (uint8_t)(ADS1299_REG_CH1SET + ch);
        const bool enabled = (mask & (1U << ch)) != 0;
        const uint8_t value = enabled ? ADS1299_CHSET_GAIN12_NORMAL
                                      : ADS1299_CHSET_POWERDOWN_GAIN12_SHORT;
        ESP_RETURN_ON_ERROR(ads1299_check_reg(reg, value), TAG, "check CH%uSET", (unsigned)(ch + 1));
    }
    ESP_RETURN_ON_ERROR(ads1299_check_reg(ADS1299_REG_MISC1, 0x20), TAG, "check MISC1");
    ESP_RETURN_ON_ERROR(ads1299_check_reg(ADS1299_REG_BIAS_SENSP, bias_pos), TAG, "check BIAS_SENSP");
    ESP_RETURN_ON_ERROR(ads1299_check_reg(ADS1299_REG_BIAS_SENSN, 0x00), TAG, "check BIAS_SENSN");
    ESP_RETURN_ON_ERROR(ads1299_check_reg(ADS1299_REG_LOFF, 0x00), TAG, "check LOFF");
    ESP_RETURN_ON_ERROR(ads1299_check_reg(ADS1299_REG_LOFF_SENSP, 0x00), TAG, "check LOFF_SENSP");
    ESP_RETURN_ON_ERROR(ads1299_check_reg(ADS1299_REG_LOFF_SENSN, 0x00), TAG, "check LOFF_SENSN");
    ESP_RETURN_ON_ERROR(ads1299_check_reg(ADS1299_REG_LOFF_FLIP, 0x00), TAG, "check LOFF_FLIP");
    ESP_RETURN_ON_ERROR(ads1299_check_reg(ADS1299_REG_CONFIG4, 0x00), TAG, "check CONFIG4");
    ESP_LOGI(TAG, "ADS1299 SPI register readback test passed");
    return ESP_OK;
}

esp_err_t ads1299_apply_verified_reference_config(bool enable_all_channels)
{
    const uint8_t mask = enable_all_channels ? 0xFFU : s_config.enabled_channel_mask;
    return ads1299_apply_verified_reference_config_impl(enable_all_channels, mask);
}

esp_err_t ads1299_apply_verified_reference_config_with_bias(uint8_t bias_mask)
{
    return ads1299_apply_verified_reference_config_impl(false, bias_mask);
}

esp_err_t ads1299_set_bias_sensp_verified(uint8_t bias_mask)
{
    if (!s_initialized) {
        return ESP_ERR_INVALID_STATE;
    }
    if (bias_mask == 0U ||
        (bias_mask & (uint8_t)~s_config.enabled_channel_mask) != 0U) {
        return ESP_ERR_INVALID_ARG;
    }
    ESP_RETURN_ON_ERROR(ads1299_write_reg(ADS1299_REG_BIAS_SENSP, bias_mask),
                        TAG, "write runtime BIAS_SENSP");
    ESP_RETURN_ON_ERROR(ads1299_check_reg(ADS1299_REG_BIAS_SENSP, bias_mask),
                        TAG, "verify runtime BIAS_SENSP");
    ESP_LOGI(TAG, "ADS1299 runtime BIAS_SENSP verified: mask=0x%02x", bias_mask);
    return ESP_OK;
}

esp_err_t ads1299_apply_internal_test_config(void)
{
    if (!s_initialized) {
        return ESP_ERR_INVALID_STATE;
    }

    /*
     * 1 kSPS, internal 4.5-V reference, gain 12, internal square-wave test
     * source on every channel. CONFIG2=0xD5 selects 2x amplitude and
     * fCLK/2^20 (1.953125 Hz at the nominal 2.048-MHz master clock).
     * Electrode, SRB and BIAS pins do not participate in this test.
     */
    ESP_RETURN_ON_ERROR(ads1299_write_reg(ADS1299_REG_CONFIG1, 0xD4), TAG, "write test CONFIG1");
    ESP_RETURN_ON_ERROR(ads1299_write_reg(ADS1299_REG_CONFIG2, 0xD5), TAG, "write test CONFIG2");
    ESP_RETURN_ON_ERROR(ads1299_write_reg(ADS1299_REG_CONFIG3, 0xE0), TAG, "write test CONFIG3");
    vTaskDelay(pdMS_TO_TICKS(ADS1299_INTERNAL_REF_STARTUP_MS));

    for (uint8_t ch = 0; ch < 8; ++ch) {
        const uint8_t reg = (uint8_t)(ADS1299_REG_CH1SET + ch);
        ESP_RETURN_ON_ERROR(ads1299_write_reg(reg, ADS1299_CHSET_GAIN12_TEST),
                            TAG, "write test CH%uSET", (unsigned)(ch + 1));
    }

    ESP_RETURN_ON_ERROR(ads1299_write_reg(ADS1299_REG_MISC1, 0x00), TAG, "write test MISC1");
    ESP_RETURN_ON_ERROR(ads1299_write_reg(ADS1299_REG_BIAS_SENSP, 0x00), TAG, "write test BIAS_SENSP");
    ESP_RETURN_ON_ERROR(ads1299_write_reg(ADS1299_REG_BIAS_SENSN, 0x00), TAG, "write test BIAS_SENSN");
    ESP_RETURN_ON_ERROR(ads1299_write_reg(ADS1299_REG_LOFF_SENSP, 0x00), TAG, "write test LOFF_SENSP");
    ESP_RETURN_ON_ERROR(ads1299_write_reg(ADS1299_REG_LOFF_SENSN, 0x00), TAG, "write test LOFF_SENSN");
    ESP_RETURN_ON_ERROR(ads1299_write_reg(ADS1299_REG_CONFIG4, 0x00), TAG, "write test CONFIG4");

    ESP_LOGI(TAG,
             "ADS1299 INTERNAL TEST configured: 8ch gain=12 mux=test amplitude=2x "
             "frequency=1.953125Hz sample_rate=1000Hz");
    ESP_LOGI(TAG, "ADS1299 internal-test register readback begin");
    ESP_RETURN_ON_ERROR(ads1299_check_reg(ADS1299_REG_CONFIG1, 0xD4), TAG, "check test CONFIG1");
    ESP_RETURN_ON_ERROR(ads1299_check_reg(ADS1299_REG_CONFIG2, 0xD5), TAG, "check test CONFIG2");
    ESP_RETURN_ON_ERROR(ads1299_check_reg_masked(ADS1299_REG_CONFIG3, 0xE0, 0xFE),
                        TAG, "check test CONFIG3");
    for (uint8_t ch = 0; ch < 8; ++ch) {
        const uint8_t reg = (uint8_t)(ADS1299_REG_CH1SET + ch);
        ESP_RETURN_ON_ERROR(ads1299_check_reg(reg, ADS1299_CHSET_GAIN12_TEST),
                            TAG, "check test CH%uSET", (unsigned)(ch + 1));
    }
    ESP_RETURN_ON_ERROR(ads1299_check_reg(ADS1299_REG_MISC1, 0x00), TAG, "check test MISC1");
    ESP_RETURN_ON_ERROR(ads1299_check_reg(ADS1299_REG_BIAS_SENSP, 0x00), TAG, "check test BIAS_SENSP");
    ESP_RETURN_ON_ERROR(ads1299_check_reg(ADS1299_REG_BIAS_SENSN, 0x00), TAG, "check test BIAS_SENSN");
    ESP_RETURN_ON_ERROR(ads1299_check_reg(ADS1299_REG_LOFF_SENSP, 0x00), TAG, "check test LOFF_SENSP");
    ESP_RETURN_ON_ERROR(ads1299_check_reg(ADS1299_REG_LOFF_SENSN, 0x00), TAG, "check test LOFF_SENSN");
    ESP_RETURN_ON_ERROR(ads1299_check_reg(ADS1299_REG_CONFIG4, 0x00), TAG, "check test CONFIG4");
    ESP_LOGI(TAG, "ADS1299 internal-test register readback passed");
    return ESP_OK;
}

esp_err_t ads1299_apply_internal_short_config(void)
{
    if (!s_initialized) {
        return ESP_ERR_INVALID_STATE;
    }

    ESP_RETURN_ON_ERROR(ads1299_write_reg(ADS1299_REG_CONFIG1, 0xD4), TAG,
                        "write short CONFIG1");
    ESP_RETURN_ON_ERROR(ads1299_write_reg(ADS1299_REG_CONFIG2, 0xC0), TAG,
                        "write short CONFIG2");
    ESP_RETURN_ON_ERROR(ads1299_write_reg(ADS1299_REG_CONFIG3, 0xE0), TAG,
                        "write short CONFIG3");
    vTaskDelay(pdMS_TO_TICKS(ADS1299_INTERNAL_REF_STARTUP_MS));

    ESP_RETURN_ON_ERROR(ads1299_write_reg(ADS1299_REG_LOFF, 0x00), TAG,
                        "write short LOFF");
    for (uint8_t ch = 0; ch < 8; ++ch) {
        const uint8_t reg = (uint8_t)(ADS1299_REG_CH1SET + ch);
        ESP_RETURN_ON_ERROR(ads1299_write_reg(reg, ADS1299_CHSET_GAIN12_SHORT),
                            TAG, "write short CH%uSET", (unsigned)(ch + 1));
    }
    ESP_RETURN_ON_ERROR(ads1299_write_reg(ADS1299_REG_BIAS_SENSP, 0x00), TAG,
                        "write short BIAS_SENSP");
    ESP_RETURN_ON_ERROR(ads1299_write_reg(ADS1299_REG_BIAS_SENSN, 0x00), TAG,
                        "write short BIAS_SENSN");
    ESP_RETURN_ON_ERROR(ads1299_write_reg(ADS1299_REG_LOFF_SENSP, 0x00), TAG,
                        "write short LOFF_SENSP");
    ESP_RETURN_ON_ERROR(ads1299_write_reg(ADS1299_REG_LOFF_SENSN, 0x00), TAG,
                        "write short LOFF_SENSN");
    ESP_RETURN_ON_ERROR(ads1299_write_reg(ADS1299_REG_LOFF_FLIP, 0x00), TAG,
                        "write short LOFF_FLIP");
    ESP_RETURN_ON_ERROR(ads1299_write_reg(ADS1299_REG_MISC1, 0x00), TAG,
                        "write short MISC1");
    ESP_RETURN_ON_ERROR(ads1299_write_reg(ADS1299_REG_CONFIG4, 0x00), TAG,
                        "write short CONFIG4");

    ESP_LOGI(TAG, "ADS1299 internal-short register readback begin");
    ESP_RETURN_ON_ERROR(ads1299_check_reg(ADS1299_REG_CONFIG1, 0xD4), TAG,
                        "check short CONFIG1");
    ESP_RETURN_ON_ERROR(ads1299_check_reg(ADS1299_REG_CONFIG2, 0xC0), TAG,
                        "check short CONFIG2");
    ESP_RETURN_ON_ERROR(ads1299_check_reg_masked(ADS1299_REG_CONFIG3, 0xE0, 0xFE), TAG,
                        "check short CONFIG3");
    ESP_RETURN_ON_ERROR(ads1299_check_reg(ADS1299_REG_LOFF, 0x00), TAG,
                        "check short LOFF");
    for (uint8_t ch = 0; ch < 8; ++ch) {
        const uint8_t reg = (uint8_t)(ADS1299_REG_CH1SET + ch);
        ESP_RETURN_ON_ERROR(ads1299_check_reg(reg, ADS1299_CHSET_GAIN12_SHORT),
                            TAG, "check short CH%uSET", (unsigned)(ch + 1));
    }
    ESP_RETURN_ON_ERROR(ads1299_check_reg(ADS1299_REG_BIAS_SENSP, 0x00), TAG,
                        "check short BIAS_SENSP");
    ESP_RETURN_ON_ERROR(ads1299_check_reg(ADS1299_REG_BIAS_SENSN, 0x00), TAG,
                        "check short BIAS_SENSN");
    ESP_RETURN_ON_ERROR(ads1299_check_reg(ADS1299_REG_LOFF_SENSP, 0x00), TAG,
                        "check short LOFF_SENSP");
    ESP_RETURN_ON_ERROR(ads1299_check_reg(ADS1299_REG_LOFF_SENSN, 0x00), TAG,
                        "check short LOFF_SENSN");
    ESP_RETURN_ON_ERROR(ads1299_check_reg(ADS1299_REG_LOFF_FLIP, 0x00), TAG,
                        "check short LOFF_FLIP");
    ESP_RETURN_ON_ERROR(ads1299_check_reg(ADS1299_REG_MISC1, 0x00), TAG,
                        "check short MISC1");
    ESP_RETURN_ON_ERROR(ads1299_check_reg(ADS1299_REG_CONFIG4, 0x00), TAG,
                        "check short CONFIG4");
    ESP_LOGI(TAG, "ADS1299 INTERNAL SHORT configured: 8ch gain=12 mux=input-short");
    return ESP_OK;
}

static esp_err_t ads1299_configure_leadoff(uint8_t channel_mask,
                                           uint8_t loff_value,
                                           bool enable_comparators)
{
    if (!s_initialized) {
        return ESP_ERR_INVALID_STATE;
    }
    const uint8_t mask = (uint8_t)(channel_mask & s_config.enabled_channel_mask);
    if (mask == 0U) {
        return ESP_ERR_INVALID_ARG;
    }

    ESP_RETURN_ON_ERROR(ads1299_stop_continuous(), TAG, "stop before lead-off config");
    ESP_RETURN_ON_ERROR(ads1299_write_reg(
                            ADS1299_REG_CONFIG4,
                            enable_comparators ? ADS1299_CONFIG4_PD_LOFF_COMP : 0x00U),
                        TAG, "write lead-off CONFIG4");
    ESP_RETURN_ON_ERROR(ads1299_write_reg(ADS1299_REG_LOFF, loff_value),
                        TAG, "write lead-off LOFF");
    ESP_RETURN_ON_ERROR(ads1299_write_reg(ADS1299_REG_LOFF_FLIP, 0x00),
                        TAG, "write lead-off LOFF_FLIP");
    ESP_RETURN_ON_ERROR(ads1299_write_reg(ADS1299_REG_LOFF_SENSP, mask),
                        TAG, "write lead-off LOFF_SENSP");
    ESP_RETURN_ON_ERROR(ads1299_write_reg(ADS1299_REG_LOFF_SENSN, 0x00),
                        TAG, "write lead-off LOFF_SENSN");
    ESP_RETURN_ON_ERROR(ads1299_check_reg(ADS1299_REG_CONFIG4,
                                          enable_comparators ?
                                              ADS1299_CONFIG4_PD_LOFF_COMP : 0x00U),
                        TAG, "check lead-off CONFIG4");
    ESP_RETURN_ON_ERROR(ads1299_check_reg(ADS1299_REG_LOFF, loff_value),
                        TAG, "check lead-off LOFF");
    ESP_RETURN_ON_ERROR(ads1299_check_reg(ADS1299_REG_LOFF_SENSP, mask),
                        TAG, "check lead-off LOFF_SENSP");
    ESP_RETURN_ON_ERROR(ads1299_check_reg(ADS1299_REG_LOFF_SENSN, 0x00),
                        TAG, "check lead-off LOFF_SENSN");
    return ESP_OK;
}

esp_err_t ads1299_configure_leadoff_dc(uint8_t channel_mask)
{
    return ads1299_configure_leadoff(channel_mask, ADS1299_LOFF_DC_6NA, true);
}

esp_err_t ads1299_configure_leadoff_ac(uint8_t channel_mask)
{
    return ads1299_configure_leadoff(channel_mask, ADS1299_LOFF_AC_24NA_31HZ, false);
}

esp_err_t ads1299_read_leadoff_status(uint8_t channel_mask,
                                     ads1299_leadoff_result_t *result)
{
    if (!s_initialized) {
        return ESP_ERR_INVALID_STATE;
    }
    if (result == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    const uint8_t mask = (uint8_t)(channel_mask & s_config.enabled_channel_mask);
    if (mask == 0U) {
        return ESP_ERR_INVALID_ARG;
    }

    ESP_RETURN_ON_ERROR(ads1299_stop_continuous(), TAG, "stop before lead-off status");
    uint8_t statp = 0;
    uint8_t statn = 0;
    ESP_RETURN_ON_ERROR(ads1299_read_reg(ADS1299_REG_LOFF_STATP, &statp),
                        TAG, "read LOFF_STATP");
    ESP_RETURN_ON_ERROR(ads1299_read_reg(ADS1299_REG_LOFF_STATN, &statn),
                        TAG, "read LOFF_STATN");
    result->tested_channel_mask = mask;
    result->loff_statp = (uint8_t)(statp & mask);
    result->loff_statn = 0U;
    return ESP_OK;
}

esp_err_t ads1299_measure_leadoff(ads1299_leadoff_result_t *result)
{
    if (!s_initialized) {
        return ESP_ERR_INVALID_STATE;
    }
    if (result == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    const uint8_t mask = s_config.enabled_channel_mask;
    uint8_t old_loff = 0;
    uint8_t old_sensp = 0;
    uint8_t old_sensn = 0;
    uint8_t old_flip = 0;
    uint8_t old_config4 = 0;

    ESP_RETURN_ON_ERROR(ads1299_stop_continuous(), TAG, "stop before lead-off test");
    ESP_RETURN_ON_ERROR(ads1299_read_reg(ADS1299_REG_LOFF, &old_loff), TAG, "read LOFF");
    ESP_RETURN_ON_ERROR(ads1299_read_reg(ADS1299_REG_LOFF_SENSP, &old_sensp), TAG, "read LOFF_SENSP");
    ESP_RETURN_ON_ERROR(ads1299_read_reg(ADS1299_REG_LOFF_SENSN, &old_sensn), TAG, "read LOFF_SENSN");
    ESP_RETURN_ON_ERROR(ads1299_read_reg(ADS1299_REG_LOFF_FLIP, &old_flip), TAG, "read LOFF_FLIP");
    ESP_RETURN_ON_ERROR(ads1299_read_reg(ADS1299_REG_CONFIG4, &old_config4), TAG, "read CONFIG4");

    /* This project uses single-ended electrodes: each EEG electrode is on
     * CHnP while CHnN is intentionally not a per-channel electrode. Test P
     * inputs only. Enabling LOFF_SENSN would correctly report every unused N
     * input as open and therefore produce a false channel-fault result. */
    ESP_RETURN_ON_ERROR(ads1299_write_reg(ADS1299_REG_CONFIG4,
                                          (uint8_t)(old_config4 | ADS1299_CONFIG4_PD_LOFF_COMP)),
                        TAG, "enable lead-off comparators");
    ESP_RETURN_ON_ERROR(ads1299_write_reg(ADS1299_REG_LOFF, 0x00), TAG, "write LOFF");
    ESP_RETURN_ON_ERROR(ads1299_write_reg(ADS1299_REG_LOFF_FLIP, 0x00), TAG, "write LOFF_FLIP");
    ESP_RETURN_ON_ERROR(ads1299_write_reg(ADS1299_REG_LOFF_SENSP, mask), TAG, "write LOFF_SENSP");
    ESP_RETURN_ON_ERROR(ads1299_write_reg(ADS1299_REG_LOFF_SENSN, 0x00), TAG, "write LOFF_SENSN");

    ESP_RETURN_ON_ERROR(ads1299_start_continuous(), TAG, "start lead-off conversion");
    vTaskDelay(pdMS_TO_TICKS(ADS1299_LEADOFF_SETTLE_MS));

    uint8_t statp = 0;
    uint8_t statn = 0;
    esp_err_t err = ads1299_read_reg(ADS1299_REG_LOFF_STATP, &statp);
    if (err == ESP_OK) {
        err = ads1299_read_reg(ADS1299_REG_LOFF_STATN, &statn);
    }

    esp_err_t restore_err = ESP_OK;
    if (ads1299_write_reg(ADS1299_REG_LOFF_SENSP, old_sensp) != ESP_OK) {
        restore_err = ESP_FAIL;
    }
    if (ads1299_write_reg(ADS1299_REG_LOFF_SENSN, old_sensn) != ESP_OK) {
        restore_err = ESP_FAIL;
    }
    if (ads1299_write_reg(ADS1299_REG_LOFF_FLIP, old_flip) != ESP_OK) {
        restore_err = ESP_FAIL;
    }
    if (ads1299_write_reg(ADS1299_REG_LOFF, old_loff) != ESP_OK) {
        restore_err = ESP_FAIL;
    }
    if (ads1299_write_reg(ADS1299_REG_CONFIG4, old_config4) != ESP_OK) {
        restore_err = ESP_FAIL;
    }

    if (err != ESP_OK) {
        return err;
    }
    if (restore_err != ESP_OK) {
        return restore_err;
    }

    result->tested_channel_mask = mask;
    result->loff_statp = (uint8_t)(statp & mask);
    /* N-side lead-off is intentionally not tested in single-ended mode. */
    result->loff_statn = 0;
    ESP_LOGI(TAG, "ADS1299 single-ended lead-off: mask=0x%02x statp=0x%02x statn=ignored(0x%02x)",
             result->tested_channel_mask,
             result->loff_statp,
             statn);
    return ESP_OK;
}

esp_err_t ads1299_start_continuous(void)
{
    if (!s_initialized) {
        return ESP_ERR_INVALID_STATE;
    }
    ESP_LOGI(TAG, "ADS1299 START + RDATAC command sequence begin");
    ESP_RETURN_ON_ERROR(ads1299_send_cmd(ADS1299_CMD_START), TAG, "START failed");
    ESP_RETURN_ON_ERROR(ads1299_send_cmd(ADS1299_CMD_RDATAC), TAG, "RDATAC failed");
    s_rdatac = true;
    ESP_LOGI(TAG, "ADS1299 continuous conversion enabled");
    return ESP_OK;
}

esp_err_t ads1299_stop_continuous(void)
{
    if (!s_initialized) {
        return ESP_ERR_INVALID_STATE;
    }
    esp_err_t err = ads1299_send_cmd(ADS1299_CMD_SDATAC);
    if (err == ESP_OK) {
        err = ads1299_send_cmd(ADS1299_CMD_STOP);
    }
    s_rdatac = false;
    return err;
}

esp_err_t ads1299_init(const ads1299_config_t *config)
{
    if (config == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    s_config = *config;
    const uint32_t cs_hold_cycles = (uint32_t)(
        ((uint64_t)s_config.spi_clock_hz * ADS1299_CS_HOLD_US + 999999U) / 1000000U);
    ESP_RETURN_ON_FALSE(cs_hold_cycles >= 1U && cs_hold_cycles <= 16U,
                        ESP_ERR_INVALID_ARG, TAG, "SPI clock cannot provide ADS CS hold time");
    ESP_LOGI(TAG,
             "ADS1299 SPI bring-up: host=%s mode=1 cpol=0 cpha=1 speed=%" PRIu32
             "Hz CS=%d SCLK=%d MOSI=%d MISO=%d DRDY=%d RESET=%d START=%d PWDN=%d",
             ads1299_spi_host_name(),
             s_config.spi_clock_hz,
             s_config.board.ads_cs,
             s_config.board.ads_sclk,
             s_config.board.ads_mosi,
             s_config.board.ads_miso,
             s_config.board.ads_drdy,
             s_config.board.ads_reset,
             s_config.board.ads_start,
             s_config.board.ads_pwdn);

    if (gpio_is_valid(s_config.board.ads_reset)) {
        gpio_config_t reset_cfg = {
            .pin_bit_mask = BIT64(s_config.board.ads_reset),
            .mode = GPIO_MODE_OUTPUT,
            .pull_up_en = GPIO_PULLUP_DISABLE,
            .pull_down_en = GPIO_PULLDOWN_DISABLE,
            .intr_type = GPIO_INTR_DISABLE,
        };
        ESP_RETURN_ON_ERROR(gpio_config(&reset_cfg), TAG, "configure RESET");
        gpio_set_level(s_config.board.ads_reset, 1);
    }

    if (gpio_is_valid(s_config.board.ads_start)) {
        gpio_config_t start_cfg = {
            .pin_bit_mask = BIT64(s_config.board.ads_start),
            .mode = GPIO_MODE_OUTPUT,
            .pull_up_en = GPIO_PULLUP_DISABLE,
            .pull_down_en = GPIO_PULLDOWN_DISABLE,
            .intr_type = GPIO_INTR_DISABLE,
        };
        ESP_RETURN_ON_ERROR(gpio_config(&start_cfg), TAG, "configure START");
        gpio_set_level(s_config.board.ads_start, 0);
    }

    if (gpio_is_valid(s_config.board.ads_pwdn)) {
        gpio_config_t pwdn_cfg = {
            .pin_bit_mask = BIT64(s_config.board.ads_pwdn),
            .mode = GPIO_MODE_OUTPUT,
            .pull_up_en = GPIO_PULLUP_DISABLE,
            .pull_down_en = GPIO_PULLDOWN_DISABLE,
            .intr_type = GPIO_INTR_DISABLE,
        };
        ESP_RETURN_ON_ERROR(gpio_config(&pwdn_cfg), TAG, "configure PWDN");
        gpio_set_level(s_config.board.ads_pwdn, 1);
    }

    if (gpio_is_valid(s_config.board.ads_drdy)) {
        gpio_config_t drdy_cfg = {
            .pin_bit_mask = BIT64(s_config.board.ads_drdy),
            .mode = GPIO_MODE_INPUT,
            .pull_up_en = GPIO_PULLUP_ENABLE,
            .pull_down_en = GPIO_PULLDOWN_DISABLE,
            .intr_type = GPIO_INTR_DISABLE,
        };
        ESP_RETURN_ON_ERROR(gpio_config(&drdy_cfg), TAG, "configure DRDY");
    }

    spi_bus_config_t bus_cfg = {
        .mosi_io_num = s_config.board.ads_mosi,
        .miso_io_num = s_config.board.ads_miso,
        .sclk_io_num = s_config.board.ads_sclk,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = 64,
    };
    esp_err_t err = spi_bus_initialize(ADS1299_SPI_HOST, &bus_cfg, SPI_DMA_CH_AUTO);
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        ESP_RETURN_ON_ERROR(err, TAG, "initialize SPI bus");
    }
    ESP_LOGI(TAG, "ADS1299 SPI bus initialized: mosi=%d miso=%d sclk=%d dma=auto max_transfer=%d",
             s_config.board.ads_mosi,
             s_config.board.ads_miso,
             s_config.board.ads_sclk,
             bus_cfg.max_transfer_sz);

    spi_device_interface_config_t dev_cfg = {
        .clock_speed_hz = (int)s_config.spi_clock_hz,
        .mode = 1,
        .spics_io_num = s_config.board.ads_cs,
        .cs_ena_posttrans = (uint8_t)cs_hold_cycles,
        .queue_size = 1,
    };
    ESP_RETURN_ON_ERROR(spi_bus_add_device(ADS1299_SPI_HOST, &dev_cfg, &s_spi), TAG, "add SPI device");
    ESP_LOGI(TAG, "ADS1299 SPI device added: cs=%d mode=%d clock=%" PRIu32 "Hz queue=%d cs_hold=%u bit-cycles",
             s_config.board.ads_cs,
             dev_cfg.mode,
             s_config.spi_clock_hz,
             dev_cfg.queue_size,
             (unsigned)dev_cfg.cs_ena_posttrans);

    s_initialized = true;
    s_rdatac = false;

    if (gpio_is_valid(s_config.board.ads_reset)) {
        ESP_LOGI(TAG, "ADS1299 hardware reset pulse on GPIO%d", s_config.board.ads_reset);
        gpio_set_level(s_config.board.ads_reset, 0);
        vTaskDelay(pdMS_TO_TICKS(2));
        gpio_set_level(s_config.board.ads_reset, 1);
        vTaskDelay(pdMS_TO_TICKS(20));
    } else {
        ESP_LOGI(TAG, "ADS1299 software RESET command");
        ESP_RETURN_ON_ERROR(ads1299_send_cmd(ADS1299_CMD_RESET), TAG, "RESET command failed");
        vTaskDelay(pdMS_TO_TICKS(20));
    }

    if (gpio_is_valid(s_config.board.ads_drdy)) {
        ESP_LOGI(TAG, "ADS1299 DRDY initial level after reset: gpio=%d level=%d",
                 s_config.board.ads_drdy,
                 gpio_get_level(s_config.board.ads_drdy));
    }

    ESP_RETURN_ON_ERROR(ads1299_send_cmd(ADS1299_CMD_SDATAC), TAG, "SDATAC failed");

    uint8_t ids[ADS1299_ID_READ_ATTEMPTS] = {0};
    for (uint8_t i = 0; i < ADS1299_ID_READ_ATTEMPTS; ++i) {
        err = ads1299_read_id(&ids[i]);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "ADS1299 ID read attempt %u failed: %s", (unsigned)(i + 1), esp_err_to_name(err));
            s_initialized = false;
            return err;
        }
        ESP_LOGI(TAG,
                 "ADS1299 ID read attempt %u: id=0x%02x",
                 (unsigned)(i + 1),
                 ids[i]);
    }

    if (ids[0] != ids[1] || ids[1] != ids[2]) {
        ESP_LOGE(TAG, "ADS1299 ID is unstable: 0x%02x 0x%02x 0x%02x; check MISO/CS/SCLK wiring",
                 ids[0], ids[1], ids[2]);
        s_initialized = false;
        return ESP_ERR_INVALID_RESPONSE;
    }
    if ((ids[0] & ADS1299_ID_DEVICE_MASK) != ADS1299_ID_8_CHANNEL) {
        ESP_LOGE(TAG, "ADS1299 ID invalid: 0x%02x; expected (ID & 0x%02x)=0x%02x; "
                 "check ADS power/CLKSEL/RESET/PWDN/CS/SCLK/MOSI/MISO/GND",
                 ids[0], ADS1299_ID_DEVICE_MASK, ADS1299_ID_8_CHANNEL);
        s_initialized = false;
        return ESP_ERR_INVALID_RESPONSE;
    }
    ESP_LOGI(TAG, "ADS1299 SPI ID test passed: id=0x%02x ch_mask=0x%02x spi=%" PRIu32 "Hz",
             ids[0], s_config.enabled_channel_mask, s_config.spi_clock_hz);

    ESP_LOGI(TAG, "ADS1299 SPI bring-up complete; acquisition may start after register readback");
    return ESP_OK;
}

esp_err_t ads1299_read_frame(ads1299_frame_t *frame)
{
    if (!s_initialized) {
        return ESP_ERR_INVALID_STATE;
    }
    if (frame == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    uint8_t tx[1 + ADS1299_FRAME_BYTES] = {0};
    uint8_t rx[sizeof(tx)] = {0};
    size_t offset = 0;
    size_t len = ADS1299_FRAME_BYTES;
    if (!s_rdatac) {
        tx[0] = ADS1299_CMD_RDATA;
        offset = 1;
        len = sizeof(tx);
    }

    ESP_RETURN_ON_ERROR(ads1299_spi_transfer(tx, rx, len), TAG, "read data frame failed");

    const uint8_t *data = &rx[offset];
    memcpy(frame->status, data, sizeof(frame->status));
    data += 3;

    for (uint8_t ch = 0; ch < 8; ++ch) {
        const uint32_t raw = ((uint32_t)data[0] << 16) | ((uint32_t)data[1] << 8) | data[2];
        frame->channel[ch] = sign_extend_24(raw);
        data += 3;
    }

    return ESP_OK;
}

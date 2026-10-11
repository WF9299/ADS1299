#include "eeg_drdy_monitor.h"

#include "driver/pulse_cnt.h"

#define EEG_DRDY_PCNT_LOW_LIMIT (-1)
#define EEG_DRDY_PCNT_HIGH_LIMIT 30000

static pcnt_unit_handle_t s_unit;
static pcnt_channel_handle_t s_channel;
static bool s_high_watch_added;
static bool s_low_watch_added;
static bool s_enabled;
static bool s_started;

esp_err_t eeg_drdy_monitor_deinit(void)
{
    esp_err_t first_error = ESP_OK;

    if (s_started) {
        const esp_err_t err = pcnt_unit_stop(s_unit);
        if (first_error == ESP_OK && err != ESP_OK) {
            first_error = err;
        }
        s_started = false;
    }
    if (s_enabled) {
        const esp_err_t err = pcnt_unit_disable(s_unit);
        if (first_error == ESP_OK && err != ESP_OK) {
            first_error = err;
        }
        s_enabled = false;
    }
    if (s_channel != NULL) {
        const esp_err_t err = pcnt_del_channel(s_channel);
        if (first_error == ESP_OK && err != ESP_OK) {
            first_error = err;
        }
        s_channel = NULL;
    }
    if (s_unit != NULL && s_high_watch_added) {
        const esp_err_t err = pcnt_unit_remove_watch_point(s_unit, EEG_DRDY_PCNT_HIGH_LIMIT);
        if (first_error == ESP_OK && err != ESP_OK) {
            first_error = err;
        }
        s_high_watch_added = false;
    }
    if (s_unit != NULL && s_low_watch_added) {
        const esp_err_t err = pcnt_unit_remove_watch_point(s_unit, EEG_DRDY_PCNT_LOW_LIMIT);
        if (first_error == ESP_OK && err != ESP_OK) {
            first_error = err;
        }
        s_low_watch_added = false;
    }
    if (s_unit != NULL) {
        const esp_err_t err = pcnt_del_unit(s_unit);
        if (first_error == ESP_OK && err != ESP_OK) {
            first_error = err;
        }
        s_unit = NULL;
    }

    return first_error;
}

esp_err_t eeg_drdy_monitor_init(int gpio_num)
{
    if (gpio_num < 0) {
        return ESP_ERR_INVALID_ARG;
    }
    if (s_unit != NULL || s_channel != NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    const pcnt_unit_config_t unit_config = {
        .low_limit = EEG_DRDY_PCNT_LOW_LIMIT,
        .high_limit = EEG_DRDY_PCNT_HIGH_LIMIT,
        .flags.accum_count = 1,
    };
    esp_err_t err = pcnt_new_unit(&unit_config, &s_unit);
    if (err != ESP_OK) {
        goto fail;
    }

    const pcnt_chan_config_t channel_config = {
        .edge_gpio_num = gpio_num,
        .level_gpio_num = -1,
    };
    err = pcnt_new_channel(s_unit, &channel_config, &s_channel);
    if (err != ESP_OK) {
        goto fail;
    }
    err = pcnt_channel_set_edge_action(s_channel,
                                       PCNT_CHANNEL_EDGE_ACTION_HOLD,
                                       PCNT_CHANNEL_EDGE_ACTION_INCREASE);
    if (err != ESP_OK) {
        goto fail;
    }
    err = pcnt_channel_set_level_action(s_channel,
                                        PCNT_CHANNEL_LEVEL_ACTION_KEEP,
                                        PCNT_CHANNEL_LEVEL_ACTION_KEEP);
    if (err != ESP_OK) {
        goto fail;
    }

    err = pcnt_unit_add_watch_point(s_unit, EEG_DRDY_PCNT_HIGH_LIMIT);
    if (err != ESP_OK) {
        goto fail;
    }
    s_high_watch_added = true;
    err = pcnt_unit_add_watch_point(s_unit, EEG_DRDY_PCNT_LOW_LIMIT);
    if (err != ESP_OK) {
        goto fail;
    }
    s_low_watch_added = true;

    err = pcnt_unit_enable(s_unit);
    if (err != ESP_OK) {
        goto fail;
    }
    s_enabled = true;
    err = pcnt_unit_clear_count(s_unit);
    if (err != ESP_OK) {
        goto fail;
    }
    err = pcnt_unit_start(s_unit);
    if (err != ESP_OK) {
        goto fail;
    }
    s_started = true;
    return ESP_OK;

fail:
    eeg_drdy_monitor_deinit();
    return err;
}

esp_err_t eeg_drdy_monitor_reset(void)
{
    if (!eeg_drdy_monitor_is_available()) {
        return ESP_ERR_INVALID_STATE;
    }
    return pcnt_unit_clear_count(s_unit);
}

esp_err_t eeg_drdy_monitor_take_count(uint32_t *count)
{
    if (count == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (!eeg_drdy_monitor_is_available()) {
        return ESP_ERR_INVALID_STATE;
    }

    int raw_count = 0;
    esp_err_t err = pcnt_unit_get_count(s_unit, &raw_count);
    if (err != ESP_OK) {
        return err;
    }
    if (raw_count < 0) {
        return ESP_FAIL;
    }
    err = pcnt_unit_clear_count(s_unit);
    if (err != ESP_OK) {
        return err;
    }
    *count = (uint32_t)raw_count;
    return ESP_OK;
}

bool eeg_drdy_monitor_is_available(void)
{
    return s_unit != NULL && s_channel != NULL && s_enabled && s_started;
}

static void check_state(uint8_t mask) {
    assert(regs[ADS1299_REG_CONFIG3] == (mask ? 0xec : 0xe8));
    assert(regs[ADS1299_REG_BIAS_SENSP] == mask);
    assert(regs[ADS1299_REG_BIAS_SENSN] == 0);
    assert(regs[ADS1299_REG_MISC1] == 0x20);
    for(unsigned c=0; c<8; ++c) assert(regs[ADS1299_REG_CH1SET+c]==0x50);
}
int main(void) {
    assert(ads1299_apply_verified_reference_config_with_bias(255)==ESP_OK);
    check_state(255);
    uint8_t original[32]; memcpy(original, regs, sizeof(regs));
    assert(eeg_acq_set_normal_bias_mask(0)==EEG_ACQ_BIAS_SWITCH_OK);
    check_state(0);
    for(unsigned r=0; r<32; ++r) if(r!=3 && r!=13 && r!=14) assert(regs[r]==original[r]);
    assert(s_normal_bias_mask==0 && s_normal_bias_verified && !s_paused);
    /* Normal-mode restore used by impedance, self-test exit and restart. */
    memset(regs, 0xa5, sizeof(regs));
    assert(ads1299_apply_verified_reference_config_with_bias(s_normal_bias_mask)==ESP_OK);
    check_state(0);
    int before=resumes;
    assert(eeg_acq_set_normal_bias_mask(0)==EEG_ACQ_BIAS_SWITCH_OK);
    assert(resumes==before);
    /* Failed enable must roll back to verified OFF. */
    write_fail_reg=ADS1299_REG_BIAS_SENSP; write_fail_count=1;
    assert(eeg_acq_set_normal_bias_mask(0x35)==EEG_ACQ_BIAS_SWITCH_APPLY_FAILED);
    check_state(0);
    assert(s_normal_bias_mask==0 && s_normal_bias_verified && !s_paused);
    write_fail_reg=-1;
    resume_fail_count=1;
    assert(eeg_acq_set_normal_bias_mask(0x35)==EEG_ACQ_BIAS_SWITCH_APPLY_FAILED);
    check_state(0);
    assert(s_normal_bias_mask==0 && s_normal_bias_verified && !s_paused);
    assert(eeg_acq_set_normal_bias_mask(0x35)==EEG_ACQ_BIAS_SWITCH_OK);
    check_state(0x35);
    /* Verification failure after writing OFF restores all prior registers. */
    read_fail_reg=ADS1299_REG_CONFIG3; read_fail_count=1;
    assert(eeg_acq_set_normal_bias_mask(0)==EEG_ACQ_BIAS_SWITCH_APPLY_FAILED);
    check_state(0x35);
    assert(s_normal_bias_mask==0x35 && s_normal_bias_verified);
    read_fail_reg=-1;
    pause_error=ESP_ERR_TIMEOUT;
    assert(eeg_acq_set_normal_bias_mask(0)==EEG_ACQ_BIAS_SWITCH_PAUSE_TIMEOUT);
    check_state(0x35);
    pause_error=0;
    /* Repeated IO failure: no claim of verified OFF, keep acquisition paused. */
    write_fail_reg=ADS1299_REG_BIAS_SENSP; write_fail_count=10;
    assert(eeg_acq_set_normal_bias_mask(0)==EEG_ACQ_BIAS_SWITCH_RESTORE_FAILED);
    assert(!s_normal_bias_verified && s_paused && s_pause_requested);
    assert(s_normal_bias_mask==0x35);
    assert(clears>0);
    puts("PASS: production BIAS register and acquisition fault injection");
    return 0;
}

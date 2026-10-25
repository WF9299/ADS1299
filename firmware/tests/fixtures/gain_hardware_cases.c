static void check_normal(uint8_t gain, uint8_t bias) {
    uint8_t bits;
    assert(ads1299_gain_bits(gain,&bits)==ESP_OK);
    for(int ch=0; ch<8; ++ch) assert(regs[ADS1299_REG_CH1SET+ch]==bits);
    assert(regs[ADS1299_REG_BIAS_SENSP]==bias);
    assert(regs[ADS1299_REG_CONFIG3]==(bias ? 0xec : 0xe8));
    assert(regs[ADS1299_REG_MISC1]==0x20);
    assert(s_normal_gain==gain && s_gain_verified);
}
static void initialize(uint8_t gain, uint8_t bias) {
    write_fail_reg=read_fail_reg=snapshot_fail_reg=-1;
    write_fail_count=read_fail_count=resume_fail_count=pause_error=0;
    s_paused=s_pause_requested=false; s_config_id=EEG_ACQ_CONFIG_NORMAL;
    s_normal_gain=gain; s_normal_bias_mask=bias; s_normal_bias_verified=true;
    s_discard_samples=EEG_ACQ_MODE_SETTLE_SAMPLES;
    assert(apply_ads1299_capture_config()==ESP_OK);
}
int main(void) {
    const uint8_t gains[]={1,2,4,6,8,12,24};
    const uint8_t masks[]={0,0x3f};
    for(unsigned b=0;b<2;++b) for(unsigned g=0;g<7;++g) {
        initialize(12,masks[b]);
        uint8_t bits=255;
        assert(ads1299_gain_bits(gains[g],&bits)==ESP_OK && bits==(g<<4));
        assert(eeg_acq_set_normal_gain(gains[g])==EEG_ACQ_GAIN_SWITCH_OK);
        check_normal(gains[g],masks[b]);
        int previous=resumes;
        assert(eeg_acq_set_normal_gain(gains[g])==EEG_ACQ_GAIN_SWITCH_OK);
        assert(resumes==previous);
        assert(eeg_acq_set_config_id(EEG_ACQ_CONFIG_INTERNAL_SHORT)==EEG_ACQ_MODE_SWITCH_OK);
        for(int ch=0;ch<8;++ch) assert(regs[ADS1299_REG_CH1SET+ch]==0x51);
        assert(s_normal_gain==gains[g]);
        assert(eeg_acq_set_config_id(EEG_ACQ_CONFIG_NORMAL)==EEG_ACQ_MODE_SWITCH_OK);
        check_normal(gains[g],masks[b]);
        /* BIAS apply and failed BIAS rollback both retain the chosen gain. */
        assert(eeg_acq_set_normal_bias_mask(masks[1-b])==EEG_ACQ_BIAS_SWITCH_OK);
        check_normal(gains[g],masks[1-b]);
        write_fail_reg=ADS1299_REG_BIAS_SENSP; write_fail_count=1;
        assert(eeg_acq_set_normal_bias_mask(masks[b])==EEG_ACQ_BIAS_SWITCH_APPLY_FAILED);
        check_normal(gains[g],masks[1-b]);
    }
    initialize(8,0x3f);
    uint8_t before[32];
    for(int ch=0;ch<8;++ch) regs[ADS1299_REG_CH1SET+ch]=0x40|(ch & 7)|((ch & 1)?0x88:0);
    memcpy(before,regs,sizeof(regs));
    assert(ads1299_set_gain_verified(24)==ESP_OK);
    for(int r=0;r<32;++r) {
        if(r>=ADS1299_REG_CH1SET && r<ADS1299_REG_CH1SET+8)
            assert(regs[r]==((before[r]&0x8f)|0x60));
        else assert(regs[r]==before[r]);
    }
    for(int ch=0;ch<8;++ch) {
        for(int phase=0;phase<3;++phase) {
            initialize(8,0x3f);
            if(phase==0) { write_fail_reg=ADS1299_REG_CH1SET+ch; write_fail_count=1; }
            if(phase==1) { read_fail_reg=ADS1299_REG_CH1SET+ch; read_fail_count=1; }
            if(phase==2) snapshot_fail_reg=ADS1299_REG_CH1SET+ch;
            assert(eeg_acq_set_normal_gain(24)==EEG_ACQ_GAIN_SWITCH_APPLY_FAILED);
            check_normal(8,0x3f);
            assert(!s_paused && !s_pause_requested);
        }
    }
    initialize(4,0);
    resume_fail_count=1;
    assert(eeg_acq_set_normal_gain(24)==EEG_ACQ_GAIN_SWITCH_APPLY_FAILED);
    check_normal(4,0);
    initialize(6,0x3f);
    write_fail_reg=ADS1299_REG_CH1SET+3; write_fail_count=10;
    assert(eeg_acq_set_normal_gain(24)==EEG_ACQ_GAIN_SWITCH_RESTORE_FAILED);
    assert(s_normal_gain==6 && !s_gain_verified && !s_normal_bias_verified);
    assert(s_paused && s_pause_requested);
    initialize(8,0x3f);
    pause_error=ESP_ERR_TIMEOUT;
    assert(eeg_acq_set_normal_gain(24)==EEG_ACQ_GAIN_SWITCH_APPLY_FAILED);
    check_normal(8,0x3f);
    uint8_t bits;
    assert(ads1299_gain_bits(3,&bits)==ESP_ERR_INVALID_ARG);
    assert(eeg_acq_set_normal_gain(0)==EEG_ACQ_GAIN_SWITCH_INVALID_REQUEST);
    for(unsigned g=0;g<7;++g) for(int failure=0;failure<5;++failure) {
        initialize(gains[g],0x3f);
        measure_phase=0; fail_measure_phase=failure; fail_dc=(failure==4);
        eeg_acq_impedance_result_t measured;
        assert(eeg_acq_measure_impedance(0x3f,&measured)==ESP_OK);
        assert(measured.status==(failure==0 ? EEG_ACQ_IMPEDANCE_STATUS_OK :
            (failure==4 ? EEG_ACQ_IMPEDANCE_STATUS_CONFIG_FAILED : EEG_ACQ_IMPEDANCE_STATUS_SAMPLE_TIMEOUT)));
        check_normal(gains[g],0x3f);
    }
    puts("PASS: seven gains, preserved fields, 24 channel IO failures, rollback, BIAS and short restore");
    return 0;
}

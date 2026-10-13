#include "serial_protocol.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
static unsigned count;
static void receive(uint8_t type,uint32_t id,const uint8_t *payload,size_t size,void *ctx)
{
    (void)ctx;
    assert(type==SERIAL_EEG && id==0x10203040 && size==5 && !memcmp(payload,"hello",5));
    count++;
}
int main(int argc,char **argv)
{
    uint8_t frame[SERIAL_FRAME_MAX];
    size_t n=serial_encode(SERIAL_EEG,0x10203040,(const uint8_t *)"hello",5,frame,sizeof(frame));
    assert(n==21 && serial_crc32c((const uint8_t *)"123456789",9)==0xe3069283U);
    if (argc>1 && !strcmp(argv[1],"--golden")) {
        for (size_t i=0;i<n;i++) printf("%02x",frame[i]);
        puts(""); return 0;
    }
    serial_parser_t parser={0};
    for (size_t i=0;i<n;i++) serial_parser_feed(&parser,frame+i,1,receive,NULL);
    assert(count==1);
    uint8_t broken[21]; memcpy(broken,frame,n); broken[20]^=1;
    serial_parser_feed(&parser,broken,n,receive,NULL);
    serial_parser_feed(&parser,frame,n,receive,NULL);
    assert(count==2 && parser.errors>0);
    uint8_t invalid[12]={0xa5,0xc3,1,6,0,0,0,1,0xff,0xff,0xff,0xff};
    serial_parser_feed(&parser,invalid,sizeof(invalid),receive,NULL);
    serial_parser_feed(&parser,frame,n,receive,NULL);
    assert(count==3);
    assert(serial_encode(6,1,frame,SERIAL_PAYLOAD_MAX+1,frame,sizeof(frame))==0);
    puts("PASS: serial C framing, CRC, fragmentation and resynchronization");
    return 0;
}

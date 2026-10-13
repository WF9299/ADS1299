#include "serial_protocol.h"
#include <string.h>
static uint32_t u32(const uint8_t *p)
{ return (uint32_t)p[0]<<24 | (uint32_t)p[1]<<16 | (uint32_t)p[2]<<8 | p[3]; }
static void put32(uint8_t *p, uint32_t v)
{ p[0]=v>>24; p[1]=v>>16; p[2]=v>>8; p[3]=v; }
uint32_t serial_crc32c(const uint8_t *data, size_t size)
{
    uint32_t crc=UINT32_MAX;
    for (size_t i=0;i<size;i++) {
        crc ^= data[i];
        for (unsigned j=0;j<8;j++) crc=(crc>>1)^((crc&1)?0x82f63b78U:0U);
    }
    return ~crc;
}
size_t serial_encode(uint8_t type, uint32_t id, const uint8_t *payload,
                     size_t size, uint8_t *out, size_t capacity)
{
    if (type<1 || type>11 || size>SERIAL_PAYLOAD_MAX || capacity<size+16 ||
        out==NULL || (size && payload==NULL)) return 0;
    out[0]=0xa5; out[1]=0xc3; out[2]=1; out[3]=type;
    put32(out+4,id); put32(out+8,(uint32_t)size);
    if (size) memcpy(out+12,payload,size);
    put32(out+12+size,serial_crc32c(out,12+size));
    return size+16;
}
static void discard(serial_parser_t *p, size_t n)
{ memmove(p->data,p->data+n,p->used-n); p->used-=n; }
void serial_parser_feed(serial_parser_t *p, const uint8_t *data, size_t size,
                        serial_frame_fn callback, void *ctx)
{
    if (!p || !data || !callback) return;
    for (size_t i=0;i<size;i++) {
        if (p->used==sizeof(p->data)) { discard(p,1); p->errors++; }
        p->data[p->used++]=data[i];
        while (p->used>=2) {
            if (p->data[0]!=0xa5 || p->data[1]!=0xc3) { discard(p,1); continue; }
            if (p->used<12) break;
            size_t n=u32(p->data+8);
            if (p->data[2]!=1 || p->data[3]<1 || p->data[3]>11 || n>SERIAL_PAYLOAD_MAX) {
                p->errors++; discard(p,1); continue;
            }
            if (p->used<n+16) break;
            if (u32(p->data+12+n)!=serial_crc32c(p->data,12+n)) {
                p->errors++; discard(p,1); continue;
            }
            callback(p->data[3],u32(p->data+4),p->data+12,n,ctx);
            discard(p,n+16);
        }
    }
}

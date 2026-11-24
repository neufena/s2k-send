// Prints the wire bytes produced by michaelsanois's hardware-proven S2000 Bridge C++ code (see NOTICE) so that
// tests/parity.py can compare them with s2ksend.py. Built against the original release
// headers (not shipped here): g++ -std=c++17 -I<payload> -I<payload>/vendor tests/parity.cpp
#include "NativeQueries.h"
#include "NativePcmReadback.h"
#include <cstdio>
using namespace nativeSend;
static void out(const char* k, const Bytes& b){printf("%s ",k);for(auto v:b)printf("%02x",v);printf("\n");}
static void outc(const char* k, const std::array<uint8_t,6>& c){out(k,Bytes(c.begin(),c.end()));}
int main(){
  // sample header + wire encoding (name, root, rate, frames from prepareSample; identifier 1)
  std::vector<int16_t> pcm(30000,0);
  auto s=BundlePreparation::prepareSample(capturedWireTemplates().sample,"TEST TONE-1",60,44100,pcm);
  out("header",Bytes(s.header.begin(),s.header.end()));
  out("sysex_sample",Codec::encode(Codec::Kind::Sample,s.header,1));
  out("post",Codec::postObject(0));
  outc("cdb_send392",Codec::transferCdb(12,392));
  outc("cdb_pcm16384",Codec::transferCdb(12,16384,true));
  outc("cdb_recv141",Codec::transferCdb(14,141));
  // header read requests at offsets 0/64/128 (op 44 => request opcode 43)
  HeaderAssembly h(44,5);
  for(int i=0;i<3;++i){
    char k[32];snprintf(k,sizeof k,"hdrreq%d",i);out(k,h.request());
    Bytes f{240,71,0,44,72,5,0,0,uint8_t((i*64)&127),uint8_t((i*64)>>7),64,0};
    f.insert(f.end(),128,0);f.push_back(247);h.append(f);
  }
  // PCM readback request: Mode -> Send
  PcmReadback r(5,Bytes(60000,0));
  r.accept({Completion::Good,size_t(0),{}},0.1);
  out("pcmreq",r.next().payload);
  // inventory requests (program list, sample list, STAT)
  for(int part=0;part<3;++part){
    InventoryRead inv(1);
    // inventory starts with Mode; its Send payload is the request
    inv.accept({Completion::Good,size_t(0),{}},0.1);
    char k[32];snprintf(k,sizeof k,"inv%d",part);out(k,inv.next().payload);
    (void)part;
  }
}

FFmpeg 64-bit static Windows build from www.gyan.dev

Version: 2026-06-15-git-44d082edc8-essentials_build-www.gyan.dev

License: GPL v3

Source Code: https://github.com/FFmpeg/FFmpeg/commit/44d082edc8

git-essentials build configuration: 

ARCH                      x86 (generic)
big-endian                no
runtime cpu detection     yes
standalone assembly       yes
x86 assembler             nasm
MMX enabled               yes
MMXEXT enabled            yes
SSE enabled               yes
SSSE3 enabled             yes
AESNI enabled             yes
CLMUL enabled             yes
AVX enabled               yes
AVX2 enabled              yes
AVX-512 enabled           yes
AVX-512ICL enabled        yes
XOP enabled               yes
FMA3 enabled              yes
FMA4 enabled              yes
i686 features enabled     yes
CMOV is fast              yes
EBX available             yes
EBP available             yes
debug symbols             yes
strip symbols             yes
optimize for size         no
optimizations             yes
static                    yes
shared                    no
network support           yes
threading support         pthreads
safe bitstream reader     yes
texi2html enabled         no
perl enabled              yes
pod2man enabled           yes
makeinfo enabled          yes
makeinfo supports HTML    yes
experimental features     yes
xmllint enabled           yes

External libraries:
avisynth                libmp3lame              libvorbis
bzlib                   libopencore_amrnb       libvpx
cairo                   libopencore_amrwb       libwebp
gmp                     libopenjpeg             libx264
gnutls                  libopenmpt              libx265
iconv                   libopus                 libxml2
libaom                  librubberband           libxvid
libass                  libspeex                libzimg
libfontconfig           libsrt                  libzmq
libfreetype             libssh                  lzma
libfribidi              libtheora               mediafoundation
libgme                  libvidstab              openal
libgsm                  libvmaf                 sdl2
libharfbuzz             libvo_amrwbenc          zlib

External libraries providing hardware acceleration:
amf                     d3d12va                 nvdec
cuda                    dxva2                   nvenc
cuda_llvm               ffnvcodec               vaapi
cuvid                   libmfx
d3d11va                 libvpl

Libraries:
avcodec                 avformat                swscale
avdevice                avutil
avfilter                swresample

Programs:
ffmpeg                  ffplay                  ffprobe

Enabled decoders:
aac                     flashsv                 pdv
aac_fixed               flashsv2                pfm
aac_latm                flic                    pgm
aasc                    flv                     pgmyuv
ac3                     fmvc                    pgssub
ac3_fixed               fourxm                  pgx
acelp_kelvin            fraps                   phm
adpcm_4xm               frwu                    photocd
adpcm_adx               ftr                     pictor
adpcm_afc               g2m                     pixlet
adpcm_agm               g723_1                  pjs
adpcm_aica              g728                    png
adpcm_argo              g729                    ppm
adpcm_circus            gdv                     prores
adpcm_ct                gem                     prores_raw
adpcm_dtk               gif                     prosumer
adpcm_ea                gremlin_dpcm            psd
adpcm_ea_maxis_xa       gsm                     ptx
adpcm_ea_r1             gsm_ms                  qcelp
adpcm_ea_r2             h261                    qdm2
adpcm_ea_r3             h263                    qdmc
adpcm_ea_xas            h263i                   qdraw
adpcm_g722              h263p                   qoa
adpcm_g726              h264                    qoi
adpcm_g726le            h264_amf                qpeg
adpcm_ima_acorn         h264_cuvid              qtrle
adpcm_ima_alp           h264_qsv                r10k
adpcm_ima_amv           hap                     r210
adpcm_ima_apc           hca                     ra_144
adpcm_ima_apm           hcom                    ra_288
adpcm_ima_cunning       hdr                     ralf
adpcm_ima_dat4          hevc                    rasc
adpcm_ima_dk3           hevc_amf                rawvideo
adpcm_ima_dk4           hevc_cuvid              realtext
adpcm_ima_ea_eacs       hevc_qsv                rka
adpcm_ima_ea_sead       hnm4_video              rl2
adpcm_ima_escape        hq_hqa                  roq
adpcm_ima_hvqm2         hqx                     roq_dpcm
adpcm_ima_hvqm4         huffyuv                 rpza
adpcm_ima_iss           hymt                    rscc
adpcm_ima_magix         iac                     rtv1
adpcm_ima_moflex        idcin                   rv10
adpcm_ima_mtf           idf                     rv20
adpcm_ima_oki           iff_ilbm                rv30
adpcm_ima_pda           ilbc                    rv40
adpcm_ima_qt            imc                     rv60
adpcm_ima_rad           imm4                    s302m
adpcm_ima_smjpeg        imm5                    sami
adpcm_ima_ssi           indeo2                  sanm
adpcm_ima_wav           indeo3                  sbc
adpcm_ima_ws            indeo4                  scpr
adpcm_ima_xbox          indeo5                  screenpresso
adpcm_ms                interplay_acm           sdx2_dpcm
adpcm_mtaf              interplay_dpcm          sga
adpcm_n64               interplay_video         sgi
adpcm_psx               ipu                     sgirle
adpcm_psxc              jacosub                 sheervideo
adpcm_sanyo             jpeg2000                shorten
adpcm_sbpro_2           jpegls                  simbiosis_imx
adpcm_sbpro_3           jv                      sipr
adpcm_sbpro_4           kgv1                    siren
adpcm_swf               kmvc                    smackaud
adpcm_thp               lagarith                smacker
adpcm_thp_le            lead                    smc
adpcm_vima              libaom_av1              smvjpeg
adpcm_xa                libgsm                  snow
adpcm_xmd               libgsm_ms               sol_dpcm
adpcm_yamaha            libopencore_amrnb       sonic
adpcm_zork              libopencore_amrwb       sp5x
agm                     libopus                 speedhq
ahx                     libspeex                speex
aic                     libvorbis               srgc
alac                    libvpx_vp8              srt
alias_pix               libvpx_vp9              ssa
als                     loco                    stl
amrnb                   lscr                    subrip
amrwb                   m101                    subviewer
amv                     mace3                   subviewer1
anm                     mace6                   sunrast
ansi                    magicyuv                svq1
anull                   mdec                    svq3
apac                    media100                tak
ape                     metasound               targa
apng                    microdvd                targa_y216
aptx                    mimic                   tdsc
aptx_hd                 misc4                   text
apv                     mjpeg                   theora
arbc                    mjpeg_cuvid             thp
argo                    mjpeg_qsv               tiertexseqvideo
ass                     mjpegb                  tiff
asv1                    mlp                     tmv
asv2                    mmvideo                 truehd
atrac1                  mobiclip                truemotion1
atrac3                  motionpixels            truemotion2
atrac3al                movtext                 truemotion2rt
atrac3p                 mp1                     truespeech
atrac3pal               mp1float                tscc
atrac9                  mp2                     tscc2
aura                    mp2float                tta
aura2                   mp3                     twinvq
av1                     mp3adu                  txd
av1_amf                 mp3adufloat             ulti
av1_cuvid               mp3float                utvideo
av1_qsv                 mp3on4                  v210
avrn                    mp3on4float             v210x
avrp                    mpc7                    v308
avs                     mpc8                    v408
avui                    mpeg1_cuvid             v410
bethsoftvid             mpeg1video              vb
bfi                     mpeg2_cuvid             vble
bink                    mpeg2_qsv               vbn
binkaudio_dct           mpeg2video              vc1
binkaudio_rdft          mpeg4                   vc1_cuvid
bintext                 mpeg4_cuvid             vc1_qsv
bitpacked               mpegvideo               vc1image
bmp                     mpl2                    vcr1
bmv_audio               msa1                    vmdaudio
bmv_video               mscc                    vmdvideo
bonk                    msmpeg4v1               vmix
brender_pix             msmpeg4v2               vmnc
c93                     msmpeg4v3               vnull
cavs                    msnsiren                vorbis
cbd2_dpcm               msp2                    vp3
ccaption                msrle                   vp4
cdgraphics              mss1                    vp5
cdtoons                 mss2                    vp6
cdxl                    msvideo1                vp6a
cfhd                    mszh                    vp6f
cinepak                 mts2                    vp7
clearvideo              mv30                    vp8
cljr                    mvc1                    vp8_cuvid
cllc                    mvc2                    vp8_qsv
comfortnoise            mvdv                    vp9
cook                    mvha                    vp9_amf
cpia                    mwsc                    vp9_cuvid
cri                     mxpeg                   vp9_qsv
cscd                    nellymoser              vplayer
cyuv                    notchlc                 vqa
dca                     nuv                     vqc
dds                     on2avc                  vvc
derf_dpcm               opus                    vvc_qsv
dfa                     osq                     wady_dpcm
dfpwm                   paf_audio               wavarc
dirac                   paf_video               wavpack
dnxhd                   pam                     wbmp
dolby_e                 pbm                     wcmv
dpx                     pcm_alaw                webp
dsd_lsbf                pcm_bluray              webp_anim
dsd_lsbf_planar         pcm_dvd                 webvtt
dsd_msbf                pcm_f16le               wmalossless
dsd_msbf_planar         pcm_f24le               wmapro
dsicinaudio             pcm_f32be               wmav1
dsicinvideo             pcm_f32le               wmav2
dss_sp                  pcm_f64be               wmavoice
dst                     pcm_f64le               wmv1
dvaudio                 pcm_lxf                 wmv2
dvbsub                  pcm_mulaw               wmv3
dvdsub                  pcm_s16be               wmv3image
dvvideo                 pcm_s16be_planar        wnv1
dxa                     pcm_s16le               wrapped_avframe
dxtory                  pcm_s16le_planar        ws_snd1
dxv                     pcm_s24be               xan_dpcm
eac3                    pcm_s24daud             xan_wc3
eacmv                   pcm_s24le               xan_wc4
eamad                   pcm_s24le_planar        xbin
eatgq                   pcm_s32be               xbm
eatgv                   pcm_s32le               xface
eatqi                   pcm_s32le_planar        xl
eightbps                pcm_s64be               xma1
eightsvx_exp            pcm_s64le               xma2
eightsvx_fib            pcm_s8                  xpm
escape124               pcm_s8_planar           xsub
escape130               pcm_sga                 xwd
evrc                    pcm_u16be               y41p
exr                     pcm_u16le               ylc
fastaudio               pcm_u24be               yop
ffv1                    pcm_u24le               yuv4
ffvhuff                 pcm_u32be               zero12v
ffwavesynth             pcm_u32le               zerocodec
fic                     pcm_u8                  zlib
fits                    pcm_vidc                zmbv
flac                    pcx

Enabled encoders:
a64multi                hevc_amf                pcm_u16le
a64multi5               hevc_d3d12va            pcm_u24be
aac                     hevc_mf                 pcm_u24le
aac_mf                  hevc_nvenc              pcm_u32be
ac3                     hevc_qsv                pcm_u32le
ac3_fixed               hevc_vaapi              pcm_u8
ac3_mf                  huffyuv                 pcm_vidc
adpcm_adx               jpeg2000                pcx
adpcm_argo              jpegls                  pdv
adpcm_g722              libaom_av1              pfm
adpcm_g726              libgsm                  pgm
adpcm_g726le            libgsm_ms               pgmyuv
adpcm_ima_alp           libmp3lame              phm
adpcm_ima_amv           libopencore_amrnb       png
adpcm_ima_apm           libopenjpeg             ppm
adpcm_ima_qt            libopus                 prores
adpcm_ima_ssi           libspeex                prores_aw
adpcm_ima_wav           libtheora               prores_ks
adpcm_ima_ws            libvo_amrwbenc          qoi
adpcm_ms                libvorbis               qtrle
adpcm_swf               libvpx_vp8              r10k
adpcm_yamaha            libvpx_vp9              r210
alac                    libwebp                 ra_144
alias_pix               libwebp_anim            rawvideo
amv                     libx264                 roq
anull                   libx264rgb              roq_dpcm
apng                    libx265                 rpza
aptx                    libxvid                 rv10
aptx_hd                 ljpeg                   rv20
ass                     magicyuv                s302m
asv1                    mjpeg                   sbc
asv2                    mjpeg_qsv               sgi
av1_amf                 mjpeg_vaapi             smc
av1_d3d12va             mlp                     snow
av1_mf                  movtext                 speedhq
av1_nvenc               mp2                     srt
av1_qsv                 mp2fixed                ssa
av1_vaapi               mp3_mf                  subrip
avrp                    mpeg1video              sunrast
avui                    mpeg2_qsv               svq1
bitpacked               mpeg2_vaapi             targa
bmp                     mpeg2video              text
cfhd                    mpeg4                   tiff
cinepak                 msmpeg4v2               truehd
cljr                    msmpeg4v3               tta
comfortnoise            msrle                   ttml
dca                     msvideo1                utvideo
dfpwm                   nellymoser              v210
dnxhd                   opus                    v308
dpx                     pam                     v408
dvbsub                  pbm                     v410
dvdsub                  pcm_alaw                vbn
dvvideo                 pcm_bluray              vc2
dxv                     pcm_dvd                 vnull
eac3                    pcm_f32be               vorbis
exr                     pcm_f32le               vp8_vaapi
ffv1                    pcm_f64be               vp9_qsv
ffvhuff                 pcm_f64le               vp9_vaapi
fits                    pcm_mulaw               wavpack
flac                    pcm_s16be               wbmp
flashsv                 pcm_s16be_planar        webvtt
flashsv2                pcm_s16le               wmav1
flv                     pcm_s16le_planar        wmav2
g723_1                  pcm_s24be               wmv1
gif                     pcm_s24daud             wmv2
h261                    pcm_s24le               wrapped_avframe
h263                    pcm_s24le_planar        xbm
h263p                   pcm_s32be               xface
h264_amf                pcm_s32le               xsub
h264_d3d12va            pcm_s32le_planar        xwd
h264_mf                 pcm_s64be               y41p
h264_nvenc              pcm_s64le               yuv4
h264_qsv                pcm_s8                  zlib
h264_vaapi              pcm_s8_planar           zmbv
hdr                     pcm_u16be

Enabled hwaccels:
av1_d3d11va             hevc_nvdec              vc1_nvdec
av1_d3d11va2            hevc_vaapi              vc1_vaapi
av1_d3d12va             mjpeg_nvdec             vp8_nvdec
av1_dxva2               mjpeg_vaapi             vp8_vaapi
av1_nvdec               mpeg1_nvdec             vp9_d3d11va
av1_vaapi               mpeg2_d3d11va           vp9_d3d11va2
h263_vaapi              mpeg2_d3d11va2          vp9_d3d12va
h264_d3d11va            mpeg2_d3d12va           vp9_dxva2
h264_d3d11va2           mpeg2_dxva2             vp9_nvdec
h264_d3d12va            mpeg2_nvdec             vp9_vaapi
h264_dxva2              mpeg2_vaapi             vvc_vaapi
h264_nvdec              mpeg4_nvdec             wmv3_d3d11va
h264_vaapi              mpeg4_vaapi             wmv3_d3d11va2
hevc_d3d11va            vc1_d3d11va             wmv3_d3d12va
hevc_d3d11va2           vc1_d3d11va2            wmv3_dxva2
hevc_d3d12va            vc1_d3d12va             wmv3_nvdec
hevc_dxva2              vc1_dxva2               wmv3_vaapi

Enabled parsers:
aac                     dvdsub                  mpegaudio
aac_latm                evc                     mpegvideo
ac3                     ffv1                    opus
adx                     flac                    png
ahx                     ftr                     pnm
amr                     g723_1                  prores
apv                     g729                    prores_raw
av1                     gif                     qoi
avs2                    gsm                     rv34
avs3                    h261                    sbc
bmp                     h263                    sipr
cavsvideo               h264                    tak
cook                    hdr                     vc1
cri                     hevc                    vorbis
dca                     ipu                     vp3
dirac                   jpeg2000                vp8
dnxhd                   jpegxl                  vp9
dnxuc                   jpegxs                  vvc
dolby_e                 lcevc                   webp
dpx                     misc4                   xbm
dvaudio                 mjpeg                   xma
dvbsub                  mlp                     xwd
dvd_nav                 mpeg4video

Enabled demuxers:
aa                      idcin                   pcm_mulaw
aac                     idf                     pcm_s16be
aax                     iff                     pcm_s16le
ac3                     ifv                     pcm_s24be
ac4                     ilbc                    pcm_s24le
ace                     image2                  pcm_s32be
acm                     image2_alias_pix        pcm_s32le
act                     image2_brender_pix      pcm_s8
adf                     image2pipe              pcm_u16be
adp                     image_bmp_pipe          pcm_u16le
ads                     image_cri_pipe          pcm_u24be
adx                     image_dds_pipe          pcm_u24le
aea                     image_dpx_pipe          pcm_u32be
afc                     image_exr_pipe          pcm_u32le
aiff                    image_gem_pipe          pcm_u8
aix                     image_gif_pipe          pcm_vidc
alp                     image_hdr_pipe          pdv
amr                     image_j2k_pipe          pjs
amrnb                   image_jpeg_pipe         pmp
amrwb                   image_jpegls_pipe       pp_bnk
anm                     image_jpegxl_pipe       pva
apac                    image_jpegxs_pipe       pvf
apc                     image_pam_pipe          qcp
ape                     image_pbm_pipe          qoa
apm                     image_pcx_pipe          r3d
apng                    image_pfm_pipe          rawvideo
aptx                    image_pgm_pipe          rcwt
aptx_hd                 image_pgmyuv_pipe       realtext
apv                     image_pgx_pipe          redspark
aqtitle                 image_phm_pipe          rka
argo_asf                image_photocd_pipe      rl2
argo_brp                image_pictor_pipe       rm
argo_cvg                image_png_pipe          roq
asf                     image_ppm_pipe          rpl
asf_o                   image_psd_pipe          rsd
ass                     image_qdraw_pipe        rso
ast                     image_qoi_pipe          rtp
au                      image_sgi_pipe          rtsp
av1                     image_sunrast_pipe      s337m
avi                     image_svg_pipe          sami
avisynth                image_tiff_pipe         sap
avr                     image_vbn_pipe          sbc
avs                     image_webp_pipe         sbg
avs2                    image_xbm_pipe          scc
avs3                    image_xpm_pipe          scd
bethsoftvid             image_xwd_pipe          sdns
bfi                     imf                     sdp
bfstm                   ingenient               sdr2
bink                    ipmovie                 sds
binka                   ipu                     sdx
bintext                 ircam                   segafilm
bit                     iss                     ser
bitpacked               iv8                     sga
bmv                     ivf                     shorten
boa                     ivr                     siff
bonk                    jacosub                 simbiosis_imx
brstm                   jpegxl_anim             sln
c93                     jv                      smacker
caf                     kux                     smjpeg
cavsvideo               kvag                    smush
cdg                     laf                     sol
cdxl                    lc3                     sox
cine                    libgme                  spdif
codec2                  libopenmpt              srt
codec2raw               live_flv                stl
concat                  lmlm4                   str
dash                    loas                    subviewer
data                    lrc                     subviewer1
daud                    luodat                  sup
dcstr                   lvf                     svag
derf                    lxf                     svs
dfa                     m4v                     swf
dfpwm                   matroska                tak
dhav                    mca                     tedcaptions
dirac                   mcc                     thp
dnxhd                   mgsts                   threedostr
dsf                     microdvd                tiertexseq
dsicin                  mjpeg                   tmv
dss                     mjpeg_2000              truehd
dts                     mlp                     tta
dtshd                   mlv                     tty
dv                      mm                      txd
dvbsub                  mmf                     ty
dvbtxt                  mods                    usm
dxa                     moflex                  v210
ea                      mov                     v210x
ea_cdata                mp3                     vag
eac3                    mpc                     vc1
epaf                    mpc8                    vc1t
evc                     mpegps                  vividas
ffmetadata              mpegts                  vivo
filmstrip               mpegtsraw               vmd
fits                    mpegvideo               vobsub
flac                    mpjpeg                  voc
flic                    mpl2                    vpk
flv                     mpsub                   vplayer
fourxm                  msf                     vqf
frm                     msnwc_tcp               vvc
fsb                     msp                     w64
fwse                    mtaf                    wady
g722                    mtv                     wav
g723_1                  musx                    wavarc
g726                    mv                      wc3
g726le                  mvi                     webm_dash_manifest
g728                    mxf                     webp_anim
g729                    mxg                     webvtt
gdv                     nc                      wsaud
genh                    nistsphere              wsd
gif                     nsp                     wsvqa
gsm                     nsv                     wtv
gxf                     nut                     wv
h261                    nuv                     wve
h263                    obu                     xa
h264                    ogg                     xbin
hca                     oma                     xmd
hcom                    osq                     xmv
hevc                    paf                     xvag
hls                     pcm_alaw                xwma
hnm                     pcm_f32be               yop
hxvs                    pcm_f32le               yuv4mpegpipe
iamf                    pcm_f64be
ico                     pcm_f64le

Enabled muxers:
a64                     h264                    pcm_s24le
ac3                     hash                    pcm_s32be
ac4                     hds                     pcm_s32le
adts                    hevc                    pcm_s8
adx                     hls                     pcm_u16be
aea                     iamf                    pcm_u16le
aiff                    ico                     pcm_u24be
alp                     ilbc                    pcm_u24le
amr                     image2                  pcm_u32be
amv                     image2pipe              pcm_u32le
apm                     ipod                    pcm_u8
apng                    ircam                   pcm_vidc
aptx                    ismv                    pdv
aptx_hd                 ivf                     psp
apv                     jacosub                 rawvideo
argo_asf                kvag                    rcwt
argo_cvg                latm                    rm
asf                     lc3                     roq
asf_stream              lrc                     rso
ass                     m4v                     rtp
ast                     matroska                rtp_mpegts
au                      matroska_audio          rtsp
avi                     mcc                     sap
avif                    md5                     sbc
avm2                    microdvd                scc
avs2                    mjpeg                   segafilm
avs3                    mkvtimestamp_v2         segment
bit                     mlp                     smjpeg
caf                     mmf                     smoothstreaming
cavsvideo               mov                     sox
codec2                  mp2                     spdif
codec2raw               mp3                     spx
crc                     mp4                     srt
dash                    mpeg1system             stream_segment
data                    mpeg1vcd                streamhash
daud                    mpeg1video              sup
dfpwm                   mpeg2dvd                swf
dirac                   mpeg2svcd               tee
dnxhd                   mpeg2video              tg2
dts                     mpeg2vob                tgp
dv                      mpegts                  truehd
eac3                    mpjpeg                  tta
evc                     mxf                     ttml
f4v                     mxf_d10                 uncodedframecrc
ffmetadata              mxf_opatom              vc1
fifo                    null                    vc1t
filmstrip               nut                     voc
fits                    obu                     vvc
flac                    oga                     w64
flv                     ogg                     wav
framecrc                ogv                     webm
framehash               oma                     webm_chunk
framemd5                opus                    webm_dash_manifest
g722                    pcm_alaw                webp
g723_1                  pcm_f32be               webvtt
g726                    pcm_f32le               whip
g726le                  pcm_f64be               wsaud
gif                     pcm_f64le               wtv
gsm                     pcm_mulaw               wv
gxf                     pcm_s16be               yuv4mpegpipe
h261                    pcm_s16le
h263                    pcm_s24be

Enabled protocols:
async                   http                    rtmp
cache                   httpproxy               rtmpe
concat                  https                   rtmps
concatf                 icecast                 rtmpt
crypto                  ipfs_gateway            rtmpte
data                    ipns_gateway            rtmpts
dtls                    libsrt                  rtp
fd                      libssh                  srtp
ffrtmpcrypt             libzmq                  subfile
ffrtmphttp              md5                     tcp
file                    mmsh                    tee
ftp                     mmst                    tls
gopher                  pipe                    udp
gophers                 prompeg                 udplite

Enabled filters:
a3dscope                dcshift                 pan
aap                     dctdnoiz                perlin
abench                  ddagrab                 perms
abitscope               deband                  perspective
acompressor             deblock                 phase
acontrast               decimate                photosensitivity
acopy                   deconvolve              pixdesctest
acrossfade              dedot                   pixelize
acrossover              deesser                 pixscope
acrusher                deflate                 pp7
acue                    deflicker               premultiply
addroi                  deinterlace_d3d12       premultiply_dynamic
adeclick                deinterlace_qsv         prewitt
adeclip                 deinterlace_vaapi       procamp_vaapi
adecorrelate            dejudder                pseudocolor
adelay                  delogo                  psnr
adenorm                 denoise_vaapi           pullup
aderivative             deshake                 qp
adrawgraph              despill                 random
adrc                    detelecine              readeia608
adynamicequalizer       dialoguenhance          readvitc
adynamicsmooth          dilation                realtime
aecho                   displace                remap
aemphasis               doubleweave             removegrain
aeval                   drawbox                 removelogo
aevalsrc                drawbox_vaapi           repeatfields
aexciter                drawgraph               replaygain
afade                   drawgrid                reverse
afdelaysrc              drawtext                rgbashift
afftdn                  drawvg                  rgbtestsrc
afftfilt                drmeter                 roberts
afir                    dynaudnorm              rotate
afireqsrc               earwax                  rubberband
afirsrc                 ebur128                 sab
aformat                 edgedetect              scale
afreqshift              elbg                    scale2ref
afwtdn                  entropy                 scale_cuda
agate                   epx                     scale_d3d11
agraphmonitor           eq                      scale_d3d12
ahistogram              equalizer               scale_qsv
aiir                    erosion                 scale_vaapi
aintegral               estdif                  scdet
ainterleave             exposure                scharr
alatency                extractplanes           scroll
alimiter                extrastereo             segment
allpass                 fade                    select
allrgb                  feedback                selectivecolor
allyuv                  fftdnoiz                sendcmd
aloop                   fftfilt                 separatefields
alphaextract            field                   setdar
alphamerge              fieldhint               setfield
amerge                  fieldmatch              setparams
ametadata               fieldorder              setpts
amf_capture             fillborders             setrange
amix                    find_rect               setsar
amovie                  firequalizer            settb
amplify                 flanger                 sharpness_vaapi
amultiply               floodfill               shear
anequalizer             format                  showcqt
anlmdn                  fps                     showcwt
anlmf                   framepack               showfreqs
anlms                   framerate               showinfo
anoisesrc               framestep               showpalette
anull                   frc_amf                 showspatial
anullsink               freezedetect            showspectrum
anullsrc                freezeframes            showspectrumpic
apad                    fspp                    showvolume
aperms                  fsync                   showwaves
aphasemeter             gblur                   showwavespic
aphaser                 geq                     shuffleframes
aphaseshift             gfxcapture              shufflepixels
apsnr                   gradfun                 shuffleplanes
apsyclip                gradients               sidechaincompress
apulsator               graphmonitor            sidechaingate
arealtime               grayworld               sidedata
aresample               greyedge                sierpinski
areverse                guided                  signalstats
arls                    haas                    signature
arnndn                  haldclut                silencedetect
asdr                    haldclutsrc             silenceremove
asegment                hdcd                    sinc
aselect                 headphone               sine
asendcmd                hflip                   siti
asetnsamples            highpass                smartblur
asetpts                 highshelf               smptebars
asetrate                hilbert                 smptehdbars
asettb                  histeq                  sobel
ashowinfo               histogram               spectrumsynth
asidedata               hqdn3d                  speechnorm
asisdr                  hqx                     split
asoftclip               hstack                  spp
aspectralstats          hstack_qsv              sr_amf
asplit                  hstack_vaapi            ssim
ass                     hsvhold                 ssim360
astats                  hsvkey                  stereo3d
astreamselect           hue                     stereotools
asubboost               huesaturation           stereowiden
asubcut                 hwdownload              streamselect
asupercut               hwmap                   subtitles
asuperpass              hwupload                super2xsai
asuperstop              hwupload_cuda           superequalizer
atadenoise              hysteresis              surround
atempo                  identity                swaprect
atilt                   idet                    swapuv
atrim                   il                      tblend
avectorscope            inflate                 telecine
avgblur                 interlace               testsrc
avsynctest              interleave              testsrc2
axcorrelate             join                    thistogram
azmq                    kerndeint               threshold
backgroundkey           kirsch                  thumbnail
bandpass                lagfun                  thumbnail_cuda
bandreject              latency                 tile
bass                    lenscorrection          tiltandshift
bbox                    libvmaf                 tiltshelf
bench                   life                    tinterlace
bilateral               limitdiff               tlut2
bilateral_cuda          limiter                 tmedian
biquad                  loop                    tmidequalizer
bitplanenoise           loudnorm                tmix
blackdetect             lowpass                 tonemap
blackframe              lowshelf                tonemap_vaapi
blend                   lumakey                 tpad
blockdetect             lut                     transpose
blurdetect              lut1d                   transpose_cuda
bm3d                    lut2                    transpose_vaapi
boxblur                 lut3d                   treble
bwdif                   lutrgb                  tremolo
bwdif_cuda              lutyuv                  trim
cas                     mandelbrot              unpremultiply
ccrepack                maskedclamp             unsharp
cellauto                maskedmax               untile
channelmap              maskedmerge             uspp
channelsplit            maskedmin               v360
chorus                  maskedthreshold         vaguedenoiser
chromahold              maskfun                 varblur
chromakey               mcdeint                 vectorscope
chromakey_cuda          mcompand                vflip
chromanr                median                  vfrdet
chromashift             mergeplanes             vibrance
ciescope                mestimate               vibrato
codecview               mestimate_d3d12         vidstabdetect
color                   metadata                vidstabtransform
colorbalance            midequalizer            vif
colorchannelmixer       minterpolate            vignette
colorchart              mix                     virtualbass
colorcontrast           monochrome              vmafmotion
colorcorrect            morpho                  volume
colordetect             movie                   volumedetect
colorhold               mpdecimate              vpp_amf
colorize                mptestsrc               vpp_qsv
colorkey                msad                    vstack
colorlevels             multiply                vstack_qsv
colormap                negate                  vstack_vaapi
colormatrix             nlmeans                 w3fdif
colorspace              nnedi                   waveform
colorspace_cuda         noformat                weave
colorspectrum           noise                   xbr
colortemperature        normalize               xcorrelate
compand                 null                    xfade
compensationdelay       nullsink                xmedian
concat                  nullsrc                 xpsnr
convolution             oscilloscope            xstack
convolve                overlay                 xstack_qsv
copy                    overlay_cuda            xstack_vaapi
corr                    overlay_qsv             yadif
cover_rect              overlay_vaapi           yadif_cuda
crop                    owdenoise               yaepblur
cropdetect              pad                     yuvtestsrc
crossfeed               pad_cuda                zmq
crystalizer             pad_vaapi               zoneplate
cue                     pal100bars              zoompan
curves                  pal75bars               zscale
datascope               palettegen
dblur                   paletteuse

Enabled bsfs:
aac_adtstoasc           filter_units            opus_metadata
ahx_to_mp2              h264_metadata           pcm_rechunk
apv_metadata            h264_mp4toannexb        pgs_frame_merge
av1_frame_merge         h264_redundant_pps      prores_metadata
av1_frame_split         hapqa_extract           remove_extradata
av1_metadata            hevc_metadata           setts
chomp                   hevc_mp4toannexb        showinfo
dca_core                imx_dump_header         smpte436m_to_eia608
dovi_rpu                lcevc_metadata          text2movsub
dovi_split              media100_to_mjpegb      trace_headers
dts2pts                 mjpeg2jpeg              truehd_core
dump_extradata          mjpega_dump_header      vp9_metadata
dv_error_marker         mov2textsub             vp9_raw_reorder
eac3_core               mpeg2_metadata          vp9_superframe
eia608_to_smpte436m     mpeg4_unpack_bframes    vp9_superframe_split
evc_frame_merge         noise                   vvc_metadata
extract_extradata       null                    vvc_mp4toannexb

Enabled indevs:
dshow                   lavfi                   vfwcap
gdigrab                 openal

Enabled outdevs:

git-essentials external libraries' versions: 

AMF v1.5.2
aom v3.14.1-67-g515f603aa0
AviSynthPlus v3.7.5-316-g345a0003
cairo 1.18.5
ffnvcodec n13.0.19.0-5-g1b5a81a
gsm 1.0.24
lame 3.100
libgme 0.6.6
libopencore-amrnb 0.1.6
libopencore-amrwb 0.1.6
libssh 0.12.0
libtheora v1.2.0
libwebp v1.6.0-187-gb43b2ca
openal-soft latest
openmpt libopenmpt-0.6.28-25-g1d77fab8
opus v1.6.1-50-g3da9f7a6
rubberband v1.8.1
SDL release-2.32.0-211-g1414dbf29
speex Speex-1.2.1-51-g0589522
srt v1.5.5-9-gc39196c
VAAPI 2.24.0.
vidstab v1.1.1-24-g92bc0b0
vmaf v3.1.0-129-g8461ae08
vo-amrwbenc 0.1.3
vorbis v1.3.7-36-ge3c9861f
VPL 2.16
vpx v1.16.0-150-g41e48324d
x264 v0.165.3223
x265 4.2-54-g3f156cd
xvid v1.3.7
zeromq 4.3.5
zimg release-3.0.6-218-gfa52dee


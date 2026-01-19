FFmpeg 64-bit static Windows build from www.gyan.dev

Version: 2025-11-02-git-f5eb11a71d-essentials_build-www.gyan.dev

License: GPL v3

Source Code: https://github.com/FFmpeg/FFmpeg/commit/f5eb11a71d

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
dsd_lsbf                pcm_bluray              webvtt
dsd_lsbf_planar         pcm_dvd                 wmalossless
dsd_msbf                pcm_f16le               wmapro
dsd_msbf_planar         pcm_f24le               wmav1
dsicinaudio             pcm_f32be               wmav2
dsicinvideo             pcm_f32le               wmavoice
dss_sp                  pcm_f64be               wmv1
dst                     pcm_f64le               wmv2
dvaudio                 pcm_lxf                 wmv3
dvbsub                  pcm_mulaw               wmv3image
dvdsub                  pcm_s16be               wnv1
dvvideo                 pcm_s16be_planar        wrapped_avframe
dxa                     pcm_s16le               ws_snd1
dxtory                  pcm_s16le_planar        xan_dpcm
dxv                     pcm_s24be               xan_wc3
eac3                    pcm_s24daud             xan_wc4
eacmv                   pcm_s24le               xbin
eamad                   pcm_s24le_planar        xbm
eatgq                   pcm_s32be               xface
eatgv                   pcm_s32le               xl
eatqi                   pcm_s32le_planar        xma1
eightbps                pcm_s64be               xma2
eightsvx_exp            pcm_s64le               xpm
eightsvx_fib            pcm_s8                  xsub
escape124               pcm_s8_planar           xwd
escape130               pcm_sga                 y41p
evrc                    pcm_u16be               ylc
exr                     pcm_u16le               yop
fastaudio               pcm_u24be               yuv4
ffv1                    pcm_u24le               zero12v
ffvhuff                 pcm_u32be               zerocodec
ffwavesynth             pcm_u32le               zlib
fic                     pcm_u8                  zmbv
fits                    pcm_vidc
flac                    pcx

Enabled encoders:
a64multi                hevc_amf                pcm_u16be
a64multi5               hevc_d3d12va            pcm_u16le
aac                     hevc_mf                 pcm_u24be
aac_mf                  hevc_nvenc              pcm_u24le
ac3                     hevc_qsv                pcm_u32be
ac3_fixed               hevc_vaapi              pcm_u32le
ac3_mf                  huffyuv                 pcm_u8
adpcm_adx               jpeg2000                pcm_vidc
adpcm_argo              jpegls                  pcx
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
av1_mf                  mlp                     snow
av1_nvenc               movtext                 speedhq
av1_qsv                 mp2                     srt
av1_vaapi               mp2fixed                ssa
avrp                    mp3_mf                  subrip
avui                    mpeg1video              sunrast
bitpacked               mpeg2_qsv               svq1
bmp                     mpeg2_vaapi             targa
cfhd                    mpeg2video              text
cinepak                 mpeg4                   tiff
cljr                    msmpeg4v2               truehd
comfortnoise            msmpeg4v3               tta
dca                     msrle                   ttml
dfpwm                   msvideo1                utvideo
dnxhd                   nellymoser              v210
dpx                     opus                    v308
dvbsub                  pam                     v408
dvdsub                  pbm                     v410
dvvideo                 pcm_alaw                vbn
dxv                     pcm_bluray              vc2
eac3                    pcm_dvd                 vnull
exr                     pcm_f32be               vorbis
ffv1                    pcm_f32le               vp8_vaapi
ffvhuff                 pcm_f64be               vp9_qsv
fits                    pcm_f64le               vp9_vaapi
flac                    pcm_mulaw               wavpack
flashsv                 pcm_s16be               wbmp
flashsv2                pcm_s16be_planar        webvtt
flv                     pcm_s16le               wmav1
g723_1                  pcm_s16le_planar        wmav2
gif                     pcm_s24be               wmv1
h261                    pcm_s24daud             wmv2
h263                    pcm_s24le               wrapped_avframe
h263p                   pcm_s24le_planar        xbm
h264_amf                pcm_s32be               xface
h264_d3d12va            pcm_s32le               xsub
h264_mf                 pcm_s32le_planar        xwd
h264_nvenc              pcm_s64be               y41p
h264_qsv                pcm_s64le               yuv4
h264_vaapi              pcm_s8                  zlib
hdr                     pcm_s8_planar           zmbv

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
aac                     dvd_nav                 mpegaudio
aac_latm                dvdsub                  mpegvideo
ac3                     evc                     opus
adx                     ffv1                    png
ahx                     flac                    pnm
amr                     ftr                     prores
apv                     g723_1                  prores_raw
av1                     g729                    qoi
avs2                    gif                     rv34
avs3                    gsm                     sbc
bmp                     h261                    sipr
cavsvideo               h263                    tak
cook                    h264                    vc1
cri                     hdr                     vorbis
dca                     hevc                    vp3
dirac                   ipu                     vp8
dnxhd                   jpeg2000                vp9
dnxuc                   jpegxl                  vvc
dolby_e                 misc4                   webp
dpx                     mjpeg                   xbm
dvaudio                 mlp                     xma
dvbsub                  mpeg4video              xwd

Enabled demuxers:
aa                      ico                     pcm_f64le
aac                     idcin                   pcm_mulaw
aax                     idf                     pcm_s16be
ac3                     iff                     pcm_s16le
ac4                     ifv                     pcm_s24be
ace                     ilbc                    pcm_s24le
acm                     image2                  pcm_s32be
act                     image2_alias_pix        pcm_s32le
adf                     image2_brender_pix      pcm_s8
adp                     image2pipe              pcm_u16be
ads                     image_bmp_pipe          pcm_u16le
adx                     image_cri_pipe          pcm_u24be
aea                     image_dds_pipe          pcm_u24le
afc                     image_dpx_pipe          pcm_u32be
aiff                    image_exr_pipe          pcm_u32le
aix                     image_gem_pipe          pcm_u8
alp                     image_gif_pipe          pcm_vidc
amr                     image_hdr_pipe          pdv
amrnb                   image_j2k_pipe          pjs
amrwb                   image_jpeg_pipe         pmp
anm                     image_jpegls_pipe       pp_bnk
apac                    image_jpegxl_pipe       pva
apc                     image_pam_pipe          pvf
ape                     image_pbm_pipe          qcp
apm                     image_pcx_pipe          qoa
apng                    image_pfm_pipe          r3d
aptx                    image_pgm_pipe          rawvideo
aptx_hd                 image_pgmyuv_pipe       rcwt
apv                     image_pgx_pipe          realtext
aqtitle                 image_phm_pipe          redspark
argo_asf                image_photocd_pipe      rka
argo_brp                image_pictor_pipe       rl2
argo_cvg                image_png_pipe          rm
asf                     image_ppm_pipe          roq
asf_o                   image_psd_pipe          rpl
ass                     image_qdraw_pipe        rsd
ast                     image_qoi_pipe          rso
au                      image_sgi_pipe          rtp
av1                     image_sunrast_pipe      rtsp
avi                     image_svg_pipe          s337m
avisynth                image_tiff_pipe         sami
avr                     image_vbn_pipe          sap
avs                     image_webp_pipe         sbc
avs2                    image_xbm_pipe          sbg
avs3                    image_xpm_pipe          scc
bethsoftvid             image_xwd_pipe          scd
bfi                     imf                     sdns
bfstm                   ingenient               sdp
bink                    ipmovie                 sdr2
binka                   ipu                     sds
bintext                 ircam                   sdx
bit                     iss                     segafilm
bitpacked               iv8                     ser
bmv                     ivf                     sga
boa                     ivr                     shorten
bonk                    jacosub                 siff
brstm                   jpegxl_anim             simbiosis_imx
c93                     jv                      sln
caf                     kux                     smacker
cavsvideo               kvag                    smjpeg
cdg                     laf                     smush
cdxl                    lc3                     sol
cine                    libgme                  sox
codec2                  libopenmpt              spdif
codec2raw               live_flv                srt
concat                  lmlm4                   stl
dash                    loas                    str
data                    lrc                     subviewer
daud                    luodat                  subviewer1
dcstr                   lvf                     sup
derf                    lxf                     svag
dfa                     m4v                     svs
dfpwm                   matroska                swf
dhav                    mca                     tak
dirac                   mcc                     tedcaptions
dnxhd                   mgsts                   thp
dsf                     microdvd                threedostr
dsicin                  mjpeg                   tiertexseq
dss                     mjpeg_2000              tmv
dts                     mlp                     truehd
dtshd                   mlv                     tta
dv                      mm                      tty
dvbsub                  mmf                     txd
dvbtxt                  mods                    ty
dxa                     moflex                  usm
ea                      mov                     v210
ea_cdata                mp3                     v210x
eac3                    mpc                     vag
epaf                    mpc8                    vc1
evc                     mpegps                  vc1t
ffmetadata              mpegts                  vividas
filmstrip               mpegtsraw               vivo
fits                    mpegvideo               vmd
flac                    mpjpeg                  vobsub
flic                    mpl2                    voc
flv                     mpsub                   vpk
fourxm                  msf                     vplayer
frm                     msnwc_tcp               vqf
fsb                     msp                     vvc
fwse                    mtaf                    w64
g722                    mtv                     wady
g723_1                  musx                    wav
g726                    mv                      wavarc
g726le                  mvi                     wc3
g728                    mxf                     webm_dash_manifest
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

Enabled muxers:
a64                     h263                    pcm_s16le
ac3                     h264                    pcm_s24be
ac4                     hash                    pcm_s24le
adts                    hds                     pcm_s32be
adx                     hevc                    pcm_s32le
aea                     hls                     pcm_s8
aiff                    iamf                    pcm_u16be
alp                     ico                     pcm_u16le
amr                     ilbc                    pcm_u24be
amv                     image2                  pcm_u24le
apm                     image2pipe              pcm_u32be
apng                    ipod                    pcm_u32le
aptx                    ircam                   pcm_u8
aptx_hd                 ismv                    pcm_vidc
apv                     ivf                     psp
argo_asf                jacosub                 rawvideo
argo_cvg                kvag                    rcwt
asf                     latm                    rm
asf_stream              lc3                     roq
ass                     lrc                     rso
ast                     m4v                     rtp
au                      matroska                rtp_mpegts
avi                     matroska_audio          rtsp
avif                    mcc                     sap
avm2                    md5                     sbc
avs2                    microdvd                scc
avs3                    mjpeg                   segafilm
bit                     mkvtimestamp_v2         segment
caf                     mlp                     smjpeg
cavsvideo               mmf                     smoothstreaming
codec2                  mov                     sox
codec2raw               mp2                     spdif
crc                     mp3                     spx
dash                    mp4                     srt
data                    mpeg1system             stream_segment
daud                    mpeg1vcd                streamhash
dfpwm                   mpeg1video              sup
dirac                   mpeg2dvd                swf
dnxhd                   mpeg2svcd               tee
dts                     mpeg2video              tg2
dv                      mpeg2vob                tgp
eac3                    mpegts                  truehd
evc                     mpjpeg                  tta
f4v                     mxf                     ttml
ffmetadata              mxf_d10                 uncodedframecrc
fifo                    mxf_opatom              vc1
filmstrip               null                    vc1t
fits                    nut                     voc
flac                    obu                     vvc
flv                     oga                     w64
framecrc                ogg                     wav
framehash               ogv                     webm
framemd5                oma                     webm_chunk
g722                    opus                    webm_dash_manifest
g723_1                  pcm_alaw                webp
g726                    pcm_f32be               webvtt
g726le                  pcm_f32le               wsaud
gif                     pcm_f64be               wtv
gsm                     pcm_f64le               wv
gxf                     pcm_mulaw               yuv4mpegpipe
h261                    pcm_s16be

Enabled protocols:
async                   http                    rtmp
cache                   httpproxy               rtmpe
concat                  https                   rtmps
concatf                 icecast                 rtmpt
crypto                  ipfs_gateway            rtmpte
data                    ipns_gateway            rtmpts
fd                      libsrt                  rtp
ffrtmpcrypt             libssh                  srtp
ffrtmphttp              libzmq                  subfile
file                    md5                     tcp
ftp                     mmsh                    tee
gopher                  mmst                    tls
gophers                 pipe                    udp
hls                     prompeg                 udplite

Enabled filters:
a3dscope                dblur                   pan
aap                     dcshift                 perlin
abench                  dctdnoiz                perms
abitscope               ddagrab                 perspective
acompressor             deband                  phase
acontrast               deblock                 photosensitivity
acopy                   decimate                pixdesctest
acrossfade              deconvolve              pixelize
acrossover              dedot                   pixscope
acrusher                deesser                 pp7
acue                    deflate                 premultiply
addroi                  deflicker               premultiply_dynamic
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
agraphmonitor           eq                      scale_qsv
ahistogram              equalizer               scale_vaapi
aiir                    erosion                 scdet
aintegral               estdif                  scharr
ainterleave             exposure                scroll
alatency                extractplanes           segment
alimiter                extrastereo             select
allpass                 fade                    selectivecolor
allrgb                  feedback                sendcmd
allyuv                  fftdnoiz                separatefields
aloop                   fftfilt                 setdar
alphaextract            field                   setfield
alphamerge              fieldhint               setparams
amerge                  fieldmatch              setpts
ametadata               fieldorder              setrange
amix                    fillborders             setsar
amovie                  find_rect               settb
amplify                 firequalizer            sharpness_vaapi
amultiply               flanger                 shear
anequalizer             floodfill               showcqt
anlmdn                  format                  showcwt
anlmf                   fps                     showfreqs
anlms                   framepack               showinfo
anoisesrc               framerate               showpalette
anull                   framestep               showspatial
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
blurdetect              lut1d                   transpose_vaapi
bm3d                    lut2                    treble
boxblur                 lut3d                   tremolo
bwdif                   lutrgb                  trim
bwdif_cuda              lutyuv                  unpremultiply
cas                     mandelbrot              unsharp
ccrepack                maskedclamp             untile
cellauto                maskedmax               uspp
channelmap              maskedmerge             v360
channelsplit            maskedmin               vaguedenoiser
chorus                  maskedthreshold         varblur
chromahold              maskfun                 vectorscope
chromakey               mcdeint                 vflip
chromakey_cuda          mcompand                vfrdet
chromanr                median                  vibrance
chromashift             mergeplanes             vibrato
ciescope                mestimate               vidstabdetect
codecview               metadata                vidstabtransform
color                   midequalizer            vif
colorbalance            minterpolate            vignette
colorchannelmixer       mix                     virtualbass
colorchart              monochrome              vmafmotion
colorcontrast           morpho                  volume
colorcorrect            movie                   volumedetect
colordetect             mpdecimate              vpp_amf
colorhold               mptestsrc               vpp_qsv
colorize                msad                    vstack
colorkey                multiply                vstack_qsv
colorlevels             negate                  vstack_vaapi
colormap                nlmeans                 w3fdif
colormatrix             nnedi                   waveform
colorspace              noformat                weave
colorspace_cuda         noise                   xbr
colorspectrum           normalize               xcorrelate
colortemperature        null                    xfade
compand                 nullsink                xmedian
compensationdelay       nullsrc                 xpsnr
concat                  oscilloscope            xstack
convolution             overlay                 xstack_qsv
convolve                overlay_cuda            xstack_vaapi
copy                    overlay_qsv             yadif
corr                    overlay_vaapi           yadif_cuda
cover_rect              owdenoise               yaepblur
crop                    pad                     yuvtestsrc
cropdetect              pad_cuda                zmq
crossfeed               pad_vaapi               zoneplate
crystalizer             pal100bars              zoompan
cue                     pal75bars               zscale
curves                  palettegen
datascope               paletteuse

Enabled bsfs:
aac_adtstoasc           h264_metadata           pgs_frame_merge
ahx_to_mp2              h264_mp4toannexb        prores_metadata
apv_metadata            h264_redundant_pps      remove_extradata
av1_frame_merge         hapqa_extract           setts
av1_frame_split         hevc_metadata           showinfo
av1_metadata            hevc_mp4toannexb        smpte436m_to_eia608
chomp                   imx_dump_header         text2movsub
dca_core                media100_to_mjpegb      trace_headers
dovi_rpu                mjpeg2jpeg              truehd_core
dts2pts                 mjpega_dump_header      vp9_metadata
dump_extradata          mov2textsub             vp9_raw_reorder
dv_error_marker         mpeg2_metadata          vp9_superframe
eac3_core               mpeg4_unpack_bframes    vp9_superframe_split
eia608_to_smpte436m     noise                   vvc_metadata
evc_frame_merge         null                    vvc_mp4toannexb
extract_extradata       opus_metadata
filter_units            pcm_rechunk

Enabled indevs:
dshow                   lavfi                   vfwcap
gdigrab                 openal

Enabled outdevs:

git-essentials external libraries' versions: 

AMF v1.5.0
aom v3.13.1-98-g7344913f4e
AviSynthPlus v3.7.5-37-gb6d79944
cairo 1.18.5
ffnvcodec n13.0.19.0-2-g876af32
freetype VER-2-14-1
fribidi v1.0.16-2-gb28f43b
gsm 1.0.22
harfbuzz 12.1.0-30-gcb6e12bf
lame 3.100
libass 0.17.4-15-g534a5f8
libgme 0.6.4
libopencore-amrnb 0.1.6
libopencore-amrwb 0.1.6
libssh 0.11.3
libtheora v1.2.0
libwebp v1.6.0-121-g2a755e7
openal-soft latest
openmpt libopenmpt-0.6.25-15-gb4adee7c9
opus v1.5.2-292-ge0dfb5c3
rubberband v1.8.1
SDL release-2.32.0-126-g398bacbac
speex Speex-1.2.1-51-g0589522
srt v1.5.5-rc.0a-5-gc09532f8
VAAPI 2.23.0.
vidstab v1.1.1-20-g4bd81e3
vmaf v3.0.0-117-g7c4beca3
vo-amrwbenc 0.1.3
vorbis v1.3.7-21-g851cce99
VPL 2.15
vpx v1.15.2-141-gcb5a578a5
x264 v0.165.3223
x265 4.1-201-gffba52bab
xvid v1.3.7
zeromq 4.3.5
zimg release-3.0.6-211-gdf9c147

